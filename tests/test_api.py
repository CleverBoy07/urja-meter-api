import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.portal.client import PortalClient


@pytest.fixture
def api(settings, transport, monkeypatch):
    monkeypatch.setattr(PortalClient, "_backoff", lambda self, attempt, base: 0.0)
    app = create_app(settings, transport=transport)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def synced(api):
    assert api.post("/v1/admin/sync").status_code == 200
    return api


def test_not_ready_before_first_sync(api):
    r = api.get("/v1/meters")
    assert r.status_code == 503
    assert r.headers["content-type"].startswith("application/problem+json")
    assert api.get("/v1/health").json()["status"] == "starting"


def test_list_and_filter(synced):
    body = synced.get("/v1/meters").json()
    assert body["page"]["total"] == 8 and body["meta"]["source"] == "export"
    r = synced.get("/v1/meters", params={"status": "installed,faulty", "phase": "three"}).json()
    assert all(m["status"] in {"installed", "faulty"} and m["phase"] == "three" for m in r["data"])
    r = synced.get("/v1/meters", params=[("transformer", "dt-007")]).json()
    assert {m["meter_id"] for m in r["data"]} == {"J100400", "J100401"}
    r = synced.get("/v1/meters", params={"q": "gn3"}).json()
    assert [m["meter_id"] for m in r["data"]] == ["J100400"]


def test_pagination(synced):
    r = synced.get("/v1/meters", params={"limit": 3, "offset": 0}).json()
    assert len(r["data"]) == 3 and r["page"]["next_offset"] == 3
    last = synced.get("/v1/meters", params={"limit": 3, "offset": 6}).json()
    assert last["page"]["next_offset"] is None


def test_near_query_sorted_by_distance(synced):
    m = synced.get("/v1/meters/J100000").json()["data"]
    r = synced.get("/v1/meters", params={"near": f"{m['location']['lat']},{m['location']['lng']}", "radius_km": 50}).json()
    assert r["data"][0]["meter_id"] == "J100000" and r["data"][0]["distance_km"] == 0
    d = [x["distance_km"] for x in r["data"]]
    assert d == sorted(d)
    assert synced.get("/v1/meters", params={"near": "abc"}).status_code == 422


def test_network_node_filter_and_tree(synced):
    tree = synced.get("/v1/network/tree", params={"depth": 1}).json()["data"]
    assert sum(z["meter_count"] for z in tree) == 8
    node = synced.get("/v1/network/nodes/Z-01/C-01").json()
    assert node["data"]["level"] == "circle" and node["meter_total"] == len(node["meters"])
    assert synced.get("/v1/network/nodes/Z-99").status_code == 404


def test_meter_detail_and_404(synced):
    m = synced.get("/v1/meters/j100011").json()["data"]
    assert m["network"]["circle"]["code"] == "C-06"
    assert m["data_quality"][0]["type"] == "blank_code_filled"
    r = synced.get("/v1/meters/NOPE")
    assert r.status_code == 404 and r.json()["title"] == "Not Found"


def test_readings_default_window_and_cache(synced, fake_portal):
    r = synced.get("/v1/meters/J100089/readings").json()
    assert r["to"] == "2026-06-30" and r["from"] == "2026-06-24"
    assert r["meta"]["cache"] == "miss" and r["meta"]["cleaning"]["duplicates_merged"] == 1
    ts = [x["timestamp"] for x in r["data"]]
    assert len(ts) == len(set(ts))
    energy_calls = fake_portal.calls.count("/portal/meters/J100089/energy")
    again = synced.get("/v1/meters/J100089/readings").json()
    assert again["meta"]["cache"] == "hit"
    assert fake_portal.calls.count("/portal/meters/J100089/energy") == energy_calls


def test_readings_daily_half_hourly(synced):
    r = synced.get("/v1/meters/J100000/readings", params={"from": "2026-06-01", "to": "2026-06-02", "granularity": "daily"}).json()
    assert [d["date"] for d in r["data"]] == ["2026-06-01", "2026-06-02"]
    assert r["data"][0]["completeness"] == 1.0
    assert r["summary"]["cadence_minutes"] == 30


def test_readings_validation(synced):
    assert synced.get("/v1/meters/J100000/readings", params={"from": "2026-06-10", "to": "2026-06-01"}).status_code == 422
    assert synced.get("/v1/meters/J100000/readings", params={"from": "junk"}).status_code == 422
    assert synced.get("/v1/meters/NOPE/readings").status_code == 404


def test_transformer_detail(synced):
    t = synced.get("/v1/transformers/DT-007").json()
    assert t["data"]["aliases"] == ["Old Malviya Nagar Xfmr"] and len(t["meters"]) == 2
    assert t["network"]["feeder"]["code"] == "F-007"


def test_data_quality_and_insights(synced):
    dq = synced.get("/v1/data-quality").json()
    assert "blank_code_filled" in dq["issue_counts"]
    synced.get("/v1/meters/J100000/readings")
    ins = synced.get("/v1/insights").json()
    assert ins["coverage"]["meters_with_readings"] >= 1
    # J100000 is decommissioned but still consuming in the portal data
    assert any(a["meter_id"] == "J100000" and a["type"] == "decommissioned_but_consuming" for a in ins["anomalies"])


def test_api_key_enforced(settings, transport):
    from dataclasses import replace
    app = create_app(replace(settings, api_keys=frozenset({"k1"})), transport=transport)
    with TestClient(app) as c:
        assert c.get("/v1/health").status_code == 401
        assert c.get("/v1/health", headers={"X-API-Key": "k1"}).status_code == 200


def test_serves_stale_inventory_when_portal_breaks(synced, fake_portal):
    fake_portal.export_broken = True
    r = synced.post("/v1/admin/sync")
    assert r.status_code in (502, 503)
    body = synced.get("/v1/meters").json()
    assert body["page"]["total"] == 8 and body["meta"]["last_sync_error"]
    assert synced.get("/v1/health").json()["status"] == "degraded"
