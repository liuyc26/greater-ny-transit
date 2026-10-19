# Greater NY Live Transit

A live map of the trains and buses that carry commuters into and out of Manhattan — LIRR, Metro-North,
NJ Transit rail and NJ Transit bus — drawn as dots moving along their routes. Hover a vehicle for its
train or route number, destination, next stop and delay; its route lights up.

Non-commercial hobby project, built to run at $0. Data from the MTA and NJ TRANSIT; not affiliated with
or endorsed by either, and information may not be real time.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env                       # NJ Transit developer credentials
.venv/bin/python scripts/build_static.py   # download schedules, build map data
.venv/bin/uvicorn transit.dev:app --reload --port 8765
```

Open http://localhost:8765. Run tests with `.venv/bin/python -m pytest`.

## Docs

See [doc/](doc/README.md): architecture, data sources and their quirks, development, deployment, roadmap.
