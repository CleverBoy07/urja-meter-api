# PROTOCOL.md — how the Urja Meter Ops portal actually works

What I found by exploring the running portal (`https://urja-ops.flockenergy.tech`) with
a browser, `curl`, and by reading its JavaScript bundles. Everything here was verified
against the live instance on 2026‑11‑03. Credentials and the signing secret are
intentionally **not** reproduced here.

---

## 1. What the portal is

* A **SvelteKit** app (the HTML has `x-sveltekit-page`, `/_app/immutable/...` bundles and
  `__sveltekit_*` boot code). Tailwind for styling. Auth is **better-auth**.
* Four screens: **Sign in**, **Meters** (searchable list), **Meter detail**, **Transformers**
  (list + an "Export all meters" button).
* The UI looks server-rendered, but the meters list renders **"0 total / No meters found"**
  in the initial HTML. The data is fetched client-side from JSON endpoints under
  **`/portal/*`**. That is the API the portal doesn't advertise.

Route table, taken from the client bundle (`app.*.js`, `dictionary`):

| Page route | What loads it |
|---|---|
| `/login` | form action (POST) |
| `/(portal)/meters` | client fetch → `/portal/meters/search` |
| `/(portal)/meters/[id]` | **server load** (nameplate + hierarchy) + client fetches → `/portal/meters/{id}/geo`, `/energy` |
| `/(portal)/transformers` | client fetch → `/portal/dts`; export button → `/portal/keys` + `/portal/export` |

`robots.txt` allows everything. There is no `/api` index, no OpenAPI, and no sitemap.

---

## 2. Authentication

**Session cookie:** `__Secure-better-auth.session_token` (HttpOnly, Secure, SameSite=Lax,
**Max-Age 3600** — one hour, not sliding as far as I could tell).

Three ways to get one:

1. **SvelteKit form action** (what the browser does):
   `POST /login` with form fields `email`, `password`. With headers
   `x-sveltekit-action: true` and `Accept: application/json` it answers
   `{"type":"redirect","status":303,"location":"/meters"}` + `Set-Cookie`.
   Bad password → `{"type":"failure","status":401,"data":"[...\"Invalid email or password.\"]"}`
   (note: `data` is a *devalue-encoded string*, not JSON).
2. **better-auth JSON API** (cleaner, what my client uses):
   `POST /api/auth/sign-in/email` with JSON `{"email","password"}` and an **`Origin`
   header matching the portal** (better-auth's CSRF check). Returns the user object, a
   `token`, and the same cookie.
3. `GET /api/auth/get-session` returns session metadata incl. `expiresAt`, IP, user agent.
   `POST /api/auth/sign-out` ends it.

**Unauthenticated behaviour differs by route type:**
* `/portal/*` JSON endpoints → `401 {"error":"unauthorized","message":"A valid session is required."}`
* page routes (`/meters`, `/meters/{id}/__data.json`) → `302` redirect to `/login`.

---

## 3. Data endpoints

All require the session cookie. All return `application/json`.

### `GET /portal/meters/search?q=&page=N`
Paginated meter list, **20 per page, fixed** (`pageSize` param ignored).
```json
{"data":[{"meterId":"J100000","serialNo":"SE33962","make":"HPL","phaseType":"single",
          "installStatus":"Decommissioned","dtCode":"DT-001"}],
 "total":403,"page":1,"pageSize":20}
```
* `q` is a **case-insensitive substring match on `meterId` and `serialNo` only**.
  `q=Genus`, `q=single` or `q=DT-007` return 0. You can't filter by any other attribute.
* Empty `q` returns everything (403 meters → 21 pages).

### `GET /portal/dts?page=N`
Distribution transformers (DTs), 20 per page, 40 total.
```json
{"data":[{"code":"DT-001","name":"Malviya Nagar DT 1","feederCode":"F-001","capacityKva":100}],
 "total":40,"page":1,"pageSize":20}
```
Page beyond the end → `{"data":[]...}` (not 404).

### `GET /portal/meters/{id}/geo`
```json
{"data":{"latitude":"26.938961002479868","longitude":"75.83095696146852"}}
```
Coordinates are **strings**. Unknown meter → `404 {"error":"not_found","message":"Meter not found"}`.

### `GET /portal/meters/{id}/energy[?from=YYYY-MM-DD&to=YYYY-MM-DD]`
```json
{"data":[{"timestamp":"24/06/2026 00:00","kwh":"48439.16","kvah":"52314.29","voltR":"229"}]}
```
* **Cumulative register reads**, not consumption: `kwh`/`kvah` only ever go up. You
  have to diff consecutive reads to get usage.
* Timestamps are **`DD/MM/YYYY HH:MM`, no timezone**. I assume IST (the network is in Jaipur).
* All values are **strings**; missing values are **`""`**, not null.
* **No params → "last 7 days of data"**, anchored at the meter's latest reading, *not*
  at today. (Data only exists for **June 2026**, so a "today"-based window is empty.)
  For half-hourly meters that's 337 rows: `23/06 23:30 … 30/06 23:30`.
* `from`/`to` are **ISO dates, inclusive**, matched on the reading's date. Only `from`
  → 7 days forward; only `to` → 7 days back. **No maximum window**: a full year in one
  call works (returns all 1,440 June rows).
* **Invalid params are silently ignored.** `from=bad`, `from=2026-06-24T10:00`, or
  `start=01/06/2026` just give you the default window. No error, so a typo looks like
  a successful query.

### `GET /meters/{id}/__data.json?x-sveltekit-invalidated=001`
SvelteKit's data endpoint for the detail page's server `load`. It's encoded in
**devalue** format (a flat array where values are indices into the same array; see
`app/portal/devalue.py`). Decoded, it contains `meterId`, `detail`, and a `hierarchy` map:
```json
{"Zone":"Jaipur Zone 1 (Z-01)","Circle":"Circle 1 (C-01)", "...":"...",
 "DT":"Malviya Nagar DT 1 (DT-001)"}
```
`detail` comes in **two shapes**, depending on the meter's record "build":
* **legacy**: `{"data":[{"parameterName":"Serial No","parameterValue":"SE33962"}, ...]}`
* **v2**: `{"classData":"{\"installed_meter\":{\"SerialNo\":\"SE65293\",...}}"}` (a JSON
  *string* inside JSON, with PascalCase keys).

Unknown meter → HTTP 200 with a node `{"type":"error","status":404}` inside.

---

## 4. The bulk path: signed export

The Transformers page has an **"Export all meters"** button. Its code (in the
`nodes/4.*.js` chunk) does:

1. `GET /portal/keys` → `{"data":{"signingSecret":"<32 chars>"}}`
2. Builds the string `"GET\n/portal/export\npage=1\n<unix-seconds>"`
3. HMAC-SHA256 with the secret, hex-encoded
4. `GET /portal/export?page=1` with headers `x-timestamp: <unix-seconds>` and
   `x-signature: <hex>`

The response has **every meter** (403) with fields the other endpoints scatter across
several calls, or don't expose at all:
```json
{"data":[{"meterId":"J100000","serialNo":"SE33962","make":"HPL","phaseType":"single",
  "installStatus":"Decommissioned","installType":"Whole Current","build":"legacy",
  "dtCode":"DT-001",
  "hierarchy":{"zone":{"name":"Jaipur Zone 1","code":"Z-01"}, "circle":{...}, "division":{...},
               "subdivision":{...}, "substation":{...}, "feeder":{...}, "dt":{...}},
  "geo":{"lat":26.938961002479868,"lng":75.83095696146852}}],
 "total":403}
```
Observations:
* **`page` is ignored.** `page=2`, `page=99` and `pageSize=1000` all return the full set.
* The secret is **the same across sessions** (it's effectively static) but is only
  served to logged-in users. I still fetch it at runtime and re-fetch on a
  `signature_invalid` 401, in case it rotates.
* The timestamp must be **within about ±5 minutes** of server time. Milliseconds are rejected.
* Missing/wrong signature or stale timestamp →
  `401 {"error":"signature_invalid","message":"Missing or invalid request signature."}`
* The empty query string also signs correctly (`""` in the canonical string).
* Geo is **numeric** here but a string in `/geo`. Same data, different types.

**Cost comparison** for the full inventory:

| Path | Requests | Time (at the rate limit) |
|---|---|---|
| Signed export | 2 (`/keys` + `/export`) | ~1 s |
| Search + detail page + geo per meter | 21 + 403 + 403 = **827** | ~7 min |

My service uses the export, and keeps the crawl as an automatic fallback.

---

## 5. Rate limiting

* About **120 requests per 60 s window per session/IP**, shared across *all* endpoints
  (`/portal/dts` and `/portal/meters/*` drain the same bucket). It behaves like a fixed
  window: request #121 failed 27.6 s into my burst, and requests worked again about 33 s later.
* Response: `429 {"error":"rate_limited","message":"Rate limit exceeded; slow down."}`
  with **no `Retry-After`** and no `X-RateLimit-*` headers, so the client has to guess.
* I found this the hard way: my first attempt fetched energy for all meters with 8
  parallel workers and got 321 × 429 out of 668 requests.

---

## 6. Data model & quirks

**Inventory (403 meters, 40 DTs):**

| Field | Values |
|---|---|
| `installStatus` | Installed 238 · Faulty 90 · Decommissioned 75 |
| `make` | HPL 90 · Genus 88 · Secure 85 · Allied 76 · L&T 64 |
| `phaseType` | single 277 · three 126 |
| `installType` | Whole Current 205 · CT Operated 198 |
| `build` | legacy 238 · v2 165 |

**Hierarchy:** Zone › Circle › Division › Subdivision › Substation › Feeder › DT, with
3 / 6 / 10 / 14 / 18 / 28 / 40 distinct *codes* per level. Once nodes are identified by
path, there are 3 / 6 / 30 / 40 / 40 / 40 / 40 *nodes*: below circle level, every DT
has its own chain. The hierarchy is **stored as a full copy on every meter**, not as a
tree, and the copies disagree:

1. **Blank codes or names** on some nodes: circle (6 blank codes, 5 blank names),
   substation (3 blank codes), feeder (5 blank codes, 3 blank names). E.g.
   `{"code":"","name":"Circle 6"}` and `{"code":"C-05","name":""}`. Each name↔code pairing
   is unambiguous elsewhere in the data, so they can be repaired.
2. **A renamed transformer.** DT-007 is "Sanganer DT 7" in `/portal/dts`, but 3 meters
   (J100400–J100402, the last ones added, with a serial prefix `GN` that no other meter
   uses) call it **"Old Malviya Nagar Xfmr"**. DT-007 also has 13 meters while every other DT has 10.
3. **Codes are reused under different parents.** Division `D-01` appears under circles
   C-01, C-03 and C-05; substation `SS-01` under three subdivisions; feeder `F-001`
   under SS-01 *and* SS-11 (DT-029's master record even says its feeder is F-001).
   But **every DT has exactly one consistent upstream chain** (apart from the blanks).
   The consistent reading is that codes are only unique *within their parent* (like
   "Division 1" of each circle), so nodes need a path-based identity. 54 codes are reused this way.
4. Meter IDs `J100000–J100402` are sequential. Serials look like `SE33962` and some are
   `L&84997` (an `&` in a serial, presumably from the make "L&T"). The serial prefix
   doesn't consistently match the make.

**Readings:**

* Only **June 2026** exists (1–30 June). Today's date in the environment is November 2026.
* **Two cadences:** 40 meters (J100000–J100039) have **half-hourly** reads (48/day,
  smart/AMI); the other 363 have **one read per day** at 00:00.
* **Duplicate rows:** 5 meters have the 30/06 00:00 reading twice, once complete and
  once with `kwh:""`, `voltR:""` (kVAh present).
* **Decommissioned meters keep consuming.** All 75 decommissioned meters show
  6–25 kWh/day, the same as installed ones. Faulty meters also report normally. Either
  the status field is stale, or there's a revenue-protection problem. The portal UI
  never shows this, because it never shows status and consumption side by side.
* Power factor (kWh/kVAh) is a constant 0.926 and voltage is always 220–240 V. The data
  is synthetic and clean in these fields. My code still checks them.
* The intra-day load profile is flat (≈0.42 kWh every half hour), again synthetic.

---

## 7. Other things worth knowing

* The server's `Date` header is reliable. My client uses it to estimate clock skew and
  keep export signatures valid.
* Responses are fast (p50 ≈ 100 ms). Rate limiting is the real throughput constraint,
  not latency.
* The `/meters` HTML says "0 total". A naive HTML scraper would conclude the portal is empty.
* `GET /portal/meters` (no `/search`) and `GET /portal/meters/{id}` don't exist (SvelteKit 404 HTML page).
