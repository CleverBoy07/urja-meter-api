"""Async client for the Urja Meter Ops portal's undocumented JSON endpoints.

Everything portal-specific (auth, signing, pacing, retries, quirks of the raw
responses) lives here so the rest of the service only sees plain dicts.

Robustness features:
- Session login via better-auth's JSON endpoint; transparent re-login on 401 and
  proactive re-login before the 1 h cookie expires.
- Client-side rate limiting below the portal's ~120 req/min window, plus a shared
  cool-down when the portal answers 429 (it sends no Retry-After header).
- Exponential back-off with jitter on 429 / 5xx / network errors.
- A small circuit breaker so a dead portal fails fast instead of piling up requests.
- Clock-offset tracking from the portal's ``Date`` header so export signatures stay
  inside the server's ±5 min window even if our clock drifts.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.config import Settings
from app.portal.devalue import page_data
from app.portal.signing import signed_headers

log = logging.getLogger("urja.portal")

SESSION_COOKIE = "__Secure-better-auth.session_token"
SESSION_MAX_AGE_S = 3600
RELOGIN_AFTER_S = SESSION_MAX_AGE_S - 5 * 60


class PortalError(Exception):
    """Base error for anything that went wrong talking to the portal."""

    status_code = 502


class PortalUnavailable(PortalError):
    status_code = 503


class PortalAuthError(PortalError):
    status_code = 502


class PortalRateLimited(PortalError):
    status_code = 503


class PortalNotFound(PortalError):
    status_code = 404


class RateLimiter:
    """Sliding-window limiter: at most ``rate`` acquisitions in any 60 s span.

    A shared ``cooldown`` lets one 429 pause every in-flight caller, because the
    portal's limit is per session, not per request path.
    """

    def __init__(self, rate_per_minute: int, window_s: float = 60.0) -> None:
        self.rate = max(1, rate_per_minute)
        self.window = window_s
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0

    def cooldown(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                if now < self._blocked_until:
                    await asyncio.sleep(self._blocked_until - now)
                    continue
                while self._stamps and now - self._stamps[0] >= self.window:
                    self._stamps.popleft()
                if len(self._stamps) < self.rate:
                    self._stamps.append(now)
                    return
                await asyncio.sleep(self.window - (now - self._stamps[0]) + 0.01)


@dataclass
class PortalStats:
    requests: int = 0
    rate_limited: int = 0
    retries: int = 0
    logins: int = 0
    failures: int = 0
    last_success_at: float | None = None
    last_error: str | None = None
    clock_offset_s: float = 0.0
    circuit_open_until: float = 0.0
    recent_latency_ms: deque[float] = field(default_factory=lambda: deque(maxlen=50))

    def as_dict(self) -> dict[str, Any]:
        lat = sorted(self.recent_latency_ms)
        return {
            "requests": self.requests,
            "rate_limited": self.rate_limited,
            "retries": self.retries,
            "logins": self.logins,
            "failures": self.failures,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "clock_offset_s": round(self.clock_offset_s, 2),
            "circuit_open": time.time() < self.circuit_open_until,
            "p50_latency_ms": round(lat[len(lat) // 2], 1) if lat else None,
        }


class PortalClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limiter: RateLimiter | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.portal_url,
            timeout=settings.portal_timeout_s,
            transport=transport,
            headers={"User-Agent": "urja-meter-api/1.0", "Accept": "application/json"},
            follow_redirects=False,
        )
        self.limiter = limiter or RateLimiter(settings.portal_rate_per_minute)
        self._sem = asyncio.Semaphore(settings.portal_concurrency)
        self._login_lock = asyncio.Lock()
        self._logged_in_at: float | None = None
        self._secret: str | None = None
        self._consecutive_failures = 0
        self._sleep = sleep
        self.stats = PortalStats()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ auth

    @property
    def has_credentials(self) -> bool:
        return bool(self.settings.username and self.settings.password)

    async def login(self, *, force: bool = False, stale_session: float | None = None) -> None:
        """Sign in. ``stale_session`` is the login time a caller saw fail; if another
        coroutine already replaced that session we skip the duplicate login."""
        if not self.has_credentials:
            raise PortalAuthError("URJA_USERNAME / URJA_PASSWORD are not configured")
        async with self._login_lock:
            if stale_session is not None and self._logged_in_at != stale_session:
                return
            fresh = self._logged_in_at and time.time() - self._logged_in_at < RELOGIN_AFTER_S
            if fresh and not force:
                return
            await self.limiter.acquire()
            self.stats.requests += 1
            # Origin header is required: better-auth rejects cross-site sign-ins.
            resp = await self._http.post(
                "/api/auth/sign-in/email",
                json={"email": self.settings.username, "password": self.settings.password},
                headers={"Origin": self.settings.portal_url},
            )
            self._track_clock(resp)
            if resp.status_code == 429:
                self.stats.rate_limited += 1
                raise PortalRateLimited("Portal rate-limited the login request")
            if resp.status_code != 200 or SESSION_COOKIE not in self._http.cookies:
                raise PortalAuthError(f"Portal login failed (HTTP {resp.status_code})")
            self._logged_in_at = time.time()
            self.stats.logins += 1
            log.info("Logged in to portal")

    async def _ensure_session(self) -> None:
        if not self._logged_in_at or time.time() - self._logged_in_at >= RELOGIN_AFTER_S:
            await self.login(force=True)

    async def signing_secret(self, *, refresh: bool = False) -> str:
        if self._secret is None or refresh:
            body = await self._get_json("/portal/keys")
            self._secret = body["data"]["signingSecret"]
        return self._secret

    # ------------------------------------------------------------ transport

    def _track_clock(self, resp: httpx.Response) -> None:
        date_header = resp.headers.get("date")
        if not date_header:
            return
        try:
            server = email.utils.parsedate_to_datetime(date_header).timestamp()
        except (TypeError, ValueError):
            return
        # Exponential smoothing; Date only has 1 s resolution.
        self.stats.clock_offset_s = 0.8 * self.stats.clock_offset_s + 0.2 * (server - time.time())

    def _backoff(self, attempt: int, base: float) -> float:
        return min(60.0, base * (2**attempt)) * (0.75 + random.random() * 0.5)

    async def _send(self, path: str, query: str = "", *, signed: bool = False) -> httpx.Response:
        if time.time() < self.stats.circuit_open_until:
            raise PortalUnavailable("Portal circuit breaker is open after repeated failures")
        await self._ensure_session()

        relogged = resecreted = False
        attempt = 0
        while True:
            headers: dict[str, str] = {}
            if signed:
                secret = await self.signing_secret()
                headers = signed_headers(secret, "GET", path, query, time.time() + self.stats.clock_offset_s)
            url = f"{path}?{query}" if query else path
            session = self._logged_in_at
            await self.limiter.acquire()
            started = time.perf_counter()
            try:
                async with self._sem:
                    self.stats.requests += 1
                    resp = await self._http.get(url, headers=headers)
            except httpx.TransportError as exc:
                self._record_failure(f"{type(exc).__name__}: {exc}")
                if attempt >= self.settings.portal_max_retries:
                    raise PortalUnavailable(f"Portal unreachable: {exc}") from exc
                self.stats.retries += 1
                await self._sleep(self._backoff(attempt, 1.0))
                attempt += 1
                continue

            self.stats.recent_latency_ms.append((time.perf_counter() - started) * 1000)
            self._track_clock(resp)
            status = resp.status_code

            if status == 401:
                error = _error_code(resp)
                if error == "signature_invalid" and not resecreted:
                    resecreted = True
                    await self.signing_secret(refresh=True)
                    continue
                if not relogged:
                    relogged = True
                    await self.login(force=True, stale_session=session)
                    continue
                self._record_failure(f"401 {error}")
                raise PortalAuthError(f"Portal rejected credentials/signature ({error})")

            if status in (302, 303, 307) and "/login" in resp.headers.get("location", ""):
                # A page route (not /portal/*) bounces to /login when the session died.
                if not relogged:
                    relogged = True
                    await self.login(force=True, stale_session=session)
                    continue
                raise PortalAuthError("Portal keeps redirecting to /login")

            if status == 429:
                self.stats.rate_limited += 1
                wait = self._backoff(attempt, 5.0)
                self.limiter.cooldown(wait)
                if attempt >= self.settings.portal_max_retries:
                    raise PortalRateLimited("Portal is rate-limiting us; try again shortly")
                self.stats.retries += 1
                attempt += 1
                continue  # limiter.acquire() will wait out the cool-down

            if status >= 500:
                self._record_failure(f"HTTP {status}")
                if attempt >= self.settings.portal_max_retries:
                    raise PortalUnavailable(f"Portal error HTTP {status}")
                self.stats.retries += 1
                await self._sleep(self._backoff(attempt, 1.0))
                attempt += 1
                continue

            if status == 404:
                self._record_success()
                raise PortalNotFound(_error_message(resp) or "Not found")

            if status >= 400:
                self._record_failure(f"HTTP {status}")
                raise PortalError(f"Unexpected portal response HTTP {status}")

            self._record_success()
            return resp

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self.stats.last_success_at = time.time()

    def _record_failure(self, message: str) -> None:
        self._consecutive_failures += 1
        self.stats.failures += 1
        self.stats.last_error = message
        if self._consecutive_failures >= 8:
            self.stats.circuit_open_until = time.time() + 30
            self._consecutive_failures = 0
            log.warning("Opening portal circuit breaker for 30 s: %s", message)

    async def _get_json(self, path: str, params: dict[str, Any] | None = None, *, signed: bool = False) -> Any:
        query = urlencode(params or {}, quote_via=quote)
        resp = await self._send(path, query, signed=signed)
        try:
            return resp.json()
        except ValueError as exc:
            raise PortalError(f"Portal returned non-JSON for {path}") from exc

    # --------------------------------------------------------- data access

    async def export_meters(self) -> list[dict[str, Any]]:
        """Bulk path: one signed call returns every meter with hierarchy + geo.

        The portal ignores ``page``/``pageSize`` here and always returns everything.
        """
        body = await self._get_json("/portal/export", {"page": 1}, signed=True)
        data = body.get("data") or []
        if body.get("total") not in (None, len(data)):
            log.warning("Export total=%s but %s rows returned", body.get("total"), len(data))
        return data

    async def list_transformers(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            body = await self._get_json("/portal/dts", {"page": page})
            batch = body.get("data") or []
            rows.extend(batch)
            if not batch or len(rows) >= int(body.get("total") or 0):
                return rows
            page += 1

    async def search_meters(self, q: str = "", page: int = 1) -> dict[str, Any]:
        return await self._get_json("/portal/meters/search", {"q": q, "page": page})

    async def meter_geo(self, meter_id: str) -> dict[str, Any]:
        return (await self._get_json(f"/portal/meters/{quote(meter_id)}/geo"))["data"]

    async def meter_energy(self, meter_id: str, start: date | None = None, end: date | None = None) -> list[dict[str, Any]]:
        params = {}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()
        return (await self._get_json(f"/portal/meters/{quote(meter_id)}/energy", params))["data"]

    async def meter_page(self, meter_id: str) -> dict[str, Any]:
        """Server-rendered detail page data (nameplate + hierarchy) via SvelteKit's __data.json."""
        body = await self._get_json(f"/meters/{quote(meter_id)}/__data.json", {"x-sveltekit-invalidated": "001"})
        data, error = page_data(body)
        if error:
            if error.get("status") == 404:
                raise PortalNotFound(error.get("message") or "Meter not found")
            raise PortalError(error.get("message") or "Portal page error")
        return data

    async def crawl_meters(self) -> list[dict[str, Any]]:
        """Slow fallback if the bulk export ever stops working.

        Walks the paginated search, then each meter's detail page and geo endpoint,
        and reshapes the result to look like an export row. ~2 requests per meter.
        """
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            body = await self.search_meters("", page)
            rows.extend(body.get("data") or [])
            if page * int(body.get("pageSize") or 20) >= int(body.get("total") or 0):
                break
            page += 1

        async def enrich(row: dict[str, Any]) -> dict[str, Any]:
            detail, geo = await asyncio.gather(self.meter_page(row["meterId"]), self.meter_geo(row["meterId"]))
            return export_row_from_page(row, detail, geo)

        return list(await asyncio.gather(*(enrich(r) for r in rows)))


def export_row_from_page(row: dict[str, Any], detail: dict[str, Any], geo: dict[str, Any]) -> dict[str, Any]:
    """Convert search row + detail page + geo into the export's row shape."""
    from app.normalize import parse_detail_fields, parse_labelled_node

    fields, build = parse_detail_fields(detail.get("detail") or {})
    levels = {
        "zone": "Zone",
        "circle": "Circle",
        "division": "Division",
        "subdivision": "Subdivision",
        "substation": "Sub Station",
        "feeder": "Feeder",
        "dt": "DT",
    }
    hierarchy_raw = detail.get("hierarchy") or {}
    return {
        **row,
        "installType": fields.get("installType"),
        "build": build,
        "hierarchy": {key: parse_labelled_node(hierarchy_raw.get(label)) for key, label in levels.items()},
        "geo": {"lat": _to_float(geo.get("latitude")), "lng": _to_float(geo.get("longitude"))},
    }


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _error_code(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error") or "")
    except ValueError:
        return ""


def _error_message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("message") or "")
    except ValueError:
        return ""
