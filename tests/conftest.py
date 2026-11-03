"""A fake Urja portal (httpx MockTransport) that reproduces the real one's behaviour
and quirks closely enough to test the client and API offline."""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.config import Settings
from app.portal.signing import sign

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "test-signing-secret"
COOKIE = "__Secure-better-auth.session_token"


class FakePortal:
    def __init__(self) -> None:
        self.export = json.loads((FIXTURES / "export.json").read_text())["data"]
        self.dts = json.loads((FIXTURES / "dts.json").read_text())
        self.energy = json.loads((FIXTURES / "energy.json").read_text())
        self.sessions: set[str] = set()
        self.calls: list[str] = []
        self.fail_next: list[int] = []  # status codes to return before serving normally
        self.export_broken = False
        self.logins = 0

    # -- helpers
    def _json(self, status: int, body) -> httpx.Response:
        return httpx.Response(status, json=body, headers={"date": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())})

    def expire_sessions(self) -> None:
        self.sessions.clear()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path == "/api/auth/sign-in/email":
            body = json.loads(request.content)
            if body != {"email": "op@test", "password": "pw"}:
                return self._json(401, {"message": "Invalid email or password"})
            self.logins += 1
            token = f"tok{self.logins}"
            self.sessions.add(token)
            resp = self._json(200, {"token": token})
            resp.headers["set-cookie"] = f"{COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Lax"
            return resp

        if self.fail_next:
            status = self.fail_next.pop(0)
            return self._json(status, {"error": "rate_limited" if status == 429 else "server_error", "message": "x"})

        cookie = request.headers.get("cookie", "")
        token = cookie.split(f"{COOKIE}=")[-1].split(";")[0] if COOKIE in cookie else None
        if token not in self.sessions:
            return self._json(401, {"error": "unauthorized", "message": "A valid session is required."})

        q = parse_qs(request.url.query.decode())
        if path == "/portal/keys":
            return self._json(200, {"data": {"signingSecret": SECRET}})
        if path == "/portal/export":
            ts = request.headers.get("x-timestamp", "")
            expected = sign(SECRET, "GET", path, request.url.query.decode(), ts)
            if self.export_broken or request.headers.get("x-signature") != expected or abs(time.time() - int(ts or 0)) > 300:
                return self._json(401, {"error": "signature_invalid", "message": "Missing or invalid request signature."})
            return self._json(200, {"data": self.export, "total": len(self.export)})
        if path == "/portal/dts":
            page = int(q.get("page", ["1"])[0])
            return self._json(200, {"data": self.dts[(page - 1) * 20 : page * 20], "total": len(self.dts), "page": page, "pageSize": 20})
        if path == "/portal/meters/search":
            qs = q.get("q", [""])[0].lower()
            page = int(q.get("page", ["1"])[0])
            rows = [
                {k: m[k] for k in ("meterId", "serialNo", "make", "phaseType", "installStatus", "dtCode")}
                for m in self.export
                if qs in m["meterId"].lower() or qs in m["serialNo"].lower()
            ]
            return self._json(200, {"data": rows[(page - 1) * 20 : page * 20], "total": len(rows), "page": page, "pageSize": 20})
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["portal", "meters"]:
            meter = next((m for m in self.export if m["meterId"] == parts[2]), None)
            if meter is None:
                return self._json(404, {"error": "not_found", "message": "Meter not found"})
            if parts[3] == "geo":
                return self._json(200, {"data": {"latitude": str(meter["geo"]["lat"]), "longitude": str(meter["geo"]["lng"])}})
            if parts[3] == "energy":
                return self._json(200, {"data": self._energy(parts[2], q)})
        if len(parts) == 3 and parts[0] == "meters" and parts[2] == "__data.json":
            return self._json(200, self._page(parts[1]))
        return httpx.Response(404, text="<html>not found</html>")

    def _energy(self, meter_id: str, q: dict) -> list[dict]:
        rows = self.energy.get(meter_id, [])

        def d(r):
            return datetime.strptime(r["timestamp"], "%d/%m/%Y %H:%M").date()

        try:
            lo = date.fromisoformat(q["from"][0]) if "from" in q else None
            hi = date.fromisoformat(q["to"][0]) if "to" in q else None
        except ValueError:
            lo = hi = None  # the real portal silently ignores bad dates
        if lo is None and hi is None and rows:
            last = d(rows[-1])
            return [r for r in rows if (last - d(r)).days <= 7]
        return [r for r in rows if (lo is None or d(r) >= lo) and (hi is None or d(r) <= hi)]

    def _page(self, meter_id: str) -> dict:
        meter = next((m for m in self.export if m["meterId"] == meter_id), None)
        if meter is None:
            return {"type": "data", "nodes": [None, {"type": "data", "data": [{}]}, {"type": "error", "error": {"message": "Meter not found"}, "status": 404}]}
        h = meter["hierarchy"]
        label = lambda n: f"{n['name']} ({n['code']})"  # noqa: E731
        hierarchy = {
            "Zone": label(h["zone"]), "Circle": label(h["circle"]), "Division": label(h["division"]),
            "Subdivision": label(h["subdivision"]), "Sub Station": label(h["substation"]),
            "Feeder": label(h["feeder"]), "DT": label(h["dt"]),
        }
        detail = {"classData": json.dumps({"installed_meter": {"MeterId": meter_id, "InstallationType": meter["installType"]}})}
        flat = [{"meterId": 1, "detail": 2, "hierarchy": 3}, meter_id, {"classData": 4}, {k: 5 + i for i, k in enumerate(hierarchy)}, detail["classData"], *hierarchy.values()]
        return {"type": "data", "nodes": [{"type": "skip"}, {"type": "skip"}, {"type": "data", "data": flat}]}


@pytest.fixture
def fake_portal() -> FakePortal:
    return FakePortal()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        portal_url="https://portal.test",
        username="op@test",
        password="pw",
        db_path=":memory:",
        warm_readings=False,
        sync_on_startup=False,
        portal_rate_per_minute=1000,
    )


@pytest.fixture
def transport(fake_portal) -> httpx.MockTransport:
    return httpx.MockTransport(fake_portal.handler)


async def _no_sleep(_):
    return None


@pytest.fixture
def no_sleep():
    return _no_sleep
