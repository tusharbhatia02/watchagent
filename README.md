# WatchAgent — Weather Monitor & AI Assistant

WatchAgent polls live weather for **Ottawa, Toronto, and Vancouver** from
[Open-Meteo](https://open-meteo.com), decides which changes are *notable* enough
to surface as events, persists both raw readings and detected events to SQLite,
and exposes everything through an HTTP API.

The interesting part is not the data collection — it is the **event detection
logic** (city-aware thresholds, delta-based detection, hysteresis, and
anti-spam) and the **Cursor engineering setup** (rules, an agent, and a runnable
data-analysis skill) that encodes this project's real conventions.

---

## System Overview

```
                       ┌──────────────────────────────────────────────┐
                       │                  WatchAgent                   │
                       │            (single FastAPI process)           │
                       │                                              │
   Open-Meteo API      │   ┌──────────────┐      ┌────────────────┐   │
  ┌──────────────┐     │   │  APScheduler │  every│    poller.py   │   │
  │ /v1/forecast │◄────┼───┤ AsyncIO job  ├──────►│  poll_city()   │   │
  │  current{...}│     │   │ (10 min)     │ tick  │  poll_all()    │   │
  └──────────────┘     │   └──────────────┘       └───────┬────────┘   │
                       │                                  │ fetch ok   │
                       │                                  ▼            │
                       │                       INSERT OR IGNORE        │
                       │                       (city,timestamp UNIQUE) │
                       │                                  │            │
                       │                      rowcount==1 │ (new only) │
                       │                                  ▼            │
                       │                          ┌────────────────┐   │
                       │                          │   events.py    │   │
                       │                          │ detect_events()│   │
                       │                          │ 5 detectors    │   │
                       │                          └───────┬────────┘   │
                       │                                  │            │
                       │              ┌───────────────────▼─────────┐  │
                       │              │      database.py (SQLite)   │  │
                       │              │   readings  │   events      │  │
                       │              │   (/data/watchagent.db)     │  │
                       │              └───────────────────┬─────────┘  │
                       │                                  │            │
                       │   ┌──────────────┐   reads       │            │
   HTTP client ◄───────┼───┤   main.py    │◄──────────────┘            │
  (curl / browser)     │   │ /health      │                            │
                       │   │ /readings    │                            │
                       │   │ /events      │                            │
                       │   └──────────────┘                            │
                       └──────────────────────────────────────────────┘

        poller ──────────────► storage ──────────────► API
   (Open-Meteo, deduped)   (SQLite, persisted        (FastAPI, read-only
                            on a Docker volume)        query endpoints)
```

**Data flow:** the scheduler fires `poll_all_cities()` every 10 minutes →
each city is fetched from Open-Meteo → the reading is written with
`INSERT OR IGNORE` so duplicate hourly timestamps are dropped → only a *newly
stored* reading (`cursor.rowcount == 1`) is run through `detect_events()` →
any detected events are written to the `events` table → the API serves both
tables read-only.

---

## Quick Start (Docker)

```bash
git clone https://github.com/yourusername/watchagent
cd watchagent
cp .env.example .env
docker compose up --build
```

After startup:

- API is reachable at **http://localhost:8000**
- The poller begins collecting readings immediately and then every
  `POLL_INTERVAL_MINUTES`
- The SQLite database lives on the named Docker volume `db_data` and
  **persists across container restarts**

### Environment variables

All variables are documented in [`.env.example`](.env.example). No credentials
are required or committed (Open-Meteo needs no API key).

| Variable | Default | Purpose |
|---|---|---|
| `DB_PATH` | `/data/watchagent.db` | SQLite file path (compose pins this to the mounted volume) |
| `POLL_INTERVAL_MINUTES` | `10` | How often the scheduler polls all cities |
| `LOG_LEVEL` | `INFO` | Root log level (`DEBUG` surfaces duplicate-skip logs) |

---

## Running Locally (without Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Use a local DB path so it doesn't try to write to /data
DB_PATH=./watchagent.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## API Reference

### `GET /health`

```bash
curl http://localhost:8000/health
```

```json
{ "status": "ok", "readings_stored": 42, "events_stored": 3 }
```

### `GET /readings`

Optional `city` filter; `limit` defaults to 50; most recent first.

```bash
curl "http://localhost:8000/readings?city=Ottawa&limit=5"
```

```json
{
  "readings": [
    {
      "id": 41,
      "city": "Ottawa",
      "timestamp": "2026-05-30T02:00",
      "temperature_2m": 10.1,
      "apparent_temperature": 6.1,
      "precipitation": 0.0,
      "wind_speed_10m": 18.5,
      "weather_code": 3,
      "fetched_at": "2026-05-30T06:14:32.552917+00:00"
    }
  ]
}
```

### `GET /events`

Optional `city` filter; `limit` defaults to 50; most recent first.

```bash
curl "http://localhost:8000/events?city=Toronto&limit=5"
```

```json
{
  "events": [
    {
      "id": 3,
      "city": "Toronto",
      "event_type": "high_wind",
      "severity": "warning",
      "description": "Toronto: high wind of 47.0 km/h exceeded the 45.0 km/h threshold",
      "timestamp": "2026-05-30T10:00",
      "triggered_at": "2026-05-30T10:01:05.123456+00:00",
      "reading_id": 18,
      "details": "{\"wind_speed_10m\": 47.0, \"threshold\": 45.0}"
    }
  ]
}
```

---

## Running the Tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

All tests are offline — any call to the real weather API is mocked with
[`respx`](https://lundberg.github.io/respx/). The suite covers the three
required areas:

- **`tests/test_deduplication.py`** — mocks Open-Meteo to return the *same*
  city+timestamp twice and asserts only one row is stored; a second test with
  two distinct timestamps asserts two rows.
- **`tests/test_event_detection.py`** — constructs controlled reading sequences
  and asserts each of the five detectors fires (and does *not* fire) as
  designed, including the hysteresis behavior for `high_wind` and the dry-run
  gate for `precipitation_onset`.
- **`tests/test_api.py`** — seeds a temporary DB and asserts the exact response
  shape of `/health`, `/readings`, and `/events`, including the `city` filter
  and `limit`.

---

## Technology Choices

| Choice | Why |
|---|---|
| **FastAPI** | Async-native (the poller is I/O-bound HTTP work), automatic request validation for `city`/`limit` query params, built-in OpenAPI docs at `/docs`, and a clean `lifespan` hook to wire up DB init + scheduler startup/shutdown. |
| **aiosqlite** | Zero-config, file-based persistence that satisfies the "persist across restarts" requirement with a single Docker volume — no separate DB container needed. Async driver keeps it off the event loop. The `(city, timestamp)` UNIQUE constraint does deduplication at the storage layer, which is the most reliable place for it. |
| **APScheduler (`AsyncIOScheduler`)** | Runs the polling job on the same asyncio event loop as FastAPI, so no extra threads/processes. Interval triggers map directly to "poll every N minutes," and `replace_existing` makes restarts idempotent. |
| **httpx** | Async HTTP client that pairs with FastAPI and has first-class test mocking via `respx`. |
| **SQLite (not Postgres)** | The dataset is tiny (a few readings per city per hour) and single-writer. SQLite removes an entire service from the stack while still meeting persistence and query needs. If this scaled to many writers, Postgres would be the swap. |

---

## Event Detection Design

Raw readings are noisy; the goal is to fire **selectively** — often enough to be
useful, rarely enough to be trusted. Two ideas drive the design:

1. **Context over absolutes.** A single hot reading means little; a *rapid
   change* from the previous reading is what signals something happening.
2. **City-awareness.** −10 °C is a normal winter day in Ottawa but alarming in
   Vancouver. Every threshold lives in `CITY_CONFIG` in
   [`app/events.py`](app/events.py) and is tuned per city — never hardcoded
   inside a detector.

```python
CITY_CONFIG = {
    "Ottawa":    {"wind_warning": 40, "temp_spike": 8, "cold_snap": -25, "heat_alert": 32},
    "Toronto":   {"wind_warning": 45, "temp_spike": 7, "cold_snap": -18, "heat_alert": 33},
    "Vancouver": {"wind_warning": 50, "temp_spike": 6, "cold_snap":  -5, "heat_alert": 28},
}
```

### A note on delta calculation

Temperature deltas are calculated against the **previous stored reading** for
that city, not strictly the previous clock-hour. Open-Meteo updates once per
hour, and the poller may have gaps (container restart, transient network error).
This means consecutive stored readings can span more than one hour. The detector
captures the sharpest observed change between consecutive stored readings rather
than assuming strict hourly continuity — which is the right behaviour: a 10 °C
swing over two stored readings is still a real event worth surfacing, regardless
of how much wall-clock time elapsed between polls.

### The five event types

| Event | Fires when | Severity | Reasoning |
|---|---|---|---|
| **temperature_spike / temperature_plunge** | hour-over-hour `temperature_2m` delta exceeds the city's `temp_spike` | `warning`, escalates to `critical` when the new temp crosses `heat_alert` / `cold_snap` | Delta-based, not absolute — captures *change*. Coastal Vancouver gets a lower delta threshold (6 °C) because its temperature is normally stable, so a 6 °C swing is genuinely unusual there. |
| **precipitation_onset** | current reading is wet (≥ 0.1 mm) **and** the previous 3 readings were all dry | `info` | The *onset* of rain/snow after a dry spell is the signal; ongoing precipitation is not re-fired. The 3-reading dry run is anti-spam. |
| **high_wind** | `wind_speed_10m` ≥ city `wind_warning`, **with hysteresis** | `warning`, `critical` at ≥ 1.5× threshold | Sustained wind would otherwise fire on every poll. Hysteresis requires a *calm* reading (below threshold) between two high-wind events, so one windstorm = one event, not a storm of duplicates. |
| **severe_weather_code** | `weather_code` is in `SEVERE_CODES` (snow 71/73/75/77, violent showers 82/85, thunderstorms 95/96/99) | per-code: thunderstorms/heavy snow `critical`, light snow `info` | WMO codes are categorical ground truth from the API for hazardous conditions. |
| **feels_like_divergence** | `|apparent_temperature − temperature_2m|` ≥ 10 °C | `warning` | A large gap between actual and "feels like" (wind chill / humidity) is actionable for anyone outdoors, and is independent of the raw temperature. |

Every event is stored with the eight fields defined in
[`.cursor/rules/event_schema.mdc`](.cursor/rules/event_schema.mdc):
`city, event_type, severity, description, timestamp, triggered_at, reading_id,
details`. The `description` always names the city and the measured values, and
`details` is a JSON blob carrying the exact numbers used in the decision — so
every event can answer **what happened, where, when, and why**.

> **Note on "0 events":** on a calm day, all readings sit well inside every
> threshold and the system correctly fires nothing. Events appear during actual
> weather movement (a gust front, a morning warm-up, a thunderstorm). This is
> the selective-by-design behavior, not a bug.

---

## Project Structure

```
watchagent/
├── app/
│   ├── database.py     # schema + init_db() + get_db() (DB_PATH from env)
│   ├── events.py       # CITY_CONFIG, SEVERE_CODES, detect_events()
│   ├── poller.py       # CITIES, poll_city(), poll_all_cities()
│   ├── scheduler.py    # APScheduler interval job
│   └── main.py         # FastAPI app, lifespan, 3 endpoints
├── tests/
│   ├── test_deduplication.py
│   ├── test_event_detection.py
│   └── test_api.py
├── .cursor/
│   ├── rules/
│   │   ├── polling_conventions.mdc
│   │   └── event_schema.mdc
│   ├── agents/
│   │   └── event_detection_reviewer.md
│   └── skills/
│       └── data_analysis.py
├── .github/workflows/ci.yml
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

---

## CI Pipeline

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push to
`main` with two jobs:

- **test** — installs dependencies and runs `pytest tests/ -v`.
- **build** — runs `docker build .` to prove the image builds with no API keys.

---

## Cursor Setup

The `.cursor/` folder encodes this project's real conventions so that any
AI-assisted change stays consistent with decisions already made in the codebase.

### Rules (`.cursor/rules/`)

**`polling_conventions.mdc`** — scoped to `app/poller.py` and
`app/scheduler.py`. Encodes the concrete operational contract for fetching:

- On any fetch failure, log at **WARNING** with `city=`, `status=`, `attempt=`
  fields and **return `None` rather than raising** (the scheduler retries next
  interval).
- All reading inserts **must** use `INSERT OR IGNORE` against the
  `(city, timestamp)` UNIQUE constraint — the DB is the source of truth for
  dedup, never a pre-check. New vs duplicate is decided by `cursor.rowcount`.
- Logging levels are fixed: new reading → INFO, duplicate skip → DEBUG (it
  happens every poll), fetch failure → WARNING, event fired → INFO with
  `event_type=` and `city=`.

This is a *real* rule: it dictates error handling, the dedup mechanism, and the
exact logging contract — not "write clean code."

**`event_schema.mdc`** — scoped to `app/events.py` and the event tests. Fixes
the event record contract: the exact eight keys every event dict must carry, the
allowed `event_type` and `severity` values, the requirement that `description`
include the city and triggering values, that `details` be a JSON string of the
numeric values used (for auditability), that all thresholds live in
`CITY_CONFIG` (never hardcoded in detectors), and the anti-spam rule for events
that persist across readings.

### Agent (`.cursor/agents/`)

**`event_detection_reviewer.md`** — a reviewer agent scoped *only* to
`app/events.py` and its tests. Its system prompt carries real project context
(the three cities, the polling cadence, the reading fields, the
`CITY_CONFIG`/JSON-details conventions) and a defined checklist: does an event
fire too often or too rarely for Canadian weather, is the logic city-aware
(Ottawa winters vs Vancouver), is hysteresis missing, does `details` carry the
triggering numbers, and is the severity actionable. It is explicitly forbidden
from touching API routes or the DB schema, keeping its boundary tight.

### Skill (`.cursor/skills/data_analysis.py`)

A runnable, graded deliverable. Given a natural-language `--question`, it queries
the live SQLite database and returns structured JSON analysis spanning the full
dataset:

```bash
# (point DB_PATH at whatever DB you want to analyze)
DB_PATH=./watchagent.db python .cursor/skills/data_analysis.py --question "temperature trend Ottawa"
DB_PATH=./watchagent.db python .cursor/skills/data_analysis.py --question "which city had most events"
DB_PATH=./watchagent.db python .cursor/skills/data_analysis.py --question "compare wind speeds"
```

It supports three analyses:

- **temperature trend `<city>`** — min/max/avg, latest vs oldest, and a
  warming/cooling verdict over the recent window.
- **events by city** — per-city event counts grouped by type and severity,
  sorted to surface the busiest city.
- **cross-city wind comparison** — average and max wind per city.

Example output for `temperature trend Ottawa`:

```json
{
  "city": "Ottawa",
  "readings_analyzed": 24,
  "min_temp": 12.0,
  "max_temp": 23.5,
  "avg_temp": 17.75,
  "latest": 23.5,
  "oldest": 12.0,
  "trend": "warming"
}
```
