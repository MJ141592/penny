"""The two dependencies every authenticated route is built from.

`require_household` IS the tenancy mechanism. There is no row-level security, no policy engine
and no `users` table: the signed cookie names a household, this dependency turns it into a
`HouseholdCtx`, and **every query in the app filters on `ctx.id`**. A route that takes an id
from the path and forgets `where(household_id == ctx.id)` is a cross-tenant read, and nothing
below this line will catch it. That is the deal — one narrow mechanism, applied everywhere,
rather than several overlapping ones applied unevenly.

Two consequences worth stating:

* The dependency loads the `Household` row, not just the id. Routes need `name`, `timezone` and
  `care_recipient_name` constantly, and a context that carried only an id would mean every one
  of them re-fetching — or worse, trusting an id for a household that has since been deleted.
* A cookie that is valid but names a household that no longer exists is a **401**, not a 500
  and not a 404. The session is genuinely no longer usable, and the client's rule for 401 is
  exactly right: clear the cache and go to /login.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import UnauthorizedError
from app.models import Household
from app.security import SESSION_COOKIE_NAME, read_session_cookie

# NOT under TYPE_CHECKING, and this is the reason: `from __future__ import annotations` turns
# every parameter annotation into a string, and FastAPI resolves those strings at runtime to
# decide what each parameter IS. A `Request` it cannot resolve is not treated as the request —
# it becomes a required *body field*, and the endpoint answers 422 "request: Field required"
# for every caller, signed in or not. Same trap for `UUID` in any response model.

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@dataclass(frozen=True, slots=True)
class HouseholdCtx:
    """The authenticated tenant. `id` is the filter value; `household` is the loaded row."""

    id: UUID
    household: Household


async def require_household(request: Request, session: SessionDep) -> HouseholdCtx:
    """401 when the cookie is absent, unsigned, expired, or names an unknown household."""
    household_id = read_session_cookie(request.cookies.get(SESSION_COOKIE_NAME))
    if household_id is None:
        raise UnauthorizedError()
    household = await session.get(Household, household_id)
    if household is None:
        raise UnauthorizedError()
    return HouseholdCtx(id=household.id, household=household)


CurrentHousehold = Annotated[HouseholdCtx, Depends(require_household)]
