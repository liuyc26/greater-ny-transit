"""Local dev server: the API plus the static frontend from one process.

Run: .venv/bin/uvicorn transit.dev:app --reload
(In production the frontend is served by the CDN and only transit.app runs as a function.)
"""

from fastapi import Request
from fastapi.staticfiles import StaticFiles

from . import config
from .app import app


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    # Without Cache-Control, browsers guess how long to reuse files and can keep an old app.js or
    # old map data after a rebuild (whose shape ids then point at the wrong geometry). Make them
    # revalidate every time; unchanged files still come back as cheap 304s.
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


app.mount("/", StaticFiles(directory=config.ROOT / "public", html=True), name="public")
