from concurrent.futures import ThreadPoolExecutor

from app.store import Store, haversine_km


def test_haversine():
    # One degree of latitude on the mean-radius sphere is ~111.19 km.
    assert abs(haversine_km(26.0, 75.8, 27.0, 75.8) - 111.195) < 0.01
    assert haversine_km(26.9, 75.8, 26.9, 75.8) == 0


def test_concurrent_reads_and_writes_do_not_race():
    """Regression: FastAPI runs sync routes on a thread pool sharing one connection."""
    store = Store(":memory:")
    store.set_meta("inventory_synced_at", 1.0)

    def worker(i):
        if i % 5 == 0:
            store.set_meta(f"k{i}", i)
        assert store.get_meta("inventory_synced_at") == 1.0
        store.query_meters(limit=5)
        return store.facets()

    with ThreadPoolExecutor(16) as pool:
        list(pool.map(worker, range(400)))
