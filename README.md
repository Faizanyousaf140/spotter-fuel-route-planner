# Fuel Route Planner: Django REST API

Give it a start and a finish anywhere in the USA. It returns:
* the driving route (GeoJSON) and a map;
* the **cheapest set of fuel stops** for a truck with a **500-mile range** at **10 mpg**;
* **gallons and cost per stop**;
* the **total fuel cost**.

Prices come from the supplied OPIS file (`fuel-prices-for-be-assessment.csv`).

* **Framework:** Django 6.1.3 and Django REST Framework 3.18.3, on Python 3.14.
* **Routing:** the free OSRM demo server, with **one call per new route** and **zero on a cached route**.
* **Station locations:** found once, offline, from US Census data. No geocoding happens at request time.
* **Fuel stops:** chosen by an exact minimum-cost dynamic programme. It is checked against an independent exhaustive solver.

## Contents
1. [Quick start](#quick-start)
2. [API](#api)
3. [How it works](#how-it-works)
4. [Fuel model and assumptions](#fuel-model-and-assumptions)
5. [Fuel-stop optimisation](#fuel-stop-optimisation)
6. [Station data preparation](#station-data-preparation)
7. [Routing and station matching](#routing-and-station-matching)
8. [Performance](#performance)
9. [Testing](#testing)
10. [Postman](#postman)
11. [Configuration](#configuration)
12. [Known limitations](#known-limitations)
13. [Demo: Loom walkthrough script](#demo-loom-walkthrough-script)
14. [Project layout](#project-layout)

---

## Quick start

**Prerequisites:**
* Python **3.14**. Django 6.1 supports 3.12–3.14, and every pinned dependency publishes 3.14 wheels.
* Internet access for the OSRM routing call and the map tiles.

```bash
git clone <repo-url> && cd Spotter_Backend_Django_Assessment
python -m venv .venv
.venv\Scripts\activate                 # Windows   |  source .venv/bin/activate  (macOS/Linux)
pip install -r requirements.txt
copy .env.example .env                 # Windows   |  cp .env.example .env
#   then set DJANGO_SECRET_KEY in .env (any long random string). DJANGO_DEBUG=True is fine locally.
python manage.py migrate
python manage.py createcachetable      # database-backed route cache
python manage.py load_stations         # 6,620 stations from data/processed/fuel_stations.csv
python manage.py runserver
```

Then:

* **Demo form:** <http://127.0.0.1:8000/>. Enter two cities; it opens the saved map.
* **API:** `POST http://127.0.0.1:8000/api/v1/route/`, documented below.
* **Health check:** <http://127.0.0.1:8000/api/v1/health/>

No Census download or geocoding is needed. The cleaned, geocoded station file
is committed.

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/route/` | Plan a route. Returns **201** and a `Location` header. |
| `GET` | `/api/v1/route/<plan_id>/` | A saved plan as JSON. Plans are stored in the database. |
| `GET` | `/api/v1/route/<plan_id>/map/` | The saved plan as a Leaflet + OpenStreetMap map (HTML). |
| `GET` | `/api/v1/health/` | Database and station-data status. Returns 503 if no stations are loaded. |

### Request

```json
POST /api/v1/route/
{ "start": "Chicago, IL", "finish": "Dallas, TX" }
```

* `start` and `finish` accept:
  * `"City, ST"` or `"City, State"` (resolved offline);
  * `"lat,lng"`;
  * an object `{"lat": 41.88, "lon": -87.63}` (`lng` also works);
  * free text, which falls back to the Nominatim geocoder (cached).
* `corridor_miles` (optional, 1–50, default 15): the furthest a station's
  city-level point may be from the route.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/route/ -H "Content-Type: application/json" \
     -d '{"start": "Chicago, IL", "finish": "Dallas, TX"}'
```

### Response

This is a real response, trimmed: the geometry is shortened and 2 of the 3 stops are omitted.

```jsonc
{
  "plan_id": "19563f0f-…",
  "map_url": "http://127.0.0.1:8000/api/v1/route/19563f0f-…/map/",
  "plan_url": "http://127.0.0.1:8000/api/v1/route/19563f0f-…/",
  "start":  { "query": "Chicago, IL", "label": "Chicago, IL", "lat": 41.837045, "lon": -87.684939, "resolved_by": "census_gazetteer" },
  "finish": { "query": "Dallas, TX",  "label": "Dallas, TX",  "lat": 32.793333, "lon": -96.766513, "resolved_by": "census_gazetteer" },
  "route": {
    "provider": "osrm", "distance_miles": 961.5, "duration_hours": 17.04,
    "geometry": { "type": "LineString", "coordinates": [[-87.68488, 41.83705], [-87.68116, 41.6918], "…"] },
    "geometry_points_returned": 596, "geometry_points_from_provider": 9539
  },
  "fuel_stops": [
    {
      "sequence": 1, "opis_id": 67427, "name": "CASEYS #2907", "address": "I-57 & I-62", "city": "Charleston", "state": "MO",
      "location": { "lat": 36.916934, "lon": -89.333912, "precision": "city-level (approximate)" },
      "price_per_gallon": 3.044, "mile_marker": 376.5,
      "distance_from_route_miles": 0.2, "estimated_detour_miles": 0.4,
      "fuel_on_arrival_gallons": 12.33, "gallons_purchased": 7.699, "fuel_on_departure_gallons": 20.02, "cost": 23.43,
      "decision": "reach_cheaper_station",
      "reason": "Bought only enough to reach PIT STOP (Wheatley, AR) at mile 576, which is cheaper ($2.999 vs $3.044/gal)."
    }
    /* … PIT STOP, Wheatley AR (22.531 gal, $67.57); EXTRA MILE TRUCK STOP, Hooks TX (16.128 gal, $45.44) */
  ],
  "fuel_cost": {
    "total_fuel_cost": 306.39,
    "purchased_during_trip":  { "gallons": 46.358, "cost": 136.44, "number_of_stops": 3 },
    "starting_fuel_estimate": {
      "gallons_in_tank_at_start": 50.0, "gallons_used": 50.0, "price_per_gallon": 3.399,
      "valuation_method": "nearest_corridor_station", "estimated_value_of_fuel_used": 169.95,
      "valued_at_station": { "opis_id": 73127, "name": "Gas N Wash", "city": "Chicago", "state": "IL", "mile_marker": 0.0, "distance_from_route_miles": 0.0 }
    },
    "how_total_is_calculated": "starting_fuel_estimate.estimated_value_of_fuel_used + purchased_during_trip.cost",
    "currency": "USD"
  },
  "fuel_usage": {
    "route_distance_miles": 961.5, "estimated_detour_miles": 2.1, "total_miles_driven": 963.6,
    "fuel_burned_gallons": 96.358, "fuel_remaining_at_destination_gallons": 0.0
  },
  "assumptions": { "vehicle_range_miles": 500.0, "vehicle_mpg": 10.0, "tank_capacity_gallons": 50.0, "station_corridor_miles": 15.0, "…": "…" },
  "warnings": [ "Station coordinates are approximate: …", "Prices come from the supplied OPIS file …" ],
  "meta": {
    "map_api_calls": { "routing": 1, "geocoding": 0 },
    "route_cache_hit": false, "stations_loaded": 6620, "corridor_stations": 228, "stations_considered_by_optimizer": 108,
    "timings_ms": { "resolve_locations": 0.2, "routing": 2795.7, "station_matching": 54.4, "fuel_optimization": 19.3, "response_geometry": 574.3, "total_request": 3470.4 }
  }
}
```

**Money is split in two:**
* **`purchased_during_trip`:** money actually spent at the recommended stops.
* **`starting_fuel_estimate`:** the estimated value of the fuel already in the tank.

`total_fuel_cost` is the sum of the two.

### Errors

Every error has the same shape, `{"error": {"code", "message", "details"}}`.

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_request` | `start`/`finish` missing, blank or wrong type; invalid coordinates; `corridor_miles` out of range |
| 400 | `malformed_request` | The body is not valid JSON |
| 415 | `unsupported_media_type` | The body is not `application/json` |
| 405 | `method_not_allowed` | e.g. `GET /api/v1/route/` |
| 404 | `not_found` | Unknown `plan_id` |
| 422 | `location_outside_usa` | Canada or Mexico input, or coordinates not near any US place |
| 422 | `location_not_found` | The text cannot be resolved |
| 422 | `route_not_found` | The provider finds no drivable route (e.g. Los Angeles → Honolulu) |
| 422 | `no_feasible_fuel_plan` | Some stretch longer than 500 miles has no usable station; `details` names the gap |
| 502 | `routing_provider_error` | The provider answered with something unusable |
| 503 | `routing_unavailable` / `geocoder_unavailable` | The provider is unreachable or rate-limited |
| 503 | `station_data_unavailable` | No stations loaded: run `load_stations` |
| 504 | `routing_timeout` | The provider timed out (connect 5 s, read 25 s) |
| 500 | `internal_error` | Unexpected error. It is logged, and no stack trace is returned. |

A failed request never returns a made-up route, and nothing is saved.

## How it works

```
POST /api/v1/route/
  │ serializers.RouteRequestSerializer          validate input (no external call on invalid input)
  ▼
services/planner.py ── orchestrates and times each step
  ├─ geocoding_service   "City, ST" / "lat,lng" → coordinates, offline (Census gazetteer); Nominatim only for free text
  ├─ routing_service     1 OSRM call (overview=full, geojson) → cached in the DB cache; 0 calls on a cache hit
  ├─ station_service     6,620 stations loaded from the DB once per process into numpy arrays
  ├─ route_matching      route resampled every 2 mi → grid + bbox pre-filter → mile marker and off-route distance
  ├─ fuel_optimizer      exact minimum-cost refuelling (dynamic programme)
  └─ cost_calculator     starting-fuel valuation, per-stop gallons and cost, totals (rounded only at output)
  ▼
RoutePlan saved in the DB  →  JSON response + map_url  (/route/<id>/map/ renders from the DB)
```

The place gazetteer and the station index are preloaded when the server starts
(`WARMUP_ON_START`). Because of that, even the first request avoids the
roughly 2 s load.

## Fuel model and assumptions

| Item | Value |
|---|---|
| Maximum range | 500 miles (`VEHICLE_RANGE_MILES`) |
| Fuel economy | 10 miles per gallon (`VEHICLE_MPG`) |
| Tank capacity | 500 / 10 = **50 gallons** |
| Fuel at the start | **Full tank (50 gallons)** |
| Fuel at the destination | May be zero (no reserve is required) |
| Partial fills | Allowed: any amount up to the free tank space |

### Starting fuel (defined before any cost code was written)

1. The vehicle leaves the start with a **full 50-gallon tank**.
2. That fuel is **not free**. It is valued at the **start price**: the price
   of the corridor station nearest the start, where "nearest" is the estimated
   driving distance `mile_marker + off_route_miles`. Ties go to the lower price.
   If the route has no corridor station at all, the average price of all
   loaded stations is used and the response includes a warning.
3. Starting fuel is burned first.
4. **Fuel burned** = (route miles + estimated detour miles) / 10 mpg.
5. **Total fuel cost** = `min(50, gallons burned) × start price` + money
   spent at stops. The optimal plan burns every gallon it buys, so this equals
   the cost of all fuel burned.
6. Fuel left at the destination is reported but **not credited**. Otherwise
   the optimiser would be rewarded for buying cheap fuel it never burns.

Consequences:
* A trip of 500 miles or less needs no stop.
* On longer trips the starting 50 gallons are always fully burned. So
  minimising money spent at stops also minimises the total cost, and the best
  plan arrives with an empty tank.

## Fuel-stop optimisation

`route_api/services/fuel_optimizer.py`. It makes no external calls and uses
only the candidate stations from matching.

**Model.**
* Fuel is counted in miles of range. Capacity U = 500.
* Stops are made **in route order**: the vehicle never backtracks along the
  route.
* Stations sharing a mile marker may be visited **in any order and
  revisited**. This is common at mile 0 and at the destination, where several
  stations project onto the same route point.
* A leg between consecutive stops i → j uses
  `off_i + (mile_j − mile_i) + off_j` miles of fuel. The start and destination
  have off = 0, and **the off values are estimates**.
* Fuel can never be below 0 or above U.
* The plan minimises money spent at stops.

**Algorithm.** This is an exact dynamic programme based on Khuller, Malekian &
Mestre, *"To fill or not to fill: the gas station problem"* (ACM TALG, 2011).
Some optimal plan has this form at every stop u with next stop v:
* if v is cheaper or the same price (or v is the destination), buy just enough
  to reach v;
* if v costs more, fill the tank.

So the fuel on arrival only ever takes a small set of values. The value function
`C(u, g)` = cheapest way to finish after arriving at u with g miles of fuel:

```
C(u,g) = min( −g·p_u + min_{v cheaper/equal, d(u,v) ≥ g} [ d(u,v)·p_u + C(v,0) ],
              (U−g)·p_u + min_{v pricier, d(u,v) ≤ U} C(v, U − d(u,v)) )
```

* `C` is computed backwards along the route. Suffix minima let each `C(u,·)`
  be evaluated in O(log k).
* Stations sharing a mile marker depend on each other, so each such group is
  **relaxed until its values stop improving**. Every hop inside a group burns
  fuel, so this always terminates.
* **Complexity:** O(n·k·log k), where n is the number of candidate stations
  (after merging stations at an identical position, keeping the cheapest) and k
  is the number of stations within range of a station. In practice:
  * New York → Los Angeles: 273 candidates, about 15–80 ms depending on
    machine load;
  * 2,000 synthetic candidates (far denser than any real route): 1.9–2.9 s
    on the loaded test machine.
* **Tie clean-up:** a stop where nothing is bought (possible only through ties)
  is removed and the fuel ledger is re-simulated.
* **No plan possible:** the API returns 422 and names the gap, e.g.
  Anchorage → Seattle: *"no usable fuel station between mile 0.0 and mile
  2178.5"*.

**Verification (tests/test_fuel_optimizer.py).**
* **Hand-calculated cases:**
  * trips under 500 miles, exactly 500 miles, and 500.5 miles;
  * a cheaper station ahead, and no cheaper station ahead;
  * a cheaper but distant station;
  * an expensive detour against a small detour;
  * identical mile markers;
  * a 5-stop route;
  * gaps over 500 miles, and a destination that can't be reached;
  * not enough starting fuel;
  * visiting same-mile stations in any order, and revisiting a station.
* **Independent exact reference:** a Dijkstra search over every (location,
  whole-mile fuel level) state.
  * It allows any order at equal mile markers and unlimited revisits.
  * It assumes nothing about "fill up or arrive empty".
  * With integer distances an optimal plan with whole-mile purchases exists
    (total unimodularity), so the search returns the true optimum.
  * The suite compares the DP with it on **3,500 random cases** (2,000 general
    and 1,500 with many shared mile markers).
  * A one-off sweep added **20,000 more** (5,869 feasible).
  * **0 mismatches.**
* **How the model got here.** Earlier versions were caught by these
  comparisons:
  1. A tie-ordering bug was found and fixed.
  2. A "nearest-to-the-route-first" rule then turned out to cost up to $2.42
     in 2 of 4,000 adversarial cases.
  3. Visits in any order and revisits were then added and verified.
* **Ledger check on every plan:** fuel never below 0, never above capacity,
  every stop buys something, and fuel in + purchases − fuel burned = fuel left.

**Claim.** Under the model above, the optimiser returns the minimum refuelling
cost. Real-road optimality also depends on the estimated detours and
city-level station positions (see [limitations](#known-limitations)).

## Station data preparation

`python manage.py build_station_dataset` is one-time and offline, and its
outputs are committed. `build_place_reference` rebuilds the Census reference
from census.gov.

| Step | Result on the supplied file |
|---|---|
| Rows read and validated | 8,151 valid, 0 rejected |
| Canadian provinces removed | 620 rows |
| Exact duplicate rows removed (after whitespace clean-up) | 26 rows |
| One station per OPIS ID, **average** of its prices (min/max kept) | 6,626 stations (487 had several prices; 227 had name variants) |
| Coordinates from city + state: US Census Gazetteer 2026 (places, then town/township subdivisions) | 3,664 of 3,808 city/state pairs |
| One-time fallback geocoder (Nominatim, single-threaded, ≥1.1 s apart, cached in `data/cache/`) | 139 more pairs (144 cached answers) |
| Not found → dropped and listed in `data/processed/unmatched_cities.csv` | 5 pairs / 6 stations |
| Outside its own state's bounding box → dropped | 0 |
| **Final stations** | **6,620** |

* **Counts:** `data/processed/ingestion_report.json`.
* **Every dropped record and the reason:** `data/processed/rejected_records.csv`.
* **The original CSV is unchanged:** `.gitattributes` turns off line-ending conversion for it.

## Routing and station matching

* **Routing:**
  * The OSRM public demo server (no key), using `overview=full&geometries=geojson`.
  * **One call per new start/finish pair.** Routes are cached in the database
    cache, so a repeated request makes **zero** routing calls. The cache
    survives restarts and is shared between worker processes.
  * Calls are spaced at least 1 s apart per process, following the demo
    server's policy.
  * OpenRouteService can be used instead (`ROUTING_PROVIDER=openrouteservice`,
    `ORS_API_KEY`). That provider is **tested with mocks only**, because no key
    was available.
* **Start/finish:**
  * `"lat,lng"` and `"City, ST"` are resolved **offline**.
  * Text naming Canada, a Canadian province or Mexico is rejected without any
    network call.
  * Coordinates must lie within 25 miles of a US Census place.
* **Matching** (`route_matching.py`):
  1. The full route geometry is resampled to one point every 2 miles. Mile
     markers are scaled to the provider's distance.
  2. Stations are filtered to the route's bounding box plus the corridor, then
     by a grid. Both filters are exact: they never drop an in-corridor station.
  3. Each station is projected onto the route segments next to its nearest
     route point. This gives `distance_from_route_miles` and `mile_marker`.

### Station coordinates are city-level estimates

The price file has no coordinates, and its addresses are highway/exit
descriptions. Every station is therefore placed at its **city's** reference
point. To stay safe:
* the corridor is wide (15 miles);
* every station used is charged a **detour of 2 × distance_from_route**. This
  counts towards reachability and fuel burned. It may overstate distance and
  cost slightly, but never understates them;
* every response carries a warning, and every stop shows its estimated
  distance from the route.

## Performance

Measured on 2026-11-16 over real HTTP, with `runserver` and `DEBUG=False`, on
a Windows laptop. The CPU was already 55–70 % busy with other software, so
treat these as indicative.

| Request | Routing calls | Total time |
|---|---|---|
| New York → Los Angeles, **fresh process, uncached** | 1 | 3,337 ms (OSRM: 2,929 ms) |
| New York → Los Angeles, cached route (5 runs) | 0 | median **240 ms** (181–298) |
| New York → Los Angeles, **after a server restart**, cached route | 0 | 325 ms |
| Chicago → Dallas, uncached / cached | 1 / 0 | 3,485 ms / **118 ms** |
| Washington → New York, uncached / cached | 1 / 0 | 3,350 ms / **57 ms** |
| Seattle → Miami (3,303 mi), uncached / cached | 1 / 0 | 4,038 ms / **206 ms** |
| Saved map page / saved plan JSON | 0 | 108 ms / 39 ms |
| Validation error / outside-USA error | 0 | 32 ms / 19 ms |

* **Uncached requests:** the OSRM call is 85–97 % of the time.
* **Cached requests:** the time goes to station matching (about 10–130 ms) and
  the optimiser (about 2–115 ms). Both are in-memory numpy/Python.
* **Startup:** preloading takes about 1.7–2.2 s at server start instead of on
  the first request.
* **Response size:** 62 KB for New York → Los Angeles. The geometry is
  simplified from 35,357 to 2,607 points; matching uses the full geometry.

## Testing

```bash
python manage.py test tests                                          # 118 offline tests (~10-15 s)
RUN_LIVE_TESTS=1 python manage.py test tests.test_live_integration   # 3 tests that call the real OSRM server
python manage.py check && python manage.py makemigrations --check --dry-run
```

Results of the last run on 2026-11-16: **118 offline tests passed**, plus
**3 live tests passed** (`RUN_LIVE_TESTS=1`). `check` reported no issues.

| Module | Tests | Covers | External services |
|---|---|---|---|
| `test_station_dataset` | 18 | CSV validation, malformed rows, Canada filter, exact duplicates, averaging, gazetteer normalisation and aliases, bounding boxes | none |
| `test_commands` | 5 | `build_station_dataset` → `load_stations` → database, fallback geocoder caching, wrong-state rejection, malformed CSV | **mocked** geocoder |
| `test_geo_and_matching` | 19 | Distances, polyline, simplification, resampling, mile markers, corridor edge, bbox-vs-geometry, many stations | none |
| `test_routing_and_geocoding` | 28 | OSRM/ORS parsing, NoRoute, timeout, 429, 5xx, bad payloads, route cache, location resolution, station index | **mocked** HTTP |
| `test_fuel_optimizer` | 22 | Hand-calculated cases, infeasibility, 3,500 random cases against the exact reference, ledger checks, performance | none |
| `test_cost_calculator` | 6 | Start-price choice, starting fuel kept separate from refuelling, detours in fuel burned, rounding | none |
| `test_api` | 20 | JSON structure, status codes, persistence, saved map after a simulated restart, caching, all error paths | **mocked** OSRM |
| `test_live_integration` | 3 | Real OSRM route and matching, real "no route", full HTTP → OSRM → plan → map, cache hit | **real** OSRM |

The Postman collection was also run end to end with Newman against a live
server: 13 requests, 33 assertions, 0 failures. The map page was rendered in
headless Edge with no JavaScript errors: route line, 9 stop markers, and start
and finish markers.

## Postman

Import `postman/Spotter_Fuel_Route_API.postman_collection.json`. Its
`base_url` variable defaults to `http://127.0.0.1:8000`.

| Folder | Requests |
|---|---|
| 0. Health | health check |
| 1. Successful routes | short route without a stop; multi-stop New York → Los Angeles (saves `plan_id`/`map_url`); cost breakdown; `lat,lng` input; custom corridor; saved plan JSON; saved map HTML |
| 2. Invalid input | missing finish (400), outside USA (422), invalid coordinates (400) |
| 3. Impossible routes | Los Angeles → Honolulu (422 `route_not_found`), Anchorage → Seattle (422 `no_feasible_fuel_plan`) |
| 4. Provider failures | 503 `routing_unavailable` and 504 `routing_timeout`. These need the server started with a broken `OSRM_BASE_URL` (instructions are in the folder). |

* **Test scripts:** every request checks the status code and key fields. The
  successful routes also check that `total = starting fuel estimate +
  purchased during trip`.
* **Example responses:** these are real captures from a local run. Long
  geometry and stop lists are truncated where marked.
* **Running from the command line:**

```bash
npx newman run postman/Spotter_Fuel_Route_API.postman_collection.json \
  --folder "0. Health" --folder "1. Successful routes" --folder "2. Invalid input" --folder "3. Impossible routes" \
  --delay-request 1100
```

## Configuration

All configuration comes from environment variables or `.env`. See `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `DJANGO_SECRET_KEY` | required unless `DJANGO_DEBUG=True` | Django secret key |
| `DJANGO_DEBUG` | `False` | Debug mode, for local use only |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated host names |
| `VEHICLE_RANGE_MILES` / `VEHICLE_MPG` | `500` / `10` | Vehicle model |
| `MAX_OFF_ROUTE_MILES` | `15` | Station corridor width; can be overridden per request with `corridor_miles` |
| `ROUTE_SAMPLE_SPACING_MILES` | `2` | Spacing of route points used for matching |
| `ROUTING_PROVIDER` | `osrm` | `osrm` or `openrouteservice` |
| `OSRM_BASE_URL` | `https://router.project-osrm.org` | Point this at a self-hosted OSRM for production |
| `ORS_BASE_URL` / `ORS_API_KEY` | HeiGIT endpoint / empty | OpenRouteService settings |
| `ROUTING_CONNECT_TIMEOUT` / `ROUTING_READ_TIMEOUT` | `5` / `25` s | Provider timeouts |
| `ROUTING_MIN_INTERVAL_SECONDS` | `1` | Minimum gap between routing calls per process (OSRM demo policy) |
| `ROUTE_CACHE_SECONDS` | `604800` (7 days) | Route cache lifetime |
| `GEOCODER_FALLBACK_ENABLED` | `True` | Use Nominatim for free-text inputs |
| `HTTP_USER_AGENT` | identifying string | Required by the OSM/Nominatim policies; add contact details if you deploy |
| `WARMUP_ON_START` | `True` | Preload the indexes when the server starts |
| `DJANGO_SECURE_SSL_REDIRECT`, `DJANGO_SECURE_HSTS_SECONDS`, `DJANGO_SECURE_COOKIES` | off | HTTPS hardening; enable when served over HTTPS |

`python manage.py check --deploy` reports only the HSTS and SSL-redirect
warnings until those HTTPS settings are switched on.

## Known limitations

* **Station positions are city-level.** Distance from the route and detours
  are straight-line estimates, so the chosen plan is optimal for those
  estimates, not for exact road positions. The 6 stations whose city could not
  be found are excluded.
* **Prices are averaged per OPIS ID.** The file does not say which duplicate
  row is current. Prices are static, not live.
* **Canadian stations are excluded.** US routes that pass through Canada
  (e.g. Alaska ↔ lower 48) can therefore be infeasible. The API reports this
  explicitly.
* **No backtracking along the route.** A station behind the vehicle is never
  used, even when that might be cheaper on a real road network.
* **OSRM demo server.** It has no SLA, is limited to about 1 request per
  second, and is for non-commercial use only. Use a self-hosted OSRM or
  OpenRouteService in production. Uncached requests take 2–4 s, almost all of
  it the routing call.
* **USA check for coordinates.** A point is accepted when a US Census place is
  within 25 miles, so points just across the border can pass.
* **Local server only.** This setup is for local use with `runserver`. A
  production deployment would add a WSGI server, static-file serving and HTTPS.
  Those were left out on purpose for this assessment.

## Demo: Loom walkthrough script

Target 4½ minutes. Before recording, start the server and open Postman with
the collection imported. Have the editor open on `route_api/services/`.

| Time | Show | Say |
|---|---|---|
| 0:00–0:30 | README top | "A Django REST API: start and finish in the USA → route, cheapest fuel stops for a 500-mile, 10-mpg truck, total cost, and a map. One routing call per new route, zero when cached." |
| 0:30–1:15 | Editor: `planner.py`, then the `services/` folder | "The view validates input and calls the planner. It resolves locations offline from Census data and makes one OSRM call, cached in the database. Stations are matched to the actual route geometry, then the optimiser and cost calculator run. The plan is saved, so its map URL survives restarts." |
| 1:15–2:30 | Postman: multi-stop New York → Los Angeles; scroll `fuel_stops`, `fuel_cost`, `meta`. Send again. Open `map_url` in a browser. | "Nine stops, each with gallons, cost and the reason it was chosen. Money spent on the trip is kept separate from the estimated value of the starting tank. `meta` shows 1 routing call. On the second send it's 0, a cache hit, and about 0.2 seconds." |
| 2:30–3:00 | Postman: missing finish (400), Toronto (422), Anchorage → Seattle (422) | "Clear errors with consistent codes. Anchorage to Seattle is impossible because there's no usable station for 2,178 miles, and the API says exactly where the gap is." |
| 3:00–3:50 | Editor: `fuel_optimizer.py` docstring | "The optimiser is an exact dynamic programme from the gas-station-problem literature. If the next stop is cheaper, buy just enough to reach it; otherwise fill up. That keeps the state space small, and it runs in milliseconds for hundreds of stations." |
| 3:50–4:20 | Editor: `test_fuel_optimizer.py` brute-force reference; terminal running `python manage.py test tests` | "It's checked against an independent exhaustive search on 3,500 random cases in the suite and 20,000 in a one-off sweep, with zero mismatches. 118 offline tests, plus live OSRM tests." |
| 4:20–4:40 | README "Known limitations" | "The main limitation: the price file has no coordinates, so stations sit at city level. Detours are estimated cautiously, and every response says so." |

## Project layout

```
config/                     settings (env-driven), urls, wsgi (+ startup warm-up)
route_api/
  models.py                 FuelStation, RoutePlan
  serializers.py            request validation
  views.py, urls.py         API views, saved-plan map, health, demo page
  exceptions.py             consistent JSON error format
  services/                 planner, geocoding, routing, station index, matching, optimiser, costs, gazetteer
  management/commands/      build_place_reference, build_station_dataset, load_stations
  templates/route_api/      map.html (Leaflet), demo.html
data/
  reference/                Census gazetteer extract and state bounding boxes (public domain)
  processed/                fuel_stations.csv (6,620), ingestion report, rejected records, unmatched cities
  cache/                    one-time fallback geocoder answers
postman/                    Postman collection
tests/                      121 tests (118 offline, 3 live)
fuel-prices-for-be-assessment.csv          original price file (unchanged)
Spotter_Django_Backend_Assessment.txt      original assignment text
```

Data credits:
* US Census Bureau Gazetteer Files (public domain).
* © OpenStreetMap contributors (ODbL). This covers the routing data used by
  OSRM, the Nominatim geocoder and the map tiles.
