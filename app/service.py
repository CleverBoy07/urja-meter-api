"""Orchestration: keeps the local index in sync with the portal and answers queries.

Freshness model
---------------
* Inventory (meters, hierarchy, geo, transformers) changes rarely and comes from one
  signed bulk call, so we refresh it every ``inventory_ttl_s`` in the background and
  serve the last good snapshot in between (stale-while-revalidate). If a refresh fails
  we keep serving the previous snapshot and say so in ``meta``.
* Readings are cached per meter per *day*. A day is "closed" once it is more than a day
  older than the newest reading we have seen for that meter: closed days are kept for
  ``readings_closed_ttl_s``; open days (today, yesterday, and anything after the last
  known reading) are refetched after ``readings_recent_ttl_s``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Any

from app.config import Settings
from app.hierarchy import LEVELS, build_network, build_tree
from app.normalize import (
    IST,
    VOLTAGE_HIGH,
    VOLTAGE_LOW,
    cadence_minutes,
    clean_meter,
    clean_readings,
    clean_transformer,
    daily_rollup,
    with_deltas,
)
from app.portal.client import PortalClient, PortalError
from app.store import Store

log = logging.getLogger("urja.service")

DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 366


class NotReady(Exception):
    """Inventory has never been loaded (portal unreachable since start-up)."""


class Service:
    def __init__(self, settings: Settings, portal: PortalClient, store: Store) -> None:
        self.settings = settings
        self.portal = portal
        self.store = store
        self._sync_lock = asyncio.Lock()
        self._meter_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: list[asyncio.Task] = []
        self.quality_report: dict[str, Any] = store.get_meta("quality_report") or {}
        self.warm_state: dict[str, Any] = {"enabled": settings.warm_readings, "running": False, "done": 0, "total": 0}

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.settings.sync_on_startup and self.portal.has_credentials:
            self._tasks.append(asyncio.create_task(self._inventory_loop(), name="inventory-sync"))
            if self.settings.warm_readings:
                self._tasks.append(asyncio.create_task(self._warm_loop(), name="readings-warm"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _inventory_loop(self) -> None:
        while True:
            try:
                if self.inventory_age_s() is None or self.inventory_age_s() >= self.settings.inventory_ttl_s:
                    await self.sync_inventory()
            except Exception as exc:  # keep the loop alive; error is surfaced via /health
                log.warning("Inventory sync failed: %s", exc)
                await asyncio.sleep(30)
                continue
            await asyncio.sleep(max(30, self.settings.inventory_ttl_s - (self.inventory_age_s() or 0)))

    # ------------------------------------------------------------ inventory

    def inventory_age_s(self) -> float | None:
        synced = self.store.get_meta("inventory_synced_at")
        return None if synced is None else time.time() - synced

    def inventory_meta(self) -> dict[str, Any]:
        synced = self.store.get_meta("inventory_synced_at")
        age = self.inventory_age_s()
        return {
            "synced_at": datetime.fromtimestamp(synced, IST).isoformat() if synced else None,
            "age_s": round(age, 1) if age is not None else None,
            "stale": age is not None and age > self.settings.inventory_ttl_s * 2,
            "source": self.store.get_meta("inventory_source"),
            "last_sync_error": self.store.get_meta("last_sync_error"),
        }

    def require_inventory(self) -> None:
        if self.store.get_meta("inventory_synced_at") is None:
            raise NotReady("Inventory has not been loaded from the portal yet")

    async def sync_inventory(self, *, allow_crawl: bool = True) -> dict[str, Any]:
        async with self._sync_lock:
            started = time.time()
            source = "export"
            try:
                raw_meters = await self.portal.export_meters()
            except PortalError as exc:
                if not allow_crawl or self.store.meter_count():
                    self.store.set_meta("last_sync_error", str(exc))
                    raise
                log.warning("Bulk export failed (%s); falling back to page crawl", exc)
                raw_meters = await self.portal.crawl_meters()
                source = "crawl"
            raw_dts = await self.portal.list_transformers()

            transformers = [clean_transformer(t) for t in raw_dts]
            networks, report = build_network(raw_meters, raw_dts)
            issues_by_meter: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for issue in report["issues"]:
                if issue["meter_id"]:
                    issues_by_meter[issue["meter_id"]].append({k: issue[k] for k in ("type", "level", "severity", "message")})

            meters = []
            for raw in raw_meters:
                m = clean_meter(raw)
                net = networks[m["meter_id"]]
                m["network"] = net["nodes"]
                m["network_path"] = net["path"]
                m["transformer_code"] = net["nodes"]["transformer"]["code"]
                m["data_quality"] = issues_by_meter.get(m["meter_id"], [])
                meters.append(m)

            meter_counts = Counter(m["transformer_code"] for m in meters)
            for t in transformers:
                t["meter_count"] = meter_counts.get(t["code"], 0)
                aliases = sorted({m["network"]["transformer"].get("alias") for m in meters if m["transformer_code"] == t["code"]} - {None})
                t["aliases"] = aliases

            self.store.replace_inventory(meters, transformers)
            report["generated_at"] = datetime.now(IST).isoformat()
            self.quality_report = report
            self.store.set_meta("quality_report", report)
            self.store.set_meta("inventory_synced_at", time.time())
            self.store.set_meta("inventory_source", source)
            self.store.set_meta("last_sync_error", None)
            summary = {"meters": len(meters), "transformers": len(transformers), "source": source, "duration_s": round(time.time() - started, 2)}
            log.info("Inventory synced: %s", summary)
            return summary

    # ------------------------------------------------------------- readings

    def _is_closed(self, day: date, latest: date | None) -> bool:
        return latest is not None and day < latest - timedelta(days=1)

    def _stale_days(self, meter_id: str, start: date, end: date) -> list[date]:
        latest_ts = self.store.latest_reading_ts(meter_id)
        latest = latest_ts.date() if latest_ts else None
        covered = self.store.coverage(meter_id, start, end)
        now = time.time()
        stale = []
        d = start
        while d <= end:
            fetched = covered.get(d)
            ttl = self.settings.readings_closed_ttl_s if self._is_closed(d, latest) else self.settings.readings_recent_ttl_s
            if fetched is None or now - fetched > ttl:
                stale.append(d)
            d += timedelta(days=1)
        return stale

    async def _ensure_readings(self, meter_id: str, start: date, end: date) -> dict[str, Any]:
        """Make sure [start, end] is cached and fresh. Returns cache info for ``meta``."""
        async with self._meter_locks[meter_id]:
            stale = self._stale_days(meter_id, start, end)
            info = {"portal_calls": 0, "quality": {}}
            if stale:
                # One contiguous portal call covering every stale day (the portal has no window cap).
                lo, hi = min(stale), max(stale)
                raw = await self.portal.meter_energy(meter_id, lo, hi)
                rows, quality = clean_readings(raw)
                days = [lo + timedelta(days=i) for i in range((hi - lo).days + 1)]
                self.store.upsert_readings(meter_id, rows, days)
                info["portal_calls"] = 1
                info["quality"] = quality
            info["cache"] = "miss" if stale else "hit"
            return info

    async def _default_window(self, meter_id: str) -> tuple[date, date, dict[str, Any] | None]:
        """Mirror the portal's default: the 7 days ending at the meter's latest reading.

        The portal's data does not run up to "today", so a wall-clock window would
        usually be empty. To avoid an extra round-trip we use the newest reading seen
        anywhere in the fleet as a hint and fetch around it; only if this meter has no
        data near that date do we ask the portal for its own default window.
        """
        latest = self.store.latest_reading_ts(meter_id)
        info = None
        if latest is None or not self._is_fresh_latest(meter_id):
            info = {"portal_calls": 0, "quality": {}, "cache": "hit"}
            fleet_latest = self.store.latest_reading_ts()
            if fleet_latest is not None:
                hint = fleet_latest.date()
                got = await self._ensure_readings(meter_id, hint - timedelta(days=DEFAULT_WINDOW_DAYS), hint + timedelta(days=1))
                info.update(got, portal_calls=got["portal_calls"])
                latest = self.store.latest_reading_ts(meter_id)
            if latest is None or (fleet_latest is not None and latest.date() < fleet_latest.date() - timedelta(days=1)):
                async with self._meter_locks[meter_id]:
                    rows, quality = clean_readings(await self.portal.meter_energy(meter_id))
                    # Rows only; coverage is recorded by the explicit-range fetch that follows.
                    self.store.upsert_readings(meter_id, rows, [])
                info.update(portal_calls=info["portal_calls"] + 1, quality=quality, cache="miss")
                latest = self.store.latest_reading_ts(meter_id)
            self.store.set_meta(f"latest_checked:{meter_id}", time.time())
        if latest is None:
            today = datetime.now(IST).date()
            return today - timedelta(days=DEFAULT_WINDOW_DAYS - 1), today, info
        end = latest.date()
        return end - timedelta(days=DEFAULT_WINDOW_DAYS - 1), end, info

    def _is_fresh_latest(self, meter_id: str) -> bool:
        checked = self.store.get_meta(f"latest_checked:{meter_id}")
        return checked is not None and time.time() - checked < self.settings.readings_recent_ttl_s

    async def readings(self, meter_id: str, start: date | None, end: date | None, granularity: str = "raw") -> dict[str, Any]:
        meter = self.store.get_meter(meter_id)
        if meter is None:
            raise KeyError(meter_id)
        meter_id = meter["meter_id"]

        default_info = None
        if start is None and end is None:
            start, end, default_info = await self._default_window(meter_id)
        elif start is None:
            start = end - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
        elif end is None:
            end = start + timedelta(days=DEFAULT_WINDOW_DAYS - 1)
        if end < start:
            raise ValueError("'to' must be on or after 'from'")
        if (end - start).days + 1 > MAX_WINDOW_DAYS:
            raise ValueError(f"Window too large; maximum is {MAX_WINDOW_DAYS} days")

        # Fetch one day either side: the day before gives the first interval its baseline,
        # the day after closes the last interval (daily meters read at 00:00).
        info = await self._ensure_readings(meter_id, start - timedelta(days=1), end + timedelta(days=1))
        if default_info:
            info["portal_calls"] += default_info["portal_calls"]
            info["quality"] = default_info["quality"] or info["quality"]
            if default_info["portal_calls"]:
                info["cache"] = "miss"

        lo = datetime.combine(start - timedelta(days=1), datetime.min.time(), IST)
        hi = datetime.combine(end + timedelta(days=2), datetime.min.time(), IST)
        rows = with_deltas(self.store.readings(meter_id, lo, hi))
        cadence = cadence_minutes(rows)
        start_dt = datetime.combine(start, datetime.min.time(), IST)
        end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), IST)
        in_window = [r for r in rows if start_dt <= r["ts"] < end_dt]
        daily = {d: v for d, v in daily_rollup(rows, cadence).items() if start <= d <= end}

        total_kwh = round(sum(v["kwh"] for v in daily.values()), 3)
        total_kvah = round(sum(v["kvah"] for v in daily.values()), 3)
        volts = [r["voltage_r"] for r in in_window if r["voltage_r"] is not None]
        summary = {
            "consumption_kwh": total_kwh,
            "apparent_energy_kvah": total_kvah,
            "power_factor": round(total_kwh / total_kvah, 3) if total_kvah else None,
            "avg_daily_kwh": round(total_kwh / len(daily), 3) if daily else None,
            "peak_day": max(daily.values(), key=lambda v: v["kwh"])["date"] if daily else None,
            "readings": len(in_window),
            "cadence_minutes": cadence,
            "first_reading": in_window[0]["ts"].isoformat() if in_window else None,
            "last_reading": in_window[-1]["ts"].isoformat() if in_window else None,
            "voltage_min": min(volts) if volts else None,
            "voltage_max": max(volts) if volts else None,
            "voltage_out_of_band": sum(1 for v in volts if not VOLTAGE_LOW <= v <= VOLTAGE_HIGH),
            "latest_register_kwh": next((r["kwh"] for r in reversed(in_window) if r["kwh"] is not None), None),
        }
        out: dict[str, Any] = {
            "meter_id": meter_id,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "granularity": granularity,
            "summary": summary,
            "meta": {
                "timezone": "Asia/Kolkata",
                "cache": info["cache"],
                "portal_calls": info["portal_calls"],
                "cleaning": info["quality"],
            },
        }
        if granularity == "daily":
            out["data"] = list(daily.values())
        else:
            out["data"] = [
                {
                    "timestamp": r["ts"].isoformat(),
                    "kwh_register": r["kwh"],
                    "kvah_register": r["kvah"],
                    "kwh": r["kwh_delta"],
                    "kvah": r["kvah_delta"],
                    "voltage_r": r["voltage_r"],
                    "interval_start": r["interval_start"].isoformat() if r["interval_start"] else None,
                    "flags": r["flags"],
                }
                for r in in_window
            ]
        return out

    # ---------------------------------------------------- background warmer

    async def _warm_loop(self) -> None:
        """Slowly pull a recent window for every meter so fleet insights have data.

        Paced evenly at ``warm_rate_per_minute`` (on top of the client's global limiter)
        so interactive API calls always have headroom.
        """
        interval = 60.0 / max(1, self.settings.warm_rate_per_minute)
        while True:
            try:
                while self.store.get_meta("inventory_synced_at") is None:
                    await asyncio.sleep(2)
                meters = self.store.all_meters()
                self.warm_state.update(running=True, total=len(meters), done=0, started_at=datetime.now(IST).isoformat())
                for m in meters:
                    await asyncio.sleep(interval)
                    try:
                        await self.readings(m["meter_id"], None, None)
                    except PortalError as exc:
                        self.warm_state["last_error"] = str(exc)
                    self.warm_state["done"] += 1
                self.warm_state.update(running=False, finished_at=datetime.now(IST).isoformat())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Readings warm-up error: %s", exc)
                self.warm_state["last_error"] = str(exc)
            await asyncio.sleep(self.settings.readings_recent_ttl_s)

    # ------------------------------------------------------------- insights

    def insights(self, window_days: int = 7) -> dict[str, Any]:
        """Fleet health computed from whatever readings are cached (coverage is reported)."""
        meters = self.store.all_meters()
        by_id = {m["meter_id"]: m for m in meters}
        have = self.store.meters_with_readings() & set(by_id)
        latest_fleet = self.store.latest_reading_ts()
        anomalies: list[dict[str, Any]] = []
        per_meter: list[dict[str, Any]] = []
        dt_kwh: dict[str, float] = defaultdict(float)
        daily_totals: dict[str, float] = defaultdict(float)

        if latest_fleet:
            end = latest_fleet.date() - timedelta(days=1)  # last *complete* day
            start = end - timedelta(days=window_days - 1)
            lo = datetime.combine(start - timedelta(days=1), datetime.min.time(), IST)
            hi = datetime.combine(end + timedelta(days=2), datetime.min.time(), IST)
            for meter_id in sorted(have):
                m = by_id[meter_id]
                rows = with_deltas(self.store.readings(meter_id, lo, hi))
                if not rows:
                    continue
                cadence = cadence_minutes(rows)
                daily = {d: v for d, v in daily_rollup(rows, cadence).items() if start <= d <= end}
                kwh = [v["kwh"] for v in daily.values()]
                avg = mean(kwh) if kwh else 0.0
                last_ts = rows[-1]["ts"]
                for d, v in daily.items():
                    daily_totals[d.isoformat()] += v["kwh"]
                dt_kwh[m["transformer_code"]] += avg
                volts = [r["voltage_r"] for r in rows if r["voltage_r"] is not None]
                per_meter.append({"meter_id": meter_id, "avg_daily_kwh": round(avg, 2), "status": m["status"], "cadence_minutes": cadence})

                if m["status"] == "decommissioned" and avg > 0.5:
                    anomalies.append(_anomaly(m, "decommissioned_but_consuming", "high", f"Decommissioned meter still recording ~{avg:.1f} kWh/day"))
                if m["status"] == "faulty" and avg > 0.5:
                    anomalies.append(_anomaly(m, "faulty_but_reporting", "medium", f"Marked faulty but reporting normally (~{avg:.1f} kWh/day); status may be out of date"))
                if m["status"] == "installed" and kwh and avg < 0.05:
                    anomalies.append(_anomaly(m, "zero_consumption", "medium", "Installed meter shows no consumption in the window"))
                if last_ts < latest_fleet - timedelta(days=2):
                    anomalies.append(_anomaly(m, "stale_data", "medium", f"Last reading {last_ts.isoformat()} lags the fleet by >2 days"))
                if volts and (min(volts) < VOLTAGE_LOW or max(volts) > VOLTAGE_HIGH):
                    anomalies.append(_anomaly(m, "voltage_out_of_band", "medium", f"Voltage range {min(volts)}–{max(volts)} V outside {VOLTAGE_LOW:.0f}–{VOLTAGE_HIGH:.0f} V"))
                if any("register_decreased" in r["flags"] for r in rows):
                    anomalies.append(_anomaly(m, "register_decreased", "high", "Cumulative kWh register went backwards (rollover, swap or bad read)"))
                if any("missing_kwh" in r["flags"] for r in rows):
                    anomalies.append(_anomaly(m, "missing_values", "low", "Some readings had blank kWh/voltage (duplicates merged where possible)"))
        else:
            start = end = None

        # Transformer loading: average demand (kW) ≈ kWh/day ÷ 24, converted to kVA with the fleet PF.
        transformers = {t["code"]: t for t in self.store.transformers()}
        loading = []
        for code, kwh_day in sorted(dt_kwh.items()):
            cap = (transformers.get(code) or {}).get("capacity_kva")
            avg_kva = kwh_day / 24 / 0.926
            loading.append({
                "transformer_code": code,
                "avg_load_kva": round(avg_kva, 2),
                "capacity_kva": cap,
                "utilisation_pct": round(100 * avg_kva / cap, 2) if cap else None,
            })
        loading.sort(key=lambda x: x["utilisation_pct"] or 0, reverse=True)
        per_meter.sort(key=lambda x: x["avg_daily_kwh"], reverse=True)
        histogram = _histogram([p["avg_daily_kwh"] for p in per_meter], width=2.0)
        severity_rank = {"high": 0, "medium": 1, "low": 2}
        anomalies.sort(key=lambda a: (severity_rank[a["severity"]], a["meter_id"]))

        return {
            "window": {"from": start.isoformat() if start else None, "to": end.isoformat() if end else None, "days": window_days},
            "coverage": {"meters_total": len(meters), "meters_with_readings": len(have), "pct": round(100 * len(have) / len(meters), 1) if meters else 0.0},
            "warmup": self.warm_state,
            "status_counts": dict(Counter(m["status"] for m in meters)),
            "cadence_counts": {str(k): v for k, v in Counter(p["cadence_minutes"] for p in per_meter).items()},
            "anomaly_counts": dict(Counter(a["type"] for a in anomalies)),
            "anomalies": anomalies,
            "top_consumers": per_meter[:10],
            "daily_fleet_kwh": [{"date": d, "kwh": round(v, 2)} for d, v in sorted(daily_totals.items())],
            "consumption_histogram": histogram,
            "avg_daily_kwh_per_meter": round(mean(p["avg_daily_kwh"] for p in per_meter), 2) if per_meter else None,
            "transformer_loading": loading,
        }

    def tree(self, depth: int) -> list[dict[str, Any]]:
        transformers = {t["code"]: t for t in self.store.transformers()}
        return build_tree(self.store.all_meters(), transformers, max_depth=depth)

    def node(self, node_id: str) -> dict[str, Any] | None:
        depth = node_id.count("/") + 1
        if depth > len(LEVELS):
            return None
        for top in self.tree(min(len(LEVELS), depth + 1)):
            found = _find(top, node_id)
            if found:
                return found
        return None


def _find(node: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    if node["id"] == node_id:
        return node
    if not node_id.startswith(node["id"] + "/"):
        return None
    for child in node["children"]:
        hit = _find(child, node_id)
        if hit:
            return hit
    return None


def _histogram(values: list[float], width: float) -> list[dict[str, Any]]:
    """Fixed-width bins (kWh/day) so the chart is comparable as coverage grows."""
    if not values:
        return []
    lo = width * int(min(values) // width)
    hi = width * (int(max(values) // width) + 1)
    bins = [{"from": round(lo + i * width, 2), "to": round(lo + (i + 1) * width, 2), "meters": 0} for i in range(int(round((hi - lo) / width)))]
    for v in values:
        bins[min(len(bins) - 1, int((v - lo) // width))]["meters"] += 1
    return bins


def _anomaly(m: dict[str, Any], type_: str, severity: str, message: str) -> dict[str, Any]:
    return {"meter_id": m["meter_id"], "transformer_code": m["transformer_code"], "status": m["status"], "type": type_, "severity": severity, "message": message}
