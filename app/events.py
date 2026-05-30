import json
import logging
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)


# City-specific thresholds. Never hardcode these inside detection functions —
# always read from CITY_CONFIG[city] (see .cursor/rules/event_schema.mdc).
CITY_CONFIG = {
    "Ottawa": {
        "wind_warning": 40.0,   # km/h sustained wind to flag high_wind
        "temp_spike": 8.0,      # °C hour-over-hour delta to flag a spike/plunge
        "cold_snap": -25.0,     # °C absolute floor that escalates a plunge to critical
        "heat_alert": 32.0,     # °C absolute ceiling that escalates a spike to critical
    },
    "Toronto": {
        "wind_warning": 45.0,
        "temp_spike": 7.0,
        "cold_snap": -18.0,
        "heat_alert": 33.0,
    },
    "Vancouver": {
        "wind_warning": 50.0,
        "temp_spike": 6.0,
        "cold_snap": -5.0,
        "heat_alert": 28.0,
    },
}

# WMO weather codes that warrant a severe_weather_code event, mapped to a
# human-readable condition name.
SEVERE_CODES = {
    71: "slight snowfall",
    73: "moderate snowfall",
    75: "heavy snowfall",
    77: "snow grains",
    82: "violent rain showers",
    85: "slight snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}

# Severity assigned per severe weather code.
_CODE_SEVERITY = {
    71: "info",
    73: "warning",
    75: "critical",
    77: "info",
    82: "warning",
    85: "info",
    95: "critical",
    96: "critical",
    99: "critical",
}

# Precipitation below this (mm) is treated as a dry reading.
DRY_THRESHOLD = 0.1

# Number of consecutive dry readings required before precipitation_onset fires.
DRY_RUN_REQUIRED = 3

# apparent vs actual temperature gap (°C) that flags feels_like_divergence.
FEELS_LIKE_GAP = 10.0


def _make_event(city, event_type, severity, description, timestamp, reading_id, details):
    return {
        "city": city,
        "event_type": event_type,
        "severity": severity,
        "description": description,
        "timestamp": timestamp,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
        "reading_id": reading_id,
        "details": json.dumps(details),
    }


async def _previous_reading(db, city, timestamp):
    cur = await db.execute(
        "SELECT * FROM readings WHERE city = ? AND timestamp < ? "
        "ORDER BY timestamp DESC LIMIT 1",
        (city, timestamp),
    )
    return await cur.fetchone()


async def _recent_precip(db, city, timestamp, limit):
    cur = await db.execute(
        "SELECT precipitation FROM readings WHERE city = ? AND timestamp < ? "
        "ORDER BY timestamp DESC LIMIT ?",
        (city, timestamp, limit),
    )
    return await cur.fetchall()


async def _last_wind_event_ts(db, city):
    cur = await db.execute(
        "SELECT timestamp FROM events WHERE city = ? AND event_type = 'high_wind' "
        "ORDER BY timestamp DESC LIMIT 1",
        (city,),
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def _has_intervening_calm(db, city, since_ts, before_ts, threshold):
    cur = await db.execute(
        "SELECT COUNT(*) FROM readings WHERE city = ? AND timestamp > ? AND timestamp < ? "
        "AND wind_speed_10m IS NOT NULL AND wind_speed_10m < ?",
        (city, since_ts, before_ts, threshold),
    )
    row = await cur.fetchone()
    return (row[0] if row else 0) > 0


def _temperature_event(config, city, reading, prev, timestamp, reading_id):
    temp = reading.get("temperature_2m")
    prev_temp = prev["temperature_2m"] if prev is not None else None
    if temp is None or prev_temp is None:
        return None

    delta = temp - prev_temp
    if abs(delta) < config["temp_spike"]:
        return None

    if delta > 0:
        event_type = "temperature_spike"
        severity = "critical" if temp >= config["heat_alert"] else "warning"
    else:
        event_type = "temperature_plunge"
        severity = "critical" if temp <= config["cold_snap"] else "warning"

    description = (
        f"{city}: temperature {event_type.split('_')[1]} of {delta:+.1f}°C "
        f"({prev_temp:.1f}°C → {temp:.1f}°C) exceeded the {config['temp_spike']:.1f}°C threshold"
    )
    details = {
        "temperature_2m": temp,
        "previous_temperature_2m": prev_temp,
        "delta": round(delta, 2),
        "threshold": config["temp_spike"],
        "cold_snap": config["cold_snap"],
        "heat_alert": config["heat_alert"],
    }
    return _make_event(city, event_type, severity, description, timestamp, reading_id, details)


def _precipitation_event(city, reading, recent, timestamp, reading_id):
    precip = reading.get("precipitation")
    if precip is None or precip < DRY_THRESHOLD:
        return None
    if len(recent) < DRY_RUN_REQUIRED:
        return None
    if not all(r[0] is not None and r[0] < DRY_THRESHOLD for r in recent):
        return None

    description = (
        f"{city}: precipitation onset of {precip:.1f}mm after "
        f"{DRY_RUN_REQUIRED}+ consecutive dry readings"
    )
    details = {
        "precipitation": precip,
        "dry_threshold": DRY_THRESHOLD,
        "consecutive_dry_readings": len(recent),
    }
    return _make_event(city, "precipitation_onset", "info", description, timestamp, reading_id, details)


def _wind_event(config, city, reading, timestamp, reading_id):
    wind = reading.get("wind_speed_10m")
    if wind is None or wind < config["wind_warning"]:
        return None
    severity = "critical" if wind >= config["wind_warning"] * 1.5 else "warning"
    description = (
        f"{city}: high wind of {wind:.1f} km/h exceeded the "
        f"{config['wind_warning']:.1f} km/h threshold"
    )
    details = {
        "wind_speed_10m": wind,
        "threshold": config["wind_warning"],
    }
    return _make_event(city, "high_wind", severity, description, timestamp, reading_id, details)


def _severe_code_event(city, reading, timestamp, reading_id):
    code = reading.get("weather_code")
    if code is None or code not in SEVERE_CODES:
        return None
    condition = SEVERE_CODES[code]
    severity = _CODE_SEVERITY.get(code, "warning")
    description = f"{city}: severe weather reported — {condition} (WMO code {code})"
    details = {
        "weather_code": code,
        "condition": condition,
    }
    return _make_event(city, "severe_weather_code", severity, description, timestamp, reading_id, details)


def _feels_like_event(city, reading, timestamp, reading_id):
    temp = reading.get("temperature_2m")
    apparent = reading.get("apparent_temperature")
    if temp is None or apparent is None:
        return None
    gap = apparent - temp
    if abs(gap) < FEELS_LIKE_GAP:
        return None
    severity = "warning"
    direction = "colder" if gap < 0 else "warmer"
    description = (
        f"{city}: feels-like {apparent:.1f}°C diverges {abs(gap):.1f}°C {direction} "
        f"than actual {temp:.1f}°C"
    )
    details = {
        "temperature_2m": temp,
        "apparent_temperature": apparent,
        "divergence": round(gap, 2),
        "threshold": FEELS_LIKE_GAP,
    }
    return _make_event(city, "feels_like_divergence", severity, description, timestamp, reading_id, details)


async def detect_events(db, city, reading, timestamp, reading_id):
    """Detect weather events for a single reading and persist them.

    Returns the list of event dicts that were inserted.
    """
    config = CITY_CONFIG.get(city)
    if config is None:
        logger.warning("No CITY_CONFIG for city=%s — skipping event detection", city)
        return []

    db.row_factory = aiosqlite.Row

    events = []

    prev = await _previous_reading(db, city, timestamp)
    temp_event = _temperature_event(config, city, reading, prev, timestamp, reading_id)
    if temp_event:
        events.append(temp_event)

    recent = await _recent_precip(db, city, timestamp, DRY_RUN_REQUIRED)
    precip_event = _precipitation_event(city, reading, recent, timestamp, reading_id)
    if precip_event:
        events.append(precip_event)

    wind = reading.get("wind_speed_10m")
    if wind is not None and wind >= config["wind_warning"]:
        last_ts = await _last_wind_event_ts(db, city)
        # Hysteresis: only fire if there has been a calm reading since the last
        # high_wind event (or if this is the first one for the city).
        if last_ts is None or await _has_intervening_calm(
            db, city, last_ts, timestamp, config["wind_warning"]
        ):
            wind_event = _wind_event(config, city, reading, timestamp, reading_id)
            if wind_event:
                events.append(wind_event)

    severe_event = _severe_code_event(city, reading, timestamp, reading_id)
    if severe_event:
        events.append(severe_event)

    feels_event = _feels_like_event(city, reading, timestamp, reading_id)
    if feels_event:
        events.append(feels_event)

    for event in events:
        await db.execute(
            "INSERT INTO events "
            "(city, event_type, severity, description, timestamp, triggered_at, reading_id, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event["city"],
                event["event_type"],
                event["severity"],
                event["description"],
                event["timestamp"],
                event["triggered_at"],
                event["reading_id"],
                event["details"],
            ),
        )
        logger.info("Event fired event_type=%s city=%s", event["event_type"], city)

    if events:
        await db.commit()

    return events
