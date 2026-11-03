# Urja Meter API

Urja Meter Ops is a legacy portal for smart-meter data, and it does not provide an API that other applications can use directly. This project adds a REST API in front of the portal, along with a small web console for exploring the data.

The portal is useful when someone needs to look up a meter manually. It becomes harder to work with when another service or engineer needs to query many meters, apply filters, or analyse readings. This API is intended to make those tasks easier. It supports attribute-based filtering, location searches, network-hierarchy queries, cleaned consumption readings, and anomaly checks that are not available in the portal itself.

The repository includes a few documents that explain the implementation in more detail:

| Document | What it covers |
|---|---|
| `README.md` | The implementation, setup instructions, design choices, assumptions, trade-offs, and possible next steps |
| [PROTOCOL.md](PROTOCOL.md) | How the portal works, including authentication, hidden endpoints, signed bulk export, rate limits, and data quirks |
| [openapi.json](openapi.json) | The OpenAPI 3.1 specification for this API. It is also available at `/reference` (Scalar) and `/docs` (Swagger UI) when the service is running |
| [REFLECTION.md](REFLECTION.md) | Responses to the assignment's reflection questions |

---

## Contents

1. [Quick start](#quick-start)
2. [What I built](#what-i-built)
3. [Optional extensions](#optional-extensions)
4. [API tour](#api-tour)
5. [How it's structured](#how-its-structured)
6. [Design decisions and trade-offs](#design-decisions-and-trade-offs)
7. [Assumptions](#assumptions)
8. [What I intentionally left out](#what-i-intentionally-left-out)
9. [What I'd improve with more time](#what-id-improve-with-more-time)
10. [Configuration reference](#configuration-reference)
11. [Testing](#testing)

---

## Quick start

You only need **Python 3.11 or later** to run the service. The web console loads Chart.js and Leaflet from a CDN, so there is no separate frontend build step.

Clone the repository and install the project dependencies:

```bash
git clone <this-repo> urja-meter-api
cd urja-meter-api
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Open `.env` and set `URJA_USERNAME` and `URJA_PASSWORD` to the portal credentials provided with the assignment. The credentials stay out of the repository: `.env` is included in `.gitignore`.

Start the API:

```bash
uvicorn app.main:app --port 8000
```

Once it is running, these are the main URLs:

| URL | What you'll find |
|---|---|
| `http://localhost:8000/` | Web console with the overview, map, meter explorer, and network hierarchy |
| `http://localhost:8000/reference` | API reference powered by Scalar |
| `http://localhost:8000/docs` | Swagger UI, where you can try API requests |
| `http://localhost:8000/v1/health` | Service health and data-freshness information |

At startup, the service logs in to the portal, downloads the full meter inventory through its signed bulk-export endpoint, and builds a local index. The export takes about one second. A background job then starts collecting recent readings for all meters at 30 requests per minute, which is comfortably below the portal's rate limit. These readings power `/v1/insights`; the console gradually fills in as the job runs. Warming the readings cache for all 403 meters takes about 14 minutes.

The inventory and readings are stored in `data/urja.sqlite`. Because the data is kept locally, the service can restart without having to rebuild everything from scratch.

### Run with Docker

The service can also be built and started with Docker:

```bash
docker build -t urja-meter-api .
docker run -p 8000:8000 --env-file .env -v urja-data:/data urja-meter-api
```

---

## What I built

I split the implementation into a few focused parts so the portal-specific behaviour stays separate from the API that consumers use.

- **Portal client (`app/portal/`)** — Handles login through better-auth's JSON endpoint and signs bulk-export requests with HMAC-SHA256, following the same process as the portal's JavaScript client. It keeps requests below the portal's undocumented limit of roughly 120 requests per minute and handles expired sessions, `401`, `429`, and `5xx` responses, as well as network failures. It also includes a circuit breaker to avoid repeatedly calling an unavailable portal and clock-skew correction to keep request signatures valid.
- **Data normalisation (`app/normalize.py`)** — Converts the portal's string values into typed records and changes its `DD/MM/YYYY` timestamps into ISO 8601 timestamps with an IST offset. It merges duplicate readings, handles both the legacy and v2 detail formats, and calculates interval consumption from cumulative register values. The portal stores the cumulative values, not the consumption deltas most API users need.
- **Network hierarchy (`app/hierarchy.py`)** — Rebuilds a consistent Zone-to-DT hierarchy from the copy of the hierarchy stored on every meter. Those copies do not always agree, so this component resolves blank codes and names where the correct pairing is unambiguous, uses the transformer master list where appropriate, and records each repair. The report is available through `/v1/data-quality`.
- **Local index (`app/store.py`)** — The portal's own search is limited to meter IDs and serial-number substrings. The SQLite index adds filters on other attributes, network-subtree searches, bounding-box and nearby-location queries, and facet counts. It also stores readings by day and applies freshness rules to the cache.
- **REST API (`app/routes/`, FastAPI)** — Provides 12 versioned endpoints under `/v1/`, with consistent response envelopes, pagination, optional API-key authentication, and error responses following RFC 9457.
- **Web console (`web/`)** — A lightweight frontend with key metrics, a daily-consumption distribution, an anomaly feed, estimated transformer loading, a meter map, a filterable meter table, per-meter consumption charts, and a network-hierarchy explorer. It has no separate build step.
- **Tests (`tests/`)** — Includes 37 tests that use a fake portal reproducing the behaviour that matters for this integration, such as request signing, session expiry, rate limits, duplicate readings, and SvelteKit devalue pages. The tests do not need a live network connection.

---

## Optional extensions

All six optional extensions are implemented. The table below summarises what each one does and where to find its implementation.

| Extension | What it does | Where to look |
|---|---|---|
| **Network hierarchy** | Rebuilds the tree from each meter's Zone-to-DT chain. Blank codes and names are repaired only when the correct pairing is unambiguous. The transformer master list takes precedence over stale names on individual meter records, such as `Old Malviya Nagar Xfmr`. Since codes like `D-01` can appear under different parents, each node is identified by its full path. Every repair is reported. | `app/hierarchy.py` · `/v1/network/tree` · `/v1/network/nodes/{path}` · `network_node=` filter · `/v1/data-quality` · [PROTOCOL §6](PROTOCOL.md#6-data-model--quirks) · Network tab |
| **Local index and query layer** | Uses SQLite to support queries the portal cannot handle: attribute filters, network-subtree searches, bounding boxes, nearby searches through `near=lat,lng&radius_km=`, distance sorting, and facet counts. | `app/store.py` · `/v1/meters` · `/v1/meters/facets` |
| **Scale note** | Describes where this local-index approach may become difficult to maintain at roughly 50,000 meters, one million meters, or with many API consumers, and what I would change at each stage. | [Where the local index would start to struggle](#where-the-local-index-would-start-to-struggle) |
| **Freshness and caching** | Refreshes the inventory every 15 minutes. If the portal is unavailable, the API continues serving the last successful snapshot and marks it as `stale`. Readings are cached per meter and day. A day is considered closed once it is more than a day older than the meter's newest reading and is cached for 24 hours; recent days are fetched again after 10 minutes. Freshness is based on the dates in the data rather than the current date, because the available readings end in June 2026. Every response includes freshness information in `meta`. | `app/service.py` · [Design decision 3](#3-cache-readings-by-day-using-the-data-to-judge-freshness) |
| **Robustness** | Re-authenticates before the one-hour session expires and whenever it receives a `401`. A shared cool-down and exponential back-off handle `429` responses; the portal does not send a `Retry-After` header. The client also retries server and network errors, uses a circuit breaker, and corrects clock skew for export signatures. Tests cover session expiry, `429`/`500` sequences, secret rotation, and serving stale data when the portal is unavailable. | `app/portal/client.py` · `tests/test_client.py` · `tests/test_api.py` |
| **Full dataset and bulk path** | The signed `/portal/export` endpoint returns all 403 meters, including hierarchy and coordinates, in two requests. Crawling the individual pages would take roughly 827 requests. If export fails while the local cache is empty, the service automatically falls back to crawling. A test checks that the crawl produces the same data shape as the export. | [PROTOCOL §4](PROTOCOL.md#4-the-bulk-path-signed-export) · `export_meters()` and `crawl_meters()` in `app/portal/client.py` |
| **Modern web client** | Provides KPIs, a distribution of daily consumption per meter, a “needs attention” feed for cases such as decommissioned meters that still consume power or faulty meters that continue reporting, estimated transformer loading, a status-coloured meter map, a filterable table, daily and interval charts in the meter drawer, and a hierarchy explorer. It also works at phone width and has no build step. | `web/` · `http://localhost:8000` |

### Known limits

- The proactive re-login at 55 minutes is covered by a simulated session-expiry test. I have not yet run the service against the live portal continuously for more than an hour.
- `/v1/insights` only includes meters whose readings have reached the local cache. The background warm-up takes about 14 minutes to cover all 403 meters, and the response reports how much of the dataset is currently covered.

---

## API tour

All endpoints return JSON. Timestamps use ISO 8601 with the IST offset (`+05:30`), and errors use `application/problem+json`. List endpoints follow the `{data, page, meta}` response format. The `meta` object also tells the caller how fresh the underlying data is.

### List and filter meters

For example, this request returns faulty, three-phase Genus meters:

```bash
curl 'http://localhost:8000/v1/meters?status=faulty&make=Genus&phase=three&limit=2'
```

A sample response looks like this:

```json
{
  "data": [
    {
      "meter_id": "J100014",
      "serial_no": "GE52551",
      "make": "Genus",
      "phase": "three",
      "status": "faulty",
      "install_type": "whole_current",
      "record_format": "legacy",
      "transformer_code": "DT-015",
      "network": {
        "zone":        {"id": "Z-03", "code": "Z-03", "name": "Jaipur Zone 3", "alias": null},
        "circle":      {"id": "Z-03/C-03", "code": "C-03", "name": "Circle 3", "alias": null},
        "division":    {"id": "Z-03/C-03/D-05", "code": "D-05", "name": "Division 5", "alias": null},
        "subdivision": {"id": "Z-03/C-03/D-05/SD-01", "code": "SD-01", "name": "Subdivision 1", "alias": null},
        "substation":  {"id": "Z-03/C-03/D-05/SD-01/SS-15", "code": "SS-15", "name": "Substation 15", "alias": null},
        "feeder":      {"id": "Z-03/C-03/D-05/SD-01/SS-15/F-015", "code": "F-015", "name": "Feeder 15", "alias": null},
        "transformer": {"id": "Z-03/C-03/D-05/SD-01/SS-15/F-015/DT-015", "code": "DT-015", "name": "Bani Park DT 15", "alias": null}
      },
      "network_path": "Z-03/C-03/D-05/SD-01/SS-15/F-015/DT-015",
      "location": {"lat": 27.035683189779615, "lng": 75.83062514945418},
      "data_quality": [],
      "distance_km": null
    }
  ],
  "page": {"total": 7, "limit": 2, "offset": 0, "next_offset": 2},
  "meta": {
    "synced_at": "2026-11-03T11:52:37.376401+05:30",
    "age_s": 41.2,
    "stale": false,
    "source": "export",
    "last_sync_error": null
  }
}
```

Filters can be repeated or supplied as comma-separated values. For example, `status=installed,faulty` matches either status. Other available filters include:

- `q` — substring search across the meter ID or serial number
- `transformer`, `feeder`, `substation`, `subdivision`, `division`, `circle`, and `zone`
- `install_type` and `record_format`
- `network_node` — includes meters beneath a hierarchy node
- `bbox=s,w,n,e` — limits results to a geographic bounding box
- `sort` — sorts the results; prefix a field with `-` for descending order
- `limit` and `offset` — control pagination

### Find meters near a location

The `near` parameter accepts latitude and longitude. `radius_km` sets the search radius:

```bash
curl 'http://localhost:8000/v1/meters?near=26.91,75.79&radius_km=1.5'
```

The API returns the nearest meters first and includes a `distance_km` value for each result.

### Turn readings into consumption

By default, the readings endpoint returns the seven days ending at the meter's latest reading, matching the portal's default window. You can also request a specific date range and ask for a daily roll-up:

```bash
# Default: the seven days ending at the meter's latest reading
curl 'http://localhost:8000/v1/meters/J100000/readings'

# Daily roll-up for an inclusive date range (IST)
curl 'http://localhost:8000/v1/meters/J100000/readings?from=2026-06-01&to=2026-06-07&granularity=daily'
```

A shortened sample response for the daily request is shown below:

```json
{
  "meter_id": "J100000",
  "from": "2026-06-01",
  "to": "2026-06-07",
  "granularity": "daily",
  "summary": {
    "consumption_kwh": 142.05,
    "apparent_energy_kvah": 153.41,
    "power_factor": 0.926,
    "avg_daily_kwh": 20.293,
    "peak_day": "2026-06-01",
    "readings": 336,
    "cadence_minutes": 30,
    "first_reading": "2026-06-01T00:00:00+05:30",
    "last_reading": "2026-06-07T23:30:00+05:30",
    "voltage_min": 220.0,
    "voltage_max": 240.0,
    "voltage_out_of_band": 0,
    "latest_register_kwh": 48114.05
  },
  "data": [
    {
      "date": "2026-06-01",
      "kwh": 20.3,
      "kvah": 21.91,
      "power_factor": 0.927,
      "intervals": 48,
      "completeness": 1.0,
      "voltage_min": 221.0,
      "voltage_max": 240.0,
      "voltage_avg": 229.6
    },
    "... 6 more days ..."
  ],
  "meta": {
    "timezone": "Asia/Kolkata",
    "cache": "miss",
    "portal_calls": 1,
    "cleaning": {
      "duplicates_merged": 0,
      "unparseable_timestamps": 0,
      "blank_values": 0
    }
  }
}
```

Use `granularity=raw` when you need the individual readings. Each row then includes the original register value (`kwh_register`), the consumption calculated for that interval (`kwh`), its `interval_start`, and any flags raised during processing. Examples include `missing_kwh`, `voltage_out_of_band`, and `register_decreased`.

### Explore the network and transformers

These requests cover the hierarchy tree, a particular node and its subtree, and transformer information:

```bash
curl 'http://localhost:8000/v1/network/tree?depth=2'          # Zones and circles, with meter counts by status
curl 'http://localhost:8000/v1/network/nodes/Z-01/C-01'       # A node, its subtree, and its meters
curl 'http://localhost:8000/v1/transformers?feeder=F-001'
curl 'http://localhost:8000/v1/transformers/DT-007'           # Upstream chain, meters, and aliases
```

### View insights and data quality

The insights endpoint returns the available anomalies, top consumers, transformer-loading estimates, consumption histogram, and reading coverage. The data-quality endpoint explains the repairs made while processing the portal's records:

```bash
curl 'http://localhost:8000/v1/insights'
curl 'http://localhost:8000/v1/data-quality'
```

### Health and operations

Use the health endpoint to check whether the service is `ok`, `degraded`, or `starting`, along with data freshness and portal-client statistics. The admin sync endpoint forces an inventory refresh:

```bash
curl 'http://localhost:8000/v1/health'
curl -X POST 'http://localhost:8000/v1/admin/sync'
```

The full endpoint list and schemas are available in [openapi.json](openapi.json). After changing the code, regenerate the specification with:

```bash
python scripts/export_openapi.py
```

---

## How it's structured

The project is organised so that the HTTP layer, portal integration, data processing, and local storage each have a clear responsibility.

```text
app/
  main.py            FastAPI setup, error handling (RFC 9457), docs, and static web files
  config.py          Settings loaded from environment variables or .env
  models.py          Pydantic response schemas used to generate OpenAPI
  service.py         Sync orchestration, freshness rules, readings cache, insights, and warm-up job
  normalize.py       Raw portal payloads converted to typed records; register reads converted to consumption
  hierarchy.py       Network-tree reconstruction and repair, plus the data-quality report
  store.py           SQLite index for meters, transformers, readings, and per-day coverage
  portal/
    client.py        Authentication, pacing, retries, circuit breaker, clock-skew handling, endpoint wrappers, and crawl fallback
    signing.py       HMAC request signing for /portal/export
    devalue.py       Decoder for SvelteKit __data.json, used by the crawl fallback
  routes/            HTTP route modules, including meters.py, network.py, and deps.py
web/                 Console built with vanilla JavaScript, Chart.js, and Leaflet
tests/               37 tests using an in-process fake portal (tests/conftest.py)
scripts/export_openapi.py
```

### What happens when a request comes in

Most inventory requests are served directly from SQLite. The portal is only involved when the local data is missing or stale.

```text
client ──▶ FastAPI route ──▶ Service ──▶ Store (SQLite) ── hit ──▶ response
                                  │                  ▲
                                  └── miss / stale ──┤
                                                     │
                                       PortalClient ─┴─▶ Urja portal
                                       (paced, retried, re-authenticated)
```

A background loop refreshes the inventory, so an ordinary inventory query does not need to call the portal. For readings, the service requests only the days that are missing or stale in the local cache.

---

## Design decisions and trade-offs

The implementation involved a few choices that are worth explaining, particularly because the portal has limited search capabilities and undocumented behaviour.

### 1. Use the signed bulk export instead of scraping

The portal's **Export all meters** button calls `/portal/export`. That endpoint expects an HMAC signature generated by the browser. I reproduced the signing process so the service can retrieve all 403 meters, including their hierarchy and coordinates, in **two requests instead of roughly 827**.

The downside is that the signing scheme is undocumented and could change. To make that less fragile, the client fetches the secret again when it receives `signature_invalid`. If the export still fails and there is no cached inventory, it falls back to crawling the search, detail, and geo pages. The fallback is covered by tests against the fake portal.

### 2. Keep a local copy of the inventory

The inventory is relatively small (231 KB) and does not change frequently. The portal is rate-limited and only supports substring searches, so querying it every time would make the API slower and less flexible.

Instead, the service refreshes the inventory every 15 minutes and serves inventory queries from SQLite. This makes queries fast, supports the additional filters, and allows the API to continue serving the last known snapshot during a portal outage.

The trade-off is that the inventory can be up to 15 minutes old. Each response includes `meta.synced_at`, `age_s`, and `stale`, so a caller can decide whether the returned data is fresh enough for its use case.

### 3. Cache readings by day, using the data to judge freshness

For each meter, a day is treated as **closed** when it is more than one day older than that meter's latest reading. Closed days are cached for 24 hours. The **open** days—the most recent two days and any days after the latest reading—are checked again after 10 minutes.

When a caller requests a date range, the service fetches only the days that are missing or stale. It can request those days in one contiguous portal call because the portal does not impose a window cap.

I chose not to base this rule on the server's current date. The portal's readings currently stop at 30 June 2026, even though the calendar has moved on to November. A wall-clock-based rule would incorrectly treat all available days as closed and could miss new data if the portal starts publishing readings again.

### 4. Return consumption, not only register readings

The portal provides cumulative register values. In most cases, API consumers need to know how much energy was used during an interval, so the normalised response includes the difference between consecutive register readings and the interval it represents.

With `granularity=daily`, those interval values are grouped by day and accompanied by a `completeness` ratio. The original register values remain available through `kwh_register`, so the transformation does not discard the source data. If a register value decreases, the API returns a `null` delta and a flag rather than reporting negative consumption.

### 5. Identify hierarchy nodes by their full path

A code such as `D-01` can appear under more than one circle. At the same time, the records for a transformer point to one consistent chain of parent nodes. Merging nodes using the code alone could therefore create cycles or make unrelated nodes appear to share a parent.

For that reason, a node's `id` is its full path, such as `Z-01/C-01/D-01`. The original code is still available separately as `code`. Blank codes and names are repaired only when the correct pairing is unambiguous across the data. For transformer names, the master list takes precedence over names copied into individual meter records.

These changes are not hidden. Each affected meter includes its repairs in `data_quality`, and `/v1/data-quality` provides the fleet-wide report.

### 6. Keep the portal client within its limits

The client limits itself to 90 requests per minute, while the portal appears to allow roughly 120. A `429` response pauses all in-flight portal requests because the limit is shared across the session.

The background readings warm-up is limited further to 30 requests per minute. That leaves room for interactive requests while the cache is being populated. Since the portal does not return a `Retry-After` header, the client uses exponential back-off with jitter.

### 7. Validate inputs more strictly than the portal

The portal can silently ignore an invalid date and return its default date range. That makes a typo look like a successful request, even though the caller did not get the data it asked for.

This API validates dates and returns `422` with an explanation when an input is invalid. It returns `404` for unknown meters or network nodes, and `503` with `Retry-After` while the service is starting or when the portal is unavailable.

### 8. Keep the stack straightforward

FastAPI and Pydantic generate the OpenAPI specification from the application schemas, which helps keep the documentation aligned with the code. The portal client uses asynchronous `httpx` with a pluggable transport; that makes it possible to test portal behaviour through a fake transport rather than making network calls.

SQLite is enough for the current dataset and does not require a separate database service. The console uses vanilla JavaScript, so someone reviewing or running the project does not need Node.js just for the frontend.

### Other trade-offs

- All API callers currently share one portal session. This keeps the integration simple and within the portal's limits, but every caller effectively uses the same portal account. I discuss how I would separate this in the improvements section.
- `/v1/insights` only covers meters whose readings have been cached. The response reports its coverage rather than presenting partial results as complete.
- Transformer loading is an estimate, not a measurement of peak demand. The calculation converts average daily kWh into average kW by dividing by 24, then converts that value to kVA using the observed power factor. It is useful for comparing relative loading, but it should not be read as peak demand.

---

## Assumptions

A few details are not explicit in the portal's data, so the implementation makes the following assumptions:

- **Timestamps use IST (`Asia/Kolkata`).** The portal does not include a timezone, and the network is in Jaipur.
- **`kwh` and `kvah` are cumulative registers.** They are expected to increase over time. The observed interval differences are plausible—for example, around 0.4 kWh per half hour and 6–26 kWh per day.
- **Consumption belongs to the interval ending at the reading's timestamp.** For a daily meter, the reading at 00:00 on 2 June minus the reading at 00:00 on 1 June represents consumption on 1 June. As a result, the final day in a daily meter's data does not have a consumption value until the next day's reading arrives.
- **The transformer master list (`/portal/dts`) is authoritative for transformer names and capacities.** Names on individual meter records can be outdated, as in `Old Malviya Nagar Xfmr`.
- **A meter belongs to the network branch assigned to its transformer.** The DT represents the physical connection. If a meter's upstream hierarchy disagrees with the majority of meters assigned to that DT, the DT's hierarchy is used.
- **Hierarchy codes are unique only within their parent.** This interpretation best matches the available data; see [PROTOCOL §6](PROTOCOL.md#6-data-model--quirks).
- **Status values are preserved as supplied.** For example, a meter marked `Decommissioned` that continues to report consumption is flagged, not silently reclassified. Whether the status or the readings are incorrect is a business decision.
- **The normal voltage band is 230 V ± 10% (207–253 V).** This is the assumed low-voltage supply range for the checks in this project.
- **The API is read-only.** The portal does not provide write operations, and this API does not add any. The exception is `/admin/sync`, which only refreshes the local cache.

---

## What I intentionally left out

I kept the scope focused on making the portal's data reliable and straightforward to query. A few features are therefore outside the current implementation:

- **Per-user portal authentication or passthrough.** Supporting this properly would require deciding how API users map to portal users. With one shared operations account, adding that layer would increase complexity without providing much benefit.
- **Full API authentication with OAuth, JWT, and roles.** The service currently supports an optional static `X-API-Key`, which is enough for the intended service-to-service use and could be replaced by an API gateway later.
- **Write operations, webhooks, and streaming.** The portal is read-only, and there is no event stream to expose.
- **Postgres, Redis, and a task queue.** For 403 meters and 30 days of readings, those services would add operational overhead without solving a current problem. The scale notes below describe when they may become useful.
- **Raw portal-payload endpoints.** There is intentionally no `/raw` passthrough. The API is meant to give consumers a stable, cleaned-up interface rather than expose the portal's internal response format.
- **CSV and Parquet exports.** These would be useful additions, but I prioritised data correctness and hierarchy reconstruction for this version.
- **A fully polished frontend.** The console is there to make the data easier to inspect and demonstrate how the API can be used. It is not intended to be a finished product; it does not currently include component tests or internationalisation.

---

## What I'd improve with more time

There are a few areas I would take further if I continued developing the project:

- **Improve the spatial index.** I would look at SQLite R*Tree or PostGIS for the location-based queries, as discussed in the next section.
- **Add incremental inventory sync.** The current export replaces the inventory as a whole. If the portal exposed change timestamps—or if a per-meter hash proved reliable—I could identify changed records and emit change events instead of always processing the full export.
- **Add per-consumer rate budgets and HTTP caching headers.** ETag and `Cache-Control` support would make it easier for API consumers to avoid requesting unchanged data.
- **Improve observability.** Prometheus metrics for portal latency, `429` responses, and cache hit ratios, together with structured logs and request IDs, would make production issues easier to diagnose.
- **Run contract tests against the live portal.** A scheduled test could detect changes to response formats, SvelteKit `build` values, or the signing process. I would also add an alert for schema drift.
- **Expand anomaly detection once there is enough data to establish normal variation.** Possible checks include consumption z-scores per DT, energy balance between the sum of connected meters and a DT's own meter (if one becomes available), phase imbalance for three-phase meters, and missing-reading gaps.
- **Add more consumer-facing endpoints.** Useful additions would include `/v1/meters/{id}/readings.csv`, a bulk-readings endpoint for multiple meters, and aggregate readings per feeder and transformer.

### Where the local index would start to struggle

The current SQLite-based approach is a good fit for the supplied dataset. The trade-offs change as the number of meters, readings, or API consumers grows:

| Scale | What is likely to become a problem | What I would change |
|---|---|---|
| **Around 50,000 meters** | The bulk export could grow to roughly 30 MB per sync. Nearby searches currently use a bounding-box pre-filter followed by the Python haversine calculation, which is still reasonable at this scale, but the hierarchy tree is rebuilt in Python for each request. | Cache the built hierarchy tree, use SQLite R*Tree for geographic lookups, and stream-parse the export. |
| **Around 1 million meters with 30-minute readings** | Readings could reach roughly 1.4 billion rows per month. Shared-connection SQLite queries and per-request roll-ups would become slow. At the current rate limit, warming one million meters would take about 7.7 days at 90 requests per minute. | Move readings to Postgres with TimescaleDB, or to ClickHouse, and use continuous daily aggregates. Ask the utility for a bulk readings feed and fetch individual readings only when consumers request them. |
| **Many API consumers** | A single portal session and shared rate budget would become a bottleneck. | Separate sync workers from API replicas, introduce a shared cache such as Redis or Postgres, and schedule portal requests through a queue. |

---

## Configuration reference

The service reads its configuration from environment variables and, when present, from `.env`. The available variables are also listed in [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `URJA_PORTAL_URL` | `https://urja-ops.flockenergy.tech` | Base URL of the portal |
| `URJA_USERNAME` / `URJA_PASSWORD` | Required | Portal login credentials |
| `URJA_API_KEYS` | Empty (API is open) | Comma-separated API keys accepted through `X-API-Key` |
| `URJA_PORTAL_RATE_PER_MINUTE` | `90` | Client-side request limit; the portal allows roughly 120 per minute |
| `URJA_PORTAL_CONCURRENCY` | `4` | Maximum number of parallel portal requests |
| `URJA_INVENTORY_TTL_S` | `900` | Inventory refresh interval in seconds |
| `URJA_READINGS_RECENT_TTL_S` | `600` | Cache lifetime for recent, open reading days |
| `URJA_READINGS_CLOSED_TTL_S` | `86400` | Cache lifetime for closed reading days |
| `URJA_WARM_READINGS` | `true` | Whether to warm the recent-readings cache in the background for insights |
| `URJA_WARM_RATE_PER_MINUTE` | `30` | Request rate for the background readings warm-up |
| `URJA_DB_PATH` | `data/urja.sqlite` | SQLite cache location; use `:memory:` to avoid a file-backed database |
| `URJA_SYNC_ON_STARTUP` | `true` | Whether to start the background inventory sync when the service boots |

---

## Testing

Run the test suite with:

```bash
pytest -q
```

There are **37 tests**, and they take about half a second to run without making network requests. The fake portal in `tests/conftest.py` uses `httpx.MockTransport`. It checks real HMAC signatures, can expire sessions, returns `429` and `500` responses when requested, ignores invalid dates, produces duplicate and blank readings, and serves detail pages encoded with SvelteKit devalue.

The tests cover:

- The signing algorithm, re-login after session expiry, retry and back-off behaviour, retry exhaustion, and secret refresh.
- The crawl fallback, including a check that it produces the same data shape as the bulk export.
- Parsing, de-duplication, consumption deltas, register rollbacks, both detail formats, and devalue decoding.
- Hierarchy repair, transformer aliases, path-based node IDs, majority voting, and tree counts.
- API filters, pagination, proximity searches, network nodes, reading windows, caching, validation, and `404`/`422`/`503` responses.
- API-key enforcement and the ability to serve stale data when the portal is unavailable.
- A concurrency regression test for the shared SQLite connection.

I also tested the implementation manually against the live portal. That included a full inventory sync, fetching readings for all 403 meters, checking the console in a browser, and verifying that the rate limiter stays below the portal's actual limit.