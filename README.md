# Fuel Route Planner: Django REST API

Give it a start and a finish anywhere in the USA. It returns:
* the driving route (GeoJSON) and a map;
* the **cheapest set of fuel stops** for a truck with a **500-mile range** at **10 mpg**;
* **gallons and cost per stop**;
* the **total fuel cost**.

Prices come from the supplied OPIS file (`fuel-prices-for-be-assessment.csv`).

* **Framework:** Django 6.1.3 and Django REST Framework 3.18.3, on Python 3.14.
* **Routing:** the free OSRM demo server, with **one call per new route** and **zero on a cached route**.
* **Station locations:** prepared once from US Census data. City/state and coordinate inputs resolve offline; free-text inputs can use a cached Nominatim fallback.
* **Fuel stops:** chosen by an exact minimum-cost dynamic programme. It is checked against an independent exhaustive solver.
* **Location confidence:** every stop is rated by checking whether the highway in its address is one the route uses near that point.
* **Self-consistent numbers:** every number in the response can be added up by hand and checks out exactly. This is enforced by a property test.

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
15. [Correctness hardening (v1.2)](#correctness-hardening-v12)
16. [Earlier quality audit (v1.1)](#earlier-quality-audit-v11)

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

* **Web app:** <http://127.0.0.1:8000/>. Enter two cities or click an example. The trip summary, itinerary and map appear, and the URL becomes a shareable saved-plan link. The divider between the panel and the map can be dragged.
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
* `min_location_confidence` (optional: `low` (default), `medium` or `high`):
  use only stations at least this confident. With `high`, every stop's address
  highway is one the route uses near that point.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/route/ -H "Content-Type: application/json" \
     -d '{"start": "Chicago, IL", "finish": "Dallas, TX"}'
```

### Response

This is a real cached Chicago → Dallas response captured on 2026-11-17.
Only the geometry is shortened and 2 of the 3 stops are omitted. Provider
routes and their resulting prices may change when the routing data changes.

```json
{
  "plan_id": "d5a9b339-3e1c-449f-9bcd-3e9625ede36e",
  "map_url": "http://127.0.0.1:8011/api/v1/route/d5a9b339-3e1c-449f-9bcd-3e9625ede36e/map/",
  "plan_url": "http://127.0.0.1:8011/api/v1/route/d5a9b339-3e1c-449f-9bcd-3e9625ede36e/",
  "start": {
    "query": "Chicago, IL",
    "label": "Chicago, IL",
    "lat": 41.837045,
    "lon": -87.684939,
    "resolved_by": "census_gazetteer"
  },
  "finish": {
    "query": "Dallas, TX",
    "label": "Dallas, TX",
    "lat": 32.793333,
    "lon": -96.766513,
    "resolved_by": "census_gazetteer"
  },
  "route": {
    "provider": "osrm",
    "distance_miles": 961.5,
    "duration_hours": 17.04,
    "geometry": {
      "type": "LineString",
      "coordinates": [
        [
          -87.68488,
          41.83705
        ],
        [
          -87.68116,
          41.6918
        ],
        "…"
      ]
    },
    "geometry_points_returned": 596,
    "geometry_points_from_provider": 9539
  },
  "fuel_stops": [
    {
      "sequence": 1,
      "opis_id": 67427,
      "name": "CASEYS #2907",
      "address": "I-57 & I-62",
      "city": "Charleston",
      "state": "MO",
      "location": {
        "lat": 36.916934,
        "lon": -89.333912,
        "precision": "city-level (approximate)",
        "confidence": "high",
        "matched_route_highways": [
          "I57"
        ],
        "confidence_reason": "Address names I57, which the route uses near mile 376: the stop is very likely at an exit on the route."
      },
      "price_per_gallon": 3.044,
      "mile_marker": 376.39,
      "distance_from_route_miles": 0.43,
      "estimated_detour_miles": 0.85,
      "leg_from_previous": {
        "miles": 376.81,
        "fuel_used_gallons": 37.681
      },
      "fuel_on_arrival_gallons": 12.319,
      "gallons_purchased": 7.737,
      "fuel_on_departure_gallons": 20.056,
      "cost": 23.55,
      "decision": "reach_cheaper_station",
      "reason": "Bought only enough to reach PIT STOP (Wheatley, AR) at mile 576, which is cheaper ($2.999 vs $3.044/gal)."
    }
  ],
  "fuel_cost": {
    "total_fuel_cost": 306.55,
    "purchased_during_trip": {
      "gallons": 46.411,
      "cost": 136.6,
      "number_of_stops": 3
    },
    "starting_fuel_estimate": {
      "gallons_in_tank_at_start": 50.0,
      "gallons_used": 50.0,
      "price_per_gallon": 3.399,
      "valuation_method": "nearest_corridor_station",
      "estimated_value_of_fuel_used": 169.95,
      "valued_at_station": {
        "opis_id": 73127,
        "name": "Gas N Wash",
        "city": "Chicago",
        "state": "IL",
        "mile_marker": 0.0,
        "distance_from_route_miles": 0.0
      }
    },
    "how_total_is_calculated": "starting_fuel_estimate.estimated_value_of_fuel_used + purchased_during_trip.cost",
    "currency": "USD"
  },
  "fuel_usage": {
    "route_distance_miles": 961.47,
    "estimated_detour_miles": 2.64,
    "total_miles_driven": 964.11,
    "fuel_burned_gallons": 96.411,
    "fuel_remaining_at_destination_gallons": 0.0,
    "final_leg": {
      "miles": 161.29,
      "fuel_used_gallons": 16.129
    },
    "longest_leg_miles": 376.81,
    "max_range_miles": 500.0
  },
  "data_quality": {
    "route_distance_and_geometry": "from the routing provider (osrm)",
    "station_positions": "estimated: city-level reference point of the station's city",
    "location_confidence": {
      "high": 3,
      "medium": 0,
      "low": 0
    },
    "detours": "approximation: 2 x straight-line distance from the route to the city point; real road detours can be shorter or longer",
    "prices": "static OPIS file; average of a station's price rows",
    "starting_fuel_value": "estimated: start price x starting gallons used",
    "money_rounding": "each purchase rounded to the cent; totals are sums of the shown parts"
  },
  "assumptions": {
    "vehicle_range_miles": 500.0,
    "vehicle_mpg": 10.0,
    "tank_capacity_gallons": 50.0,
    "starting_fuel": "Full tank, valued at the price of the corridor station nearest the start.",
    "fuel_remaining_at_destination": "Reported but not credited.",
    "station_corridor_miles": 15.0,
    "min_location_confidence": "low",
    "detour_model": "2 x straight-line distance from the route to the station's city-level point.",
    "optimization": "Exact minimum refuelling cost over corridor stations (dynamic programme)."
  },
  "warnings": [
    "Station coordinates are approximate: the price file has no coordinates, so each station is placed at its city's reference point (US Census Gazetteer, or a one-time geocoder lookup). distance_from_route_miles and estimated_detour_miles are straight-line estimates, not road distances; each used station is charged a detour of 2 x its straight-line distance from the route. This is an approximation: the real road detour can be shorter (stop at the exit) or longer (indirect roads).",
    "Prices come from the supplied OPIS file (one record per station ID, averaging duplicate price rows); they are not live prices."
  ],
  "meta": {
    "map_api_calls": {
      "routing": 0,
      "geocoding": 0
    },
    "route_cache_hit": true,
    "plan_cache_hit": true,
    "corridor_stations": 229,
    "corridor_stations_meeting_min_confidence": 229,
    "stations_considered_by_optimizer": 109,
    "stations_loaded": 6620,
    "station_data_fingerprint": "139f2ebf554e11486911aa73",
    "timings_ms": {
      "resolve_locations": 0.2,
      "station_index": 1.6,
      "total_request": 11.1
    }
  }
}
```

Every number can be checked by hand:
* $23.55 + $67.61 + $45.44 = $136.60;
* $169.95 + $136.60 = $306.55;
* 50 − 37.681 = 12.319 gal on arrival, and 12.319 + 7.737 = 20.056 gal on departure.

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
| 422 | `location_outside_usa` | Canada or Mexico input, or coordinates outside the offline US land boundaries |
| 422 | `location_not_found` | The text cannot be resolved |
| 422 | `route_not_found` | The provider finds no drivable route (e.g. Los Angeles → Honolulu) |
| 422 | `no_feasible_fuel_plan` | Some stretch longer than 500 miles has no usable station; `details` names the gap |
| 429 | `rate_limited` | More than `ROUTE_RATE_LIMIT` (default 60/min) planning requests from one IP. Includes `Retry-After`. |
| 502 | `routing_provider_error` | The provider answered with something unusable |
| 503 | `routing_unavailable` / `geocoder_unavailable` | The provider is unreachable or rate-limited |
| 503 | `station_data_unavailable` | No stations loaded: run `load_stations` |
| 503 | `location_validation_unavailable` | The committed offline boundary data is missing or corrupt |
| 504 | `routing_timeout` | The provider timed out: connect 5 s, read 15 s, **30 s hard limit for the whole call** |
| 500 | `internal_error` | Unexpected error. It is logged, and no stack trace is returned. |

A failed request never returns a made-up route, and nothing is saved.

## How it works

```
POST /api/v1/route/
  │ serializers.RouteRequestSerializer          validate input (no external call on invalid input)
  ▼
services/planner.py ── orchestrates and times each step
  ├─ geocoding_service   "City, ST" / "lat,lng" → coordinates, offline (Census gazetteer); Nominatim only for free text
  ├─ plan cache          identical request (same route, options, station data) → answered without routing work
  ├─ routing_service     1 OSRM call (overview=full, polyline6, steps) → cached in the DB cache; 0 calls on a hit;
  │                      single flight for concurrent identical requests; 30 s hard deadline
  ├─ station_service     6,620 stations in numpy arrays; DB revision check detects cross-process reloads
  ├─ route_matching      preserve every original vertex → segment grid + bbox → mile marker and off-route distance
  ├─ highways            address highway (e.g. I-80) vs the route's highway refs → location confidence
  ├─ fuel_optimizer      exact minimum-cost refuelling (dynamic programme)
  └─ cost_calculator     starting-fuel valuation, per-stop gallons and cost, totals (rounded only at output)
  ▼
RoutePlan saved in the DB  →  JSON response + map_url  (/route/<id>/map/ renders from the DB)
```

The place gazetteer, boundary polygons and station index are preloaded when
the server starts (`WARMUP_ON_START`). Each request checks a small database
revision row before reusing the station index.

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
7. **Rounding.**
   * The optimiser works unrounded.
   * Displayed fuel levels are rounded to 0.001 gal at each stop. Gallons
     bought = departure − arrival, and fuel used on a leg = previous
     departure − arrival, so the displayed ledger balances exactly.
   * Money is rounded per purchase, half up, like a pump receipt: gallons
     shown × price, where prices keep their stored 5 decimals.
   * Every total is the sum of its displayed parts.

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

**On real routes.** Across 30 diverse US routes, 81 stops in total, no plan
visited a station twice or made two stops at the same mile marker. If a plan
ever does, the response adds a warning explaining it.

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
  * The OSRM public demo server (no key), using `overview=full&geometries=polyline6&steps=true`.
    The steps carry the highway refs used for location confidence, and it is still one request.
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
  * Coordinates are validated against committed US Census 2025 cartographic
    land boundaries for the 50 states and DC, including remote Alaska and Hawaii.
    Windsor and Tijuana are rejected; Yellowstone and remote US land are accepted.
    No proximity buffer admits points across an international border.
* **Matching** (`route_matching.py`):
  1. Keep every original provider vertex. Additional points densify long
     segments without cutting corners. Cumulative mile markers are scaled to
     the provider's distance.
  2. A route bounding box and a grid of segment bounding boxes safely select
     all segments that could intersect a station's corridor box. Long segments
     use an overflow list. Longitudes are unwrapped across the antimeridian.
  3. Project onto every eligible original segment, choose the nearest point,
     and interpolate its cumulative mile marker. The right-angle regression
     retains a station 0.848 miles away in a 1-mile corridor; the previous
     shortcut measured 1.555 miles and incorrectly excluded it.

### Station reload and cache contract

`load_stations` and supported `FuelStation` ORM writes update the station
dataset revision in the same transaction as the data. Every station-index
access checks that revision, so running worker processes refresh after a
committed reload without a restart. Index rebuilding checks the revision
before and after reading rows and retries three times; repeated concurrent
changes return a controlled 503. A request already using a committed snapshot
may finish with that snapshot. Transaction-local snapshots are never published
globally, including when a transaction rolls back.

The fingerprint covers ID, name, address, city, state, effective price,
coordinates, geocode source and parsed highway references. The persistent
plan-cache key also covers MPG, maximum range, effective and default corridor,
confidence selection, matching and response-geometry settings, calculation
version, routing provider/endpoint, station revision/fingerprint and boundary
data version. A configuration or station change recomputes the plan while
still allowing a valid provider-route cache entry to be reused.

Direct SQL, external database editors and other ORM-bypassing station writes
must increment `StationDatasetRevision(pk=1).generation` in the **same
transaction**. Use `load_stations` for supported dataset reloads. Migration
`0003_stationdatasetrevision` is required.

### Station coordinates are city-level estimates

The price file has no coordinates, and its addresses are highway/exit
descriptions. Every station is therefore placed at its **city's** reference
point. How the plan handles this:
* the corridor is wide (15 miles);
* every station used is charged a **detour of 2 × distance_from_route**. This
  counts towards reachability and fuel burned. It is an **approximation, not a
  bound**:
  * it overstates the detour when the stop is right at an exit (the usual case);
  * it understates it when the roads to the city point are indirect, since
    straight-line distance is never longer than road distance;
  * `low` location confidence (below) flags the riskier case;
* every response carries a warning, and every stop shows its estimated
  distance from the route.

### Location confidence (no extra API calls)

The station file's addresses name highways (e.g. `I-80, EXIT 143 & CR-S14`).
The single OSRM call also returns the highway refs along the route
(`I 80; US 6`). Each stop is rated:

| Confidence | Rule | Historical v1.1 audit: 81 stops on 30 real routes |
|---|---|---|
| `high` | The address names a highway (I-, US- or state route) the route uses within (distance from route + 5) miles of the stop's mile marker, so the stop is very likely at an exit on the route | 61 (75 %) |
| `medium` | No highway match, but the city point is within 3 miles of the route | 16 |
| `low` | Neither. The real detour may be longer than estimated, and the response warns. | 4 |

* This raises confidence but does **not** produce exact coordinates.
* It is **not** used to change the detour approximation.
* `min_location_confidence=high` makes the optimiser use only high-confidence
  stations. In the historical v1.1 audit this cost **+$20.64 in total (+0.2 %)**. The worst
  single route was +$5.84, and every route stayed feasible.

## Performance

Measured on 2026-11-17 over real HTTP using threaded Django `runserver` on
this Windows machine and an isolated SQLite database. Public OSRM latency
varies; these are observations rather than latency guarantees. Raw results
are in [`validation/route_audit.json`](validation/route_audit.json) and
[`validation/timeout_benchmarks.json`](validation/timeout_benchmarks.json).

| Scenario | Routing calls | Result |
|---|---|---|
| 30 different uncached routes | 1 each | median 2,960 ms; p95 6,832 ms; range 2,049–12,975 ms |
| 15 cached requests | 0 | median 58.5 ms; p95 153.4 ms |
| 8 concurrent clients, 48 cached requests | 0 | 26.1 requests/s; median 176.5 ms; p95 752.2 ms |
| 4 simultaneous identical uncached requests | **1 total** | all succeed in 4.08 s |
| Real trickling response body, 0.8 s budget | 1 | 504 in 826 ms; zero remaining HTTP workers |
| Real trickling headers, 0.8 s budget | 1 | 504 in 813 ms; zero remaining HTTP workers |
| 4 simultaneous identical slow streams, 0.8 s budget | **1 total** | all 504 within 815 ms; zero remaining HTTP workers |

Each real upstream exchange runs in one owned subprocess, with an absolute
deadline enforced by its parent. The budget includes spacing-lock waits,
single-flight waits, process startup, DNS, connection, TLS, headers and body.
At expiry the parent kills and reaps the worker and closes its pipes; there
are no abandoned executor threads or ongoing network operations. A small
bounded termination/response overhead follows expiry (about 11–26 ms in the
0.8-second benchmarks). Body size is capped, including gzip decompression.
Actual read timeouts return 504; genuine connection failures return 503.

Valid plan-cache hits skip routing, station matching and optimization. The
station revision check still runs. Original geometry is retained for matching;
only the response/map geometry is simplified. The process-per-exchange design
adds startup overhead in return for cancellable network operations and uses
only Python's standard library plus the existing dependencies.

## Testing

```bash
python manage.py test tests                                       # 175 offline tests; 3 live tests skipped
RUN_LIVE_TESTS=1 python manage.py test tests                        # all 178 tests, including real OSRM
python manage.py check
python manage.py makemigrations --check --dry-run
```

PowerShell: set `$env:RUN_LIVE_TESTS='1'` before the live command. The final
2026-11-17 run passed **178 tests (175 offline + 3 live)**. No existing
assertions were removed to obtain a pass. The station-index query-budget test
now enforces the intentional revision-query contract: four queries to build
the initial snapshot, then one small query for each reuse.

| Module | Tests | Covers | External services |
|---|---|---|---|
| `test_station_dataset` | 18 | CSV validation, Canada filter, duplicates, averaging, gazetteer aliases, bounds | none |
| `test_commands` | 5 | Dataset build/reload, fallback caching, wrong-state and malformed CSV | mocked geocoder |
| `test_geo_and_matching` | 19 | Distances, polyline, simplification, mile markers, corridor edges, many stations | none |
| `test_highways_and_polyline` | 5 | Address/route highway parsing, confidence, vectorized decoder | none |
| `test_routing_and_geocoding` | 28 | Provider parsing/errors/cache, location resolution, revision-aware station index | mocked HTTP |
| `test_fuel_optimizer` | 22 | Hand calculations, infeasibility, 3,500 exhaustive-reference comparisons, ledger | none |
| `test_cost_calculator` | 7 | Start price, valuation, detours, rounding; 300 random published ledgers | none |
| `test_api` | 20 | JSON, statuses, saved plans/maps, caches, errors | mocked OSRM |
| `test_robustness` | 20 | Concurrency, cache corruption, rate limits, input, invariant guards | mocked HTTP |
| `test_hardening` | 30 | Real trickling body/headers, deadlines and worker cleanup, original geometry, US polygons, MPG and metadata invalidation, rollback hazards, malformed JSON/huge coordinates, truthful explanations and Loom narration | real local HTTP server; no external service |
| `test_station_revision_process` | 1 | Running observer refreshes after another process reloads all 6,620 stations | separate local processes and isolated SQLite DB |
| `test_live_integration` | 3 | Real OSRM geometry/highways/matching, no route, complete API/map/cache flow | real OSRM |

Further checks, with committed machine-readable results in `validation/`:
* **30-route correctness audit:** every published leg, tank level, purchase,
  fuel total and cent-rounded cost rechecked independently. All 30 succeed;
  each uncached request makes one routing call. Sparse station coverage can
  still legitimately produce 422 on other routes, such as Portland → San Diego.
* **Independent optimizer sweep:** 20,000 seeded cases, including 6,951
  feasible cases; zero mismatches against exhaustive search.
* **Newman:** original Postman assertions on the normal server and real
  connection-refused/slow-stream fault servers: 16 requests, 52 assertions,
  zero failures. Normal server uses the default rate limit.
* **Headless Edge:** real route/tiles, stop popups, legend, draggable desktop
  divider, saved plan reload and JSON link, mobile map/list tabs, controlled
  API errors, and graceful Leaflet failure. Zero JavaScript errors. Repeated
  at 20 MPG to verify configuration-aware rendering and persistent-cache reuse.
* **MPG restart check:** a persisted 10-MPG plan is recomputed at 20 MPG,
  produces a 25-gallon tank and the correct halved fuel cost, then hits cache.
  Both requests reuse the valid provider-route cache with zero routing calls.

Reproduction commands (use a separate `SQLITE_PATH` database for audits):

```bash
python scripts/validate_hardening.py --help
python scripts/benchmark_timeouts.py
python scripts/run_postman_checks.py
node scripts/check_browser.cjs
python scripts/check_config_cache.py
```

Newman and Playwright are optional **validation tools**, installed in npm's
cache rather than application requirements. `SPOTTER_NEWMAN_CLI`,
`SPOTTER_PLAYWRIGHT_MODULE` and `SPOTTER_EDGE_PATH` select their local paths.
Browser/config scripts default to a validation server on port 8011;
the config check starts and stops its own second server on port 8016.

## Postman

Import `postman/Spotter_Fuel_Route_API.postman_collection.json`. Its
`base_url` variable defaults to `http://127.0.0.1:8000`.

| Folder | Requests |
|---|---|
| 0. Health | health check |
| 1. Successful routes | short route without a stop; multi-stop New York → Los Angeles (saves `plan_id`/`map_url`); cost breakdown; `lat,lng` input; only high-confidence stops; custom corridor; saved plan JSON; saved map HTML |
| 2. Invalid input | missing finish (400), outside USA (422), invalid coordinates (400) |
| 3. Impossible routes | Los Angeles → Honolulu (422 `route_not_found`), Anchorage → Seattle (422 `no_feasible_fuel_plan`) |
| 4. Provider failures | 503 `routing_unavailable` and 504 `routing_timeout`. These need the server started with a broken `OSRM_BASE_URL` (instructions are in the folder). |

* **Test scripts:** every request checks the status code and key fields. The
  successful routes also check that:
  * `total = starting fuel estimate + purchased during trip`;
  * the stop costs add up to the trip cost;
  * no leg exceeds the vehicle range.
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
| `ROUTE_SAMPLE_SPACING_MILES` | `2` | Maximum added sampling interval; all original route vertices are retained |
| `ROUTING_PROVIDER` | `osrm` | `osrm` or `openrouteservice` |
| `OSRM_BASE_URL` | `https://router.project-osrm.org` | Point this at a self-hosted OSRM for production |
| `ORS_BASE_URL` / `ORS_API_KEY` | HeiGIT endpoint / empty | OpenRouteService settings |
| `ROUTING_CONNECT_TIMEOUT` / `ROUTING_READ_TIMEOUT` | `5` / `15` s | Per-connection and per-read timeouts |
| `ROUTING_TOTAL_TIMEOUT` | `30` s | Absolute exchange budget including waits and streaming, followed by bounded worker cleanup. |
| `ROUTE_RATE_LIMIT` | `60/min` | Per-client-IP limit on `POST /api/v1/route/` (empty disables). Clients are identified by `REMOTE_ADDR`. |
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
  OpenRouteService in production. The 30-route audit observed 2–13 s on uncached requests; the configured network deadline bounds upstream waits.
* **Single flight works within one process.** Several server processes could
  each make one call for the same new route. That is still one call per
  process, and the results are shared through the database cache afterwards.
* **CPU-bound work shares Python's global interpreter lock (GIL).** CPU work on cache misses can contend within one process; use several worker processes when needed. Identical requests are
  served from the plan cache.
* **Location confidence comes from address text.** It relies on OPIS address
  strings and OpenStreetMap highway refs. Addresses like `HIGHWAY 11/17` are
  ambiguous and are deliberately not matched.
* **US boundary precision.** The offline Census 1:500,000 cartographic land polygons are generalized, not survey-grade navigation boundaries. Small coastal features and points within the source's border generalization error can be misclassified. Territories and offshore water are excluded. No distance buffer is applied. Source, version and checksum are in `data/reference/us_boundaries.provenance.json`.
* **Local server only.** This setup is for local use with `runserver`. A
  production deployment would add a WSGI server, static-file serving and HTTPS.
  Those were left out on purpose for this assessment.

## Demo: Loom walkthrough script

Target 4½ minutes. Before recording, start the server and open Postman with
the collection imported. Have the editor open on `route_api/services/`.

| Time | Show | Say |
|---|---|---|
| 0:00–0:30 | README top | "A Django REST API: start and finish in the USA → route, cheapest fuel stops for a 500-mile, 10-mpg truck, total cost, and a map. One routing call per new route, zero when cached." |
| 0:30–1:15 | Editor: `planner.py`, then the `services/` folder | "The view validates input and calls the planner. City and state inputs resolve offline; free-text inputs can use a cached geocoding fallback. It makes one OSRM call, cached in the database. Stations are matched to the actual route geometry, then the optimiser and cost calculator run. The plan is saved, so its map URL survives restarts." |
| 1:15–2:30 | Postman: multi-stop New York → Los Angeles; scroll `fuel_stops`, `fuel_cost`, `meta`. Send again. Open `map_url` in a browser. | "Nine stops, each with gallons, cost and the reason it was chosen. Money spent on the trip is kept separate from the estimated value of the starting tank. `meta` shows 1 routing call. On the second send it's 0, a cache hit, and about 0.2 seconds." |
| 2:30–3:00 | Postman: missing finish (400), Toronto (422), Anchorage → Seattle (422) | "Clear errors with consistent codes. Anchorage to Seattle is impossible because there's no usable station for 2,178 miles, and the API says exactly where the gap is." |
| 3:00–3:50 | Editor: `fuel_optimizer.py` docstring | "The optimiser is an exact dynamic programme from the gas-station-problem literature. If the next stop is cheaper, buy just enough to reach it; otherwise fill up. That keeps the state space small, and it runs in milliseconds for hundreds of stations. The tank may reach exactly zero, but never goes below zero." |
| 3:50–4:20 | Editor: `test_fuel_optimizer.py` brute-force reference; terminal running `python manage.py test tests` | "It's checked against an independent exhaustive search on 3,500 random cases in the suite and 20,000 in a one-off sweep, with zero mismatches. 175 offline tests cover concurrency, real streaming timeouts, caching and hand-checkable numbers, plus 3 live OSRM tests." |
| 4:20–4:40 | Map legend; README "Location confidence" | "The price file has no coordinates, so stations sit at city level. Each stop is rated by checking whether its address highway is on the route; 75 % of stops on 30 test routes are high confidence, and you can require that for about 0.2 % extra cost." |

## Project layout

```
config/                     settings (env-driven), urls, wsgi (+ startup warm-up)
route_api/
  models.py                 FuelStation, StationDatasetRevision, RoutePlan
  serializers.py            request validation
  views.py, urls.py         API views, saved-plan map, health, demo page
  exceptions.py             consistent JSON error format
  services/                 planner, geocoding, routing, station index, matching, optimiser, costs, gazetteer
  management/commands/      build_place_reference, build_station_dataset, load_stations
  templates/route_api/      app.html: the web app (Leaflet map, trip summary, itinerary; no build step)
data/
  reference/                Census gazetteer, offline state land polygons/provenance, build-time bounding boxes
  processed/                fuel_stations.csv (6,620), ingestion report, rejected records, unmatched cities
  cache/                    one-time fallback geocoder answers
postman/                    Postman collection
tests/                      178 tests (175 offline, 3 live)
scripts/                    reproducible boundary build, route/optimizer audit, benchmarks, Newman/browser checks
validation/                 captured review reproductions and final verification results
fuel-prices-for-be-assessment.csv          original price file (unchanged)
Spotter_Django_Backend_Assessment.txt      original assignment text
```

Data credits:
* US Census Bureau Gazetteer Files and 2025 cartographic state boundaries (public domain).
  Boundary data: [Census cartographic boundary files](https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html); rebuild with `python scripts/build_us_boundaries.py --help`.
* © OpenStreetMap contributors (ODbL). This covers the routing data used by
  OSRM, the Nominatim geocoder and the map tiles.

## Correctness hardening (v1.2)

The independent review findings were reproduced before changing code; raw
observations are in [`validation/review_reproduction_before.json`](validation/review_reproduction_before.json).
The finding-by-finding causes, changed files, regression names, outcomes and
remaining limitations are in [`validation/final_review.json`](validation/final_review.json).
All ten findings have regression coverage. Changes include cancellable total
HTTP deadlines and shared single-flight errors, original-segment matching,
offline US polygons, complete configuration/data cache keys, atomic
cross-process station revisions, complete station fingerprints, timeout
classification, accurate optimizer explanations, controlled malformed-input
handling, and corrected Loom narration. Defaults remain 500 miles and 10 MPG.
The exact optimizer and the application dependencies are unchanged.

The full offline/live suite, independent route/optimizer audits, real HTTP
benchmarks, original Newman assertions and Edge checks passed. Measurements
and precision/coverage limitations are documented above; saved historical
plans retain their original snapshot and remain viewable.

## Earlier quality audit (v1.1)

Historical results from the audit of v1.0 (superseded where corrected by v1.2): 30 real routes, concurrent
load, a browser, and the API output read with a reviewer's eye. Defects found
and fixed:

| Finding | Evidence | Fix |
|---|---|---|
| A routing call could hang for **30 minutes** | Measured: OSRM trickled a response; `requests` timeouts only bound each read. Single flight made 3 more requests wait as long. | Initial mitigation streamed under a checked deadline with a size cap. Independent review found a blocking-chunk defect; v1.2 replaces this with owned cancellable processes and real HTTP regression tests. |
| Concurrent identical uncached requests could each call OSRM | Code review | Single flight per route key, tested with 8 threads (exactly 1 call) |
| Published numbers didn't add up by hand | 6 of 30 real plans: stop costs ≠ trip cost by 1 cent; 11 had a ledger rebuilt from displayed values going over 50 gal | Per-purchase cent rounding in `Decimal`; ledger built from rounded fuel levels; per-leg fuel published; property test over 300 random plans |
| Starting-fuel value depended on the confidence filter | The `medium` filter appeared cheaper than no filter on real routes | Valued against the full corridor; monotonicity tested |
| Cached requests slow under concurrency | 8 clients: 5.4 req/s, p95 2.2 s | Plan cache keyed on route, options, station-data fingerprint and version: 23.4 req/s, p95 0.43 s |
| A corrupted cache entry would break requests | Code review | Treated as a miss, then refetched and replaced; tested |
| No protection for the free upstream services | Code review | Per-IP rate limit (`REMOTE_ADDR` only, so a spoofed `X-Forwarded-For` doesn't bypass it); SQLite WAL with `IMMEDIATE` transactions for concurrent writes |
| City-level positions gave no sense of reliability | 30-route audit | Location confidence from address highways against route highway refs, from the same single OSRM call; optional `min_location_confidence` |
| Map: no feedback when Leaflet or tiles fail, not responsive, sparse popups | Headless browser review | Error notices, stacked mobile layout, confidence legend, detailed popups, clickable stop list, works with plans saved before v1.1 |
