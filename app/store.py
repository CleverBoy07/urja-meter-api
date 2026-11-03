"""Local SQLite index: the query layer the portal doesn't have.

The portal can only search by meter-id/serial substring and page 20 rows at a time.
Mirroring the (small) inventory locally lets us filter on any attribute, query by
location and aggregate across the network in milliseconds, and keeps serving when
the portal is slow or down.

Readings are cached per meter with a per-day coverage table, so a range request only
goes to the portal for the days we don't already hold (or that have gone stale).
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS meters (
    meter_id TEXT PRIMARY KEY,
    serial_no TEXT, make TEXT, phase TEXT, status TEXT, install_type TEXT,
    record_format TEXT, transformer_code TEXT, feeder_code TEXT,
    substation_code TEXT, subdivision_code TEXT, division_code TEXT,
    circle_code TEXT, zone_code TEXT, network_path TEXT,
    lat REAL, lng REAL,
    doc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_meters_status ON meters(status);
CREATE INDEX IF NOT EXISTS ix_meters_dt ON meters(transformer_code);
CREATE INDEX IF NOT EXISTS ix_meters_path ON meters(network_path);
CREATE INDEX IF NOT EXISTS ix_meters_latlng ON meters(lat, lng);

CREATE TABLE IF NOT EXISTS transformers (
    code TEXT PRIMARY KEY, doc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    meter_id TEXT NOT NULL, ts TEXT NOT NULL,
    kwh REAL, kvah REAL, voltage_r REAL,
    PRIMARY KEY (meter_id, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS reading_days (
    meter_id TEXT NOT NULL, day TEXT NOT NULL, fetched_at REAL NOT NULL,
    PRIMARY KEY (meter_id, day)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

FILTER_COLUMNS = {
    "status": "status",
    "make": "make",
    "phase": "phase",
    "install_type": "install_type",
    "record_format": "record_format",
    "transformer": "transformer_code",
    "feeder": "feeder_code",
    "substation": "substation_code",
    "subdivision": "subdivision_code",
    "division": "division_code",
    "circle": "circle_code",
    "zone": "zone_code",
}
SORTABLE = {"meter_id", "serial_no", "make", "status", "transformer_code", "phase"}

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        self._db.close()

    def _all(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        """Run a read under the lock. The connection is shared by FastAPI's worker
        threads, and sqlite3 connections are not safe for concurrent use."""
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _one(self, sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    # ----------------------------------------------------------------- meta

    def get_meta(self, key: str) -> Any:
        row = self._one("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(row["value"]) if row else None

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, json.dumps(value, default=str)))

    # ------------------------------------------------------------ inventory

    def replace_inventory(self, meters: list[dict[str, Any]], transformers: list[dict[str, Any]]) -> None:
        """Atomically swap in a fresh inventory snapshot."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.execute("DELETE FROM meters")
                self._db.execute("DELETE FROM transformers")
                self._db.executemany(
                    "INSERT INTO meters VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [self._meter_row(m) for m in meters],
                )
                self._db.executemany(
                    "INSERT INTO transformers VALUES (?, ?)", [(t["code"], json.dumps(t)) for t in transformers]
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    @staticmethod
    def _meter_row(m: dict[str, Any]) -> tuple:
        n = m["network"]
        loc = m.get("location") or {}
        return (
            m["meter_id"], m["serial_no"], m["make"], m["phase"], m["status"], m["install_type"],
            m["record_format"], n["transformer"]["code"], n["feeder"]["code"], n["substation"]["code"],
            n["subdivision"]["code"], n["division"]["code"], n["circle"]["code"], n["zone"]["code"],
            m["network_path"], loc.get("lat"), loc.get("lng"), json.dumps(m),
        )

    def meter_count(self) -> int:
        return self._one("SELECT COUNT(*) FROM meters")[0]

    def get_meter(self, meter_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT doc FROM meters WHERE meter_id=?", (meter_id.upper(),))
        return json.loads(row["doc"]) if row else None

    def all_meters(self) -> list[dict[str, Any]]:
        return [json.loads(r["doc"]) for r in self._all("SELECT doc FROM meters ORDER BY meter_id")]

    def query_meters(
        self,
        *,
        q: str | None = None,
        filters: dict[str, list[str]] | None = None,
        network_path_prefix: str | None = None,
        near: tuple[float, float] | None = None,
        radius_km: float | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        sort: str = "meter_id",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where, args = [], []
        if q:
            where.append("(meter_id LIKE ? OR serial_no LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        for key, values in (filters or {}).items():
            if values:
                where.append(f"{FILTER_COLUMNS[key]} IN ({','.join('?' * len(values))})")
                args += values
        if network_path_prefix:
            where.append("(network_path = ? OR network_path LIKE ?)")
            args += [network_path_prefix, network_path_prefix.rstrip("/") + "/%"]
        if bbox:
            south, west, north, east = bbox
            where.append("lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?")
            args += [south, north, west, east]
        if near and radius_km is not None:
            # Cheap bounding-box pre-filter on the (lat, lng) index; exact distance below.
            lat, lng = near
            dlat = radius_km / 111.32
            dlng = radius_km / max(1e-6, 111.32 * math.cos(math.radians(lat)))
            where.append("lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?")
            args += [lat - dlat, lat + dlat, lng - dlng, lng + dlng]

        sql = "SELECT doc, lat, lng FROM meters"
        if where:
            sql += " WHERE " + " AND ".join(where)

        descending = sort.startswith("-")
        sort_key = sort.lstrip("-")
        if near:
            rows = self._all(sql, args)
            items = []
            for r in rows:
                if r["lat"] is None:
                    continue
                dist = haversine_km(near[0], near[1], r["lat"], r["lng"])
                if radius_km is None or dist <= radius_km:
                    doc = json.loads(r["doc"])
                    doc["distance_km"] = round(dist, 3)
                    items.append(doc)
            items.sort(key=lambda d: d["distance_km"], reverse=descending and sort_key == "distance")
            return items[offset : offset + limit], len(items)

        if sort_key not in SORTABLE:
            sort_key = "meter_id"
        total = self._one(f"SELECT COUNT(*) FROM ({sql})", args)[0]
        sql += f" ORDER BY {sort_key} {'DESC' if descending else 'ASC'}, meter_id LIMIT ? OFFSET ?"
        rows = self._all(sql, [*args, limit, offset])
        return [json.loads(r["doc"]) for r in rows], total

    def facets(self) -> dict[str, dict[str, int]]:
        out = {}
        for key in ["status", "make", "phase", "install_type", "record_format", "zone_code"]:
            rows = self._all(f"SELECT {key} AS k, COUNT(*) AS n FROM meters GROUP BY {key} ORDER BY n DESC")
            out[key.removesuffix("_code")] = {str(r["k"]): r["n"] for r in rows}
        return out

    def transformers(self) -> list[dict[str, Any]]:
        return [json.loads(r["doc"]) for r in self._all("SELECT doc FROM transformers ORDER BY code")]

    def get_transformer(self, code: str) -> dict[str, Any] | None:
        row = self._one("SELECT doc FROM transformers WHERE code=?", (code.upper(),))
        return json.loads(row["doc"]) if row else None

    def meters_on_transformer(self, code: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT doc FROM meters WHERE transformer_code=? ORDER BY meter_id", (code.upper(),))
        return [json.loads(r["doc"]) for r in rows]

    # ------------------------------------------------------------- readings

    def upsert_readings(self, meter_id: str, rows: Iterable[dict[str, Any]], days: Iterable[date], fetched_at: float | None = None) -> None:
        fetched_at = fetched_at or time.time()
        with self._lock:
            self._db.execute("BEGIN")
            try:
                self._db.executemany(
                    "INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?)",
                    [(meter_id, r["ts"].isoformat(), r["kwh"], r["kvah"], r["voltage_r"]) for r in rows],
                )
                self._db.executemany(
                    "INSERT OR REPLACE INTO reading_days VALUES (?,?,?)",
                    [(meter_id, d.isoformat(), fetched_at) for d in days],
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def coverage(self, meter_id: str, start: date, end: date) -> dict[date, float]:
        rows = self._all(
            "SELECT day, fetched_at FROM reading_days WHERE meter_id=? AND day BETWEEN ? AND ?",
            (meter_id, start.isoformat(), end.isoformat()),
        )
        return {date.fromisoformat(r["day"]): r["fetched_at"] for r in rows}

    def readings(self, meter_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT ts, kwh, kvah, voltage_r FROM readings WHERE meter_id=? AND ts >= ? AND ts < ? ORDER BY ts",
            (meter_id, start.isoformat(), end.isoformat()),
        )
        return [
            {"ts": datetime.fromisoformat(r["ts"]), "kwh": r["kwh"], "kvah": r["kvah"], "voltage_r": r["voltage_r"]}
            for r in rows
        ]

    def latest_reading_ts(self, meter_id: str | None = None) -> datetime | None:
        if meter_id:
            row = self._one("SELECT MAX(ts) FROM readings WHERE meter_id=?", (meter_id,))
        else:
            row = self._one("SELECT MAX(ts) FROM readings")
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def meters_with_readings(self) -> set[str]:
        return {r[0] for r in self._all("SELECT DISTINCT meter_id FROM reading_days")}
