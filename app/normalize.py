"""Turn the portal's raw, stringly-typed payloads into clean, typed records.

Portal quirks handled here (see PROTOCOL.md for the full list):
- Numbers arrive as strings ("48438.74"); missing values are "" rather than null.
- Timestamps are "DD/MM/YYYY HH:MM" with no zone; we treat them as IST (+05:30).
- The same timestamp can appear twice, once complete and once with blanks.
- kWh / kVAh are cumulative register reads, not interval consumption.
- Meter detail comes in two shapes: legacy ``data: [{parameterName, parameterValue}]``
  and v2 ``classData: "<JSON string>"``.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterable

IST = timezone(timedelta(hours=5, minutes=30))

STATUS_MAP = {"installed": "installed", "faulty": "faulty", "decommissioned": "decommissioned"}
INSTALL_TYPE_MAP = {"whole current": "whole_current", "ct operated": "ct_operated"}
PHASE_MAP = {"single": "single", "three": "three", "1": "single", "3": "three"}

# Nominal LV supply in India is 230 V; ±10 % is the usual statutory band.
VOLTAGE_LOW, VOLTAGE_HIGH = 207.0, 253.0


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _enum(value: Any, mapping: dict[str, str]) -> str | None:
    if value is None:
        return None
    key = str(value).strip().lower()
    return mapping.get(key, key.replace(" ", "_") or None)


def parse_portal_timestamp(text: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip(), "%d/%m/%Y %H:%M").replace(tzinfo=IST)
    except (AttributeError, ValueError):
        return None


_LABELLED = re.compile(r"^(?P<name>.*?)\s*\((?P<code>[^()]*)\)\s*$")


def parse_labelled_node(text: Any) -> dict[str, str]:
    """'Feeder 1 (F-001)' -> {'name': 'Feeder 1', 'code': 'F-001'} (as the detail page renders it)."""
    if not text:
        return {"name": "", "code": ""}
    match = _LABELLED.match(str(text))
    if not match:
        return {"name": str(text).strip(), "code": ""}
    return {"name": match["name"].strip(), "code": match["code"].strip()}


_DETAIL_KEYS = {
    "meter id": "meterId",
    "meterid": "meterId",
    "serial no": "serialNo",
    "serialno": "serialNo",
    "make": "make",
    "phase type": "phaseType",
    "phasetype": "phaseType",
    "installation status": "installStatus",
    "installationstatus": "installStatus",
    "installation type": "installType",
    "installationtype": "installType",
}


def parse_detail_fields(detail: dict[str, Any]) -> tuple[dict[str, str], str]:
    """Normalise both detail-page shapes into one dict. Returns (fields, build)."""
    pairs: Iterable[tuple[str, Any]]
    if "classData" in detail:
        build = "v2"
        try:
            pairs = (json.loads(detail["classData"]).get("installed_meter") or {}).items()
        except (TypeError, ValueError):
            pairs = []
    else:
        build = "legacy"
        pairs = ((p.get("parameterName", ""), p.get("parameterValue")) for p in detail.get("data") or [])
    fields = {}
    for key, value in pairs:
        canonical = _DETAIL_KEYS.get(str(key).strip().lower())
        if canonical:
            fields[canonical] = value
    return fields, build


# --------------------------------------------------------------------- meters


def clean_meter(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape an export row into our public meter record (hierarchy is filled in later)."""
    geo = raw.get("geo") or {}
    lat, lng = to_float(geo.get("lat")), to_float(geo.get("lng"))
    return {
        "meter_id": str(raw.get("meterId", "")).strip().upper(),
        "serial_no": (raw.get("serialNo") or "").strip() or None,
        "make": (raw.get("make") or "").strip() or None,
        "phase": _enum(raw.get("phaseType"), PHASE_MAP),
        "status": _enum(raw.get("installStatus"), STATUS_MAP),
        "install_type": _enum(raw.get("installType"), INSTALL_TYPE_MAP),
        "record_format": raw.get("build"),
        "transformer_code": (raw.get("dtCode") or "").strip() or None,
        "location": {"lat": lat, "lng": lng} if lat is not None and lng is not None else None,
    }


def clean_transformer(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": raw.get("code"),
        "name": raw.get("name"),
        "feeder_code": raw.get("feederCode"),
        "capacity_kva": to_float(raw.get("capacityKva")),
    }


# ------------------------------------------------------------------- readings


def clean_readings(raw_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Parse, de-duplicate and sort readings. Returns (rows, quality counters)."""
    quality = {"duplicates_merged": 0, "unparseable_timestamps": 0, "blank_values": 0}
    by_ts: dict[datetime, dict[str, Any]] = {}
    for raw in raw_rows:
        ts = parse_portal_timestamp(raw.get("timestamp", ""))
        if ts is None:
            quality["unparseable_timestamps"] += 1
            continue
        row = {
            "ts": ts,
            "kwh": to_float(raw.get("kwh")),
            "kvah": to_float(raw.get("kvah")),
            "voltage_r": to_float(raw.get("voltR")),
        }
        quality["blank_values"] += sum(1 for k in ("kwh", "kvah", "voltage_r") if row[k] is None)
        existing = by_ts.get(ts)
        if existing is None:
            by_ts[ts] = row
        else:
            # Duplicate timestamp: keep the most complete values from either copy.
            quality["duplicates_merged"] += 1
            for key in ("kwh", "kvah", "voltage_r"):
                if existing[key] is None:
                    existing[key] = row[key]
    return [by_ts[t] for t in sorted(by_ts)], quality


def cadence_minutes(rows: list[dict[str, Any]]) -> int | None:
    steps = [(b["ts"] - a["ts"]).total_seconds() / 60 for a, b in zip(rows, rows[1:])]
    return int(median(steps)) if steps else None


def with_deltas(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach interval consumption: delta of the cumulative register since the previous read.

    The delta on a row covers [previous ts, this ts). A negative delta (register
    rollback / meter swap) is reported as null and flagged rather than silently used.
    """
    out = []
    prev_kwh = prev_kvah = None
    prev_ts = None
    for row in rows:
        kwh_d = kvah_d = None
        flags: list[str] = []
        if prev_kwh is not None and row["kwh"] is not None:
            kwh_d = round(row["kwh"] - prev_kwh, 4)
            if kwh_d < 0:
                flags.append("register_decreased")
                kwh_d = None
        if prev_kvah is not None and row["kvah"] is not None:
            kvah_d = round(row["kvah"] - prev_kvah, 4)
            if kvah_d < 0:
                kvah_d = None
        v = row["voltage_r"]
        if v is not None and not VOLTAGE_LOW <= v <= VOLTAGE_HIGH:
            flags.append("voltage_out_of_band")
        if row["kwh"] is None:
            flags.append("missing_kwh")
        out.append({**row, "interval_start": prev_ts, "kwh_delta": kwh_d, "kvah_delta": kvah_d, "flags": flags})
        if row["kwh"] is not None:
            prev_kwh = row["kwh"]
        if row["kvah"] is not None:
            prev_kvah = row["kvah"]
        prev_ts = row["ts"]
    return out


def daily_rollup(rows: list[dict[str, Any]], cadence: int | None) -> dict[date, dict[str, Any]]:
    """Aggregate interval deltas into calendar days (IST), keyed by interval start."""
    expected = max(1, round(1440 / cadence)) if cadence else None
    days: dict[date, dict[str, Any]] = defaultdict(
        lambda: {"kwh": 0.0, "kvah": 0.0, "intervals": 0, "v_min": None, "v_max": None, "v_sum": 0.0, "v_n": 0}
    )
    for row in rows:
        if row["interval_start"] is None or row["kwh_delta"] is None:
            continue
        d = days[row["interval_start"].date()]
        d["kwh"] += row["kwh_delta"]
        d["kvah"] += row["kvah_delta"] or 0.0
        d["intervals"] += 1
        v = row["voltage_r"]
        if v is not None:
            d["v_min"] = v if d["v_min"] is None else min(d["v_min"], v)
            d["v_max"] = v if d["v_max"] is None else max(d["v_max"], v)
            d["v_sum"] += v
            d["v_n"] += 1
    result = {}
    for day, d in sorted(days.items()):
        result[day] = {
            "date": day.isoformat(),
            "kwh": round(d["kwh"], 3),
            "kvah": round(d["kvah"], 3),
            "power_factor": round(d["kwh"] / d["kvah"], 3) if d["kvah"] else None,
            "intervals": d["intervals"],
            "completeness": round(min(1.0, d["intervals"] / expected), 3) if expected else None,
            "voltage_min": d["v_min"],
            "voltage_max": d["v_max"],
            "voltage_avg": round(d["v_sum"] / d["v_n"], 1) if d["v_n"] else None,
        }
    return result
