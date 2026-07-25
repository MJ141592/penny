"""Household settings: rename, change the shared passphrase, delete everything.

There is no users table. One household is one tenant, one shared credential and one care
recipient, so "account settings" and "household settings" are the same screen.
"""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import available_timezones

from fastapi import APIRouter, Response, status

from app.deps import CurrentHousehold, SessionDep
from app.errors import UnauthorizedError, ValidationError
from app.schemas import (
    MIN_PASSWORD_CHARS,
    HouseholdOut,
    HouseholdPatch,
    PasswordChange,
    to_household,
)
from app.security import clear_session_cookie, hash_password, verify_password

router = APIRouter(prefix="/api", tags=["household"])


@lru_cache(maxsize=1)
def _known_timezones() -> frozenset[str]:
    """`available_timezones()` walks the tzdata tree; it is stable for the process lifetime."""
    return frozenset(available_timezones())


@router.patch("/household", response_model=HouseholdOut)
async def patch_household(
    patch: HouseholdPatch,
    ctx: CurrentHousehold,
    session: SessionDep,
) -> HouseholdOut:
    """All fields optional; send only what changed."""
    household = ctx.household
    provided = patch.model_fields_set

    if "timezone" in provided and patch.timezone is not None:
        # Checked HERE as well as in the model validator, because the model validator raises
        # ValueError, which surfaces as a 500. A typo'd timezone is a user mistake and has to
        # read like one. Rejecting at the write is the whole point: `ZoneInfo(bad)` only
        # explodes at render time, deep inside a request unrelated to the one that stored it.
        if patch.timezone not in _known_timezones():
            raise ValidationError(f"{patch.timezone!r} is not a known timezone.")
        household.timezone = patch.timezone
    if "name" in provided and patch.name is not None:
        household.name = patch.name
    if "care_recipient_name" in provided and patch.care_recipient_name is not None:
        household.care_recipient_name = patch.care_recipient_name

    await session.flush()  # never commit: get_session owns the transaction
    return to_household(household)


@router.post("/household/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChange,
    ctx: CurrentHousehold,
    session: SessionDep,
) -> Response:
    """Change the shared passphrase. Existing sessions stay valid, deliberately.

    The cookie carries `household_id`, not the password, so nobody gets signed out — the
    family is not locked out of their own care record by one person changing a setting.
    Re-sharing the new passphrase is a conversation, not a feature.
    """
    household = ctx.household
    if not verify_password(body.current_password, household.password_hash):
        raise UnauthorizedError("That password is not correct.")
    if len(body.new_password) < MIN_PASSWORD_CHARS:
        raise ValidationError(
            f"Your new password must be at least {MIN_PASSWORD_CHARS} characters."
        )
    household.password_hash = hash_password(body.new_password)
    await session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/household", status_code=status.HTTP_204_NO_CONTENT)
async def delete_household(ctx: CurrentHousehold, session: SessionDep) -> Response:
    """Irreversible. Every message, event, member, import and report goes with it.

    The cascade is `ON DELETE CASCADE` in Postgres, declared on each child's `household_id`,
    so nothing here has to enumerate the tables — a table added later is covered by its own
    foreign key rather than by remembering to edit this function.
    """
    await session.delete(ctx.household)
    await session.flush()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    # The session now points at a household that does not exist. Leaving the cookie in place
    # would give every subsequent request a 401 that looks exactly like a broken login.
    clear_session_cookie(response)
    return response
