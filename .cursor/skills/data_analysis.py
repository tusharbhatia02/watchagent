#!/usr/bin/env python3
"""
WatchAgent Data Analysis Skill
Usage: python .cursor/skills/data_analysis.py --question "..."

Questions it can answer:
  - "temperature trend Ottawa last 24 hours"
  - "which city had the most events"
  - "compare wind speeds across cities"
  - "events by type summary"
"""

import sqlite3
import argparse
import json
import os
from collections import defaultdict

DB_PATH = os.getenv("DB_PATH", "/data/watchagent.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def temperature_trend(city: str, hours: int = 24):
    conn = get_db()
    rows = conn.execute("""
        SELECT timestamp, temperature_2m, apparent_temperature
        FROM readings WHERE city=?
        ORDER BY timestamp DESC LIMIT ?
    """, (city, hours)).fetchall()
    if not rows:
        return {"error": f"No data for {city}"}
    temps = [r["temperature_2m"] for r in rows]
    return {
        "city": city, "readings_analyzed": len(rows),
        "min_temp": min(temps), "max_temp": max(temps),
        "avg_temp": round(sum(temps)/len(temps), 2),
        "latest": rows[0]["temperature_2m"], "oldest": rows[-1]["temperature_2m"],
        "trend": "warming" if rows[0]["temperature_2m"] > rows[-1]["temperature_2m"] else "cooling"
    }

def events_by_city():
    conn = get_db()
    rows = conn.execute("""
        SELECT city, event_type, severity, COUNT(*) as count
        FROM events GROUP BY city, event_type, severity ORDER BY count DESC
    """).fetchall()
    result = defaultdict(list)
    for r in rows:
        result[r["city"]].append({"event_type": r["event_type"], "severity": r["severity"], "count": r["count"]})
    return dict(result)

def cross_city_wind_comparison():
    conn = get_db()
    result = {}
    for city in ["Ottawa", "Toronto", "Vancouver"]:
        rows = conn.execute("""
            SELECT AVG(wind_speed_10m) as avg, MAX(wind_speed_10m) as max
            FROM readings WHERE city=?
        """, (city,)).fetchone()
        result[city] = {"avg_wind_kmh": round(rows["avg"] or 0, 2), "max_wind_kmh": rows["max"] or 0}
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", required=True)
    args = parser.parse_args()
    q = args.question.lower()

    if "trend" in q:
        city = next((c for c in ["Ottawa", "Toronto", "Vancouver"] if c.lower() in q), "Ottawa")
        print(json.dumps(temperature_trend(city), indent=2))
    elif "event" in q and ("city" in q or "most" in q or "summary" in q):
        print(json.dumps(events_by_city(), indent=2))
    elif "wind" in q:
        print(json.dumps(cross_city_wind_comparison(), indent=2))
    else:
        print(json.dumps({"available_queries": [
            "temperature trend <city>",
            "which city had most events",
            "compare wind speeds"
        ]}))

if __name__ == "__main__":
    main()
