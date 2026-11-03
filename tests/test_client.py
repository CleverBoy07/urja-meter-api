import pytest

from app.portal.client import PortalClient, PortalNotFound, PortalUnavailable, RateLimiter
from app.portal.signing import canonical_string, sign


@pytest.fixture
def client(settings, transport, no_sleep, monkeypatch):
    monkeypatch.setattr(PortalClient, "_backoff", lambda self, attempt, base: 0.0)
    return PortalClient(settings, transport=transport, limiter=RateLimiter(1000), sleep=no_sleep)


def test_signature_matches_portal_js_algorithm():
    # Same algorithm as the portal bundle: HMAC-SHA256 over METHOD\nPATH\nQUERY\nTS, hex.
    assert canonical_string("get", "/portal/export", "page=1", "1700000000") == "GET\n/portal/export\npage=1\n1700000000"
    assert len(sign("s", "GET", "/portal/export", "page=1", "1700000000")) == 64


async def test_signed_export_and_pagination(client, fake_portal):
    meters = await client.export_meters()
    assert len(meters) == len(fake_portal.export)
    dts = await client.list_transformers()
    assert len(dts) == 40 and fake_portal.calls.count("/portal/dts") == 2


async def test_relogin_when_session_expires(client, fake_portal):
    await client.list_transformers()
    fake_portal.expire_sessions()
    await client.meter_geo("J100000")
    assert fake_portal.logins == 2


async def test_retries_on_429_and_500(client, fake_portal):
    fake_portal.fail_next = [429, 500, 429]
    geo = await client.meter_geo("J100000")
    assert geo["latitude"]
    assert client.stats.rate_limited == 2 and client.stats.retries == 3


async def test_gives_up_after_max_retries(client, fake_portal):
    fake_portal.fail_next = [500] * 10
    with pytest.raises(PortalUnavailable):
        await client.meter_geo("J100000")


async def test_not_found(client):
    with pytest.raises(PortalNotFound):
        await client.meter_energy("NOPE")


async def test_secret_refresh_on_signature_error(client, fake_portal):
    client._secret = "stale-secret"
    await client.signing_secret()  # cached stale value
    await client._ensure_session()
    meters = await client.export_meters()
    assert meters  # first attempt 401 signature_invalid -> refetch /portal/keys -> retry


async def test_crawl_fallback_matches_export_shape(client, fake_portal):
    crawled = {m["meterId"]: m for m in await client.crawl_meters()}
    for m in fake_portal.export:
        c = crawled[m["meterId"]]
        assert c["hierarchy"]["dt"]["code"] == m["hierarchy"]["dt"]["code"]
        assert c["installType"] == m["installType"]
        assert abs(c["geo"]["lat"] - m["geo"]["lat"]) < 1e-9
