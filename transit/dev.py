"""Local dev server: the API plus the static frontend from one process.

Run: .venv/bin/uvicorn transit.dev:app --reload
(In production the frontend is served by the CDN and only transit.app runs as a function.)
"""

from fastapi.staticfiles import StaticFiles

from . import config
from .app import app

app.mount("/", StaticFiles(directory=config.ROOT / "public", html=True), name="public")
