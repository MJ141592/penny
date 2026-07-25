"""Session revocation: the cookie carries `session_version`, and a bump invalidates it.

These run against REAL Postgres and skip without `PENNY_TEST_DATABASE_URL`, because the whole
mechanism is a comparison between a signed cookie and a column.

Every test here guards something that fails SILENTLY:

* Revocation itself is invisible from the happy path — a password change returns 204 whether or
  not the old cookie died with it, and the only way to see the difference is to keep the old
  cookie and try it afterwards. That is what a stolen cookie or a lost laptop *is*.
* `test_login_works_for_a_household_whose_sessions_were_revoked` is the one that catches the
  fix being quietly undone. `POST /api/auth/login` mints the cookie in `routers/auth.py`, and if
  that call ever loses its `household.session_version` argument the cookie goes back to carrying
  no version — which reads as version 1. Nothing looks broken until a family that has used the
  button cannot log in at all. Bumping the row *before* logging in is the only assertion that
  can tell "minted at the current version" apart from "minted at 1".
* The legacy-cookie test pins the pre-migration wire format on purpose. Cookies minted before
  `households.session_version` existed carry a bare UUID and are read as version 1, which is why
  the deploy does not sign the whole userbase out. Delete that behaviour and nobody finds out
  until every family is bounced to /login at once.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response as HttpxResponse
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.db
from app.db import to_asyncpg_url
from app.main import app as penny_app
from app.models import Household
from app.security import SESSION_COOKIE_NAME, hash_password

if TYPE_CHECKING:
    from app.config import Settings

pytestmark = pytest.mark.db

PASSWORD = "a-real-family-passphrase"
NEW_PASSWORD = "a-different-family-passphrase"
SESSION_SECRET = "test-session-secret-value-32-chars-min"
# The salt is spelled out rather than imported: this file's job includes proving that a cookie
# minted by the OLD code still verifies, and importing the constant would make the test agree
# with whatever the salt becomes instead of with what shipped.
LEGACY_SALT = "penny-session-v1"


def _run(db_url: str, fn: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
    """One statement on its own engine and loop — never the app's, which lives in the portal
    thread and whose asyncpg connections are bound to that loop."""

    async def go() -> Any:
        engine = create_async_engine(to_asyncpg_url(db_url))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                result = await fn(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture
def household(db_url: str) -> Iterator[Household]:
    """A throwaway household with a password we know, deleted afterwards by cascade."""
    row = Household(
        id=uuid.uuid4(),
        username=f"revocation-{uuid.uuid4().hex[:8]}",
        password_hash=hash_password(PASSWORD),
        name="The Revokers",
        care_recipient_name="Margaret",
        timezone="Europe/London",
    )

    async def create(session: AsyncSession) -> None:
        session.add(row)

    _run(db_url, create)
    try:
        yield row
    finally:
        _run(db_url, lambda s: s.execute(sa.delete(Household).where(Household.id == row.id)))


@pytest.fixture
def api(
    db_url: str, settings_override: Callable[..., Settings], household: Household
) -> Iterator[TestClient]:
    settings_override(
        env="test",
        database_url=db_url,
        test_database_url=db_url,
        session_secret=SESSION_SECRET,
    )
    # The engine is cached per process and holds whatever url ran last; rebuild it against ours.
    app.db.get_engine.cache_clear()
    app.db.get_sessionmaker.cache_clear()
    with TestClient(penny_app) as client:
        yield client


def _as(token: str) -> dict[str, str]:
    """Send one specific cookie, rather than whatever the client's jar accumulated.

    The jar is a single browser. These tests are about several devices holding several cookies
    at once, which is exactly the situation the jar cannot represent.
    """
    return {"cookie": f"{SESSION_COOKIE_NAME}={token}"}


def _login(api: TestClient, household: Household, password: str) -> str:
    response = api.post(
        "/api/auth/login", json={"username": household.username, "password": password}
    )
    assert response.status_code == 204, response.text
    token = response.cookies[SESSION_COOKIE_NAME]
    api.cookies.clear()
    return token


def _me(api: TestClient, token: str) -> HttpxResponse:
    return api.get("/api/me", headers=_as(token))


def _set_version(db_url: str, household: Household, version: int) -> None:
    _run(
        db_url,
        lambda s: s.execute(
            sa.update(Household).where(Household.id == household.id).values(session_version=version)
        ),
    )


def test_a_password_change_revokes_every_other_device(
    api: TestClient, household: Household
) -> None:
    """The finding, in one test: the cookie that was stolen stops working, the one that made the
    change does not, and the new passphrase still gets you in."""
    stolen = _login(api, household, PASSWORD)
    assert _me(api, stolen).status_code == 200

    changed = api.post(
        "/api/household/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=_as(stolen),
    )
    assert changed.status_code == 204
    caller = changed.cookies[SESSION_COOKIE_NAME]

    assert _me(api, stolen).status_code == 401
    assert _me(api, stolen).json() == {"detail": "Not signed in."}  # never "revoked"
    assert _me(api, caller).status_code == 200
    assert _me(api, _login(api, household, NEW_PASSWORD)).status_code == 200


def test_sign_out_everywhere_includes_the_device_that_asked(
    api: TestClient, household: Household
) -> None:
    """ "Everywhere" that quietly meant "everywhere but here" would be a lie about a control."""
    phone = _login(api, household, PASSWORD)
    laptop = _login(api, household, PASSWORD)
    assert _me(api, phone).status_code == 200

    assert api.post("/api/household/sign-out-everywhere", headers=_as(laptop)).status_code == 204

    assert _me(api, laptop).status_code == 401
    assert _me(api, phone).status_code == 401
    # The passphrase is untouched, so signing back in is one screen away.
    assert _me(api, _login(api, household, PASSWORD)).status_code == 200


def test_login_works_for_a_household_whose_sessions_were_revoked(
    api: TestClient, household: Household, db_url: str
) -> None:
    """THE ONE THAT ROTS SILENTLY: login must mint at the household's CURRENT version.

    Bumped out of band to 7 so that a cookie minted at 1 — which is what a cookie carrying no
    version at all reads as — cannot pass. A login that dropped the version argument still
    returns its 204 and still sets a cookie; only this assertion notices that the family it
    belongs to can no longer get in.
    """
    _set_version(db_url, household, 7)

    assert _me(api, _login(api, household, PASSWORD)).status_code == 200


def test_a_cookie_minted_before_the_column_existed_still_works_until_the_first_bump(
    api: TestClient, household: Household
) -> None:
    """Deploy safety, and its limit. A pre-migration cookie is a bare UUID with no version."""
    legacy = URLSafeTimedSerializer(SESSION_SECRET, salt=LEGACY_SALT).dumps(str(household.id))

    assert _me(api, legacy).status_code == 200, "the deploy must not sign the whole userbase out"

    assert api.post("/api/household/sign-out-everywhere", headers=_as(legacy)).status_code == 204
    assert _me(api, legacy).status_code == 401, "and it must still be revocable"
