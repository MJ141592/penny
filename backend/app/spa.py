"""Serve the built SPA from the API process, on the same origin as `/api`.

Same origin is the entire reason `penny` is one service and not two: it deletes CORS,
`SameSite=None` and the CSRF token dance for the session cookie.

`mount_spa()` installs a catch-all GET route, so it MUST be called after every router is
registered — anything added later is unreachable.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings

log = logging.getLogger(__name__)

_APP_DIR = Path(__file__).resolve().parent

# The image copies the Vite build to /app/static, beside the app package; a checkout has
# it at frontend/dist. Deliberately not a setting: this is a fact about the two layouts,
# not a knob worth letting an operator point at the wrong directory.
STATIC_DIRS = (_APP_DIR.parent / "static", _APP_DIR.parents[1] / "frontend" / "dist")


def mount_spa(app: FastAPI) -> None:
    """Mount the built SPA, if there is one. Never raises: a missing build is a no-op."""
    if not get_settings().serve_frontend:
        log.info("serve_frontend is off; serving the API only")
        return

    static_dir = next((d for d in STATIC_DIRS if (d / "index.html").is_file()), None)
    if static_dir is None:
        # The normal backend-only dev loop: vite serves the SPA on :5173 and proxies
        # /api here. Crashing would make the API unusable until someone runs a build.
        log.info(
            "no built SPA found in %s; serving the API only",
            " or ".join(str(d) for d in STATIC_DIRS),
        )
        return

    index = static_dir / "index.html"
    files = StaticFiles(directory=static_dir)

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(request: Request, path: str) -> Response:
        """A real file when one exists, index.html otherwise — but never under /api.

        An unknown /api path must 404 as JSON. Falling back to index.html there turns
        every frontend API bug into "Unexpected token '<'", three layers from the cause.
        """
        if path == "api" or path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        try:
            return await files.get_response(path, request.scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            # no-cache, not no-store: assets are content-hashed and immutable, but a
            # cached shell after a deploy points at asset URLs that no longer exist.
            return FileResponse(index, headers={"cache-control": "no-cache"})

    log.info("serving the SPA from %s", static_dir)
