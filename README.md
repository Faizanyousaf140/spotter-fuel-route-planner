# Fuel Route Planner (Django REST API)

Plans a driving route between two US locations and chooses where to buy fuel
so the trip costs as little as possible, using the supplied OPIS truck-stop
price file.

> Status: work in progress (built in stages). Sections marked *TODO* are
> completed in later stages.

## Contents
1. [Quick start](#quick-start)
2. [How it works](#how-it-works)
3. [Fuel model and assumptions](#fuel-model-and-assumptions)
4. [Station data preparation](#station-data-preparation)
5. [Routing and station matching](#routing-and-station-matching)
6. [Testing](#testing)
7. [Known limitations](#known-limitations)

---

## Quick start

Requires **Python 3.14** (Django 6.1.3 supports 3.12–3.14; all pinned
dependencies publish 3.14 wheels).

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
cp .env.example .env              # set DJANGO_SECRET_KEY; DJANGO_DEBUG=True for local use
python manage.py migrate
python manage.py createcachetable # database-backed route cache
python manage.py load_stations    # loads data/processed/fuel_stations.csv (6,620 stations)
python manage.py runserver
```

No Census download or geocoding is needed: the cleaned, geocoded station file
is committed.

## How it works

```
request ──► resolve start/finish (offline gazetteer; fallback geocoder only if needed)
        ──► 1 routing call (OSRM), cached by start/finish
        ──► match stations to the route geometry (in memory, numpy)
        ──► exact minimum-cost fuel plan (dynamic programme, in memory)
        ──► JSON + map                                      (TODO: stage 4)
```

## Fuel-stop optimisation

`route_api/services/fuel_optimizer.py`. It makes no external calls and uses
only the candidate stations from matching.

**Model.**
* Fuel is counted in miles of range. Capacity U = 500, and the tank starts full.
* Stops are visited in increasing (mile marker, off-route) order. Stations
  whose nearest route point is the same are visited nearest-to-the-route first.
* A leg between consecutive stops i → j uses
  `off_i + (mile_j − mile_i) + off_j` miles of fuel. The start and the
  destination have off = 0, and the off values are estimates.
* Fuel can never be below 0 or above U, and any amount can be bought.
* The plan minimises money spent at stops, which also minimises the total cost
  (see the fuel model above).

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

It is computed backwards along the route. Suffix minima let each `C(u,·)` be
evaluated in O(log k). The answer is the cheapest first stop reachable on the
starting fuel, or no stop if the starting fuel already covers the trip.

* **Complexity:** O(n·k·log k) time and O(n·k) memory, where n is the number
  of candidate stations (after removing stations that share an identical
  position, keeping the cheapest) and k is the number of stations within range
  of a station.
* **Tie clean-up:** a stop where nothing is bought (possible only through ties)
  is removed and the fuel ledger is re-simulated.
* **No plan possible:** the API returns 422 and names the gap, e.g.
  *"no usable fuel station between mile 400.0 and mile 950.0"*.

**Verification (tests/test_fuel_optimizer.py).**
* Hand-calculated cases:
  * a trip under 500 miles;
  * exactly 500 miles;
  * 500.5 miles;
  * a cheaper station ahead;
  * no cheaper station ahead;
  * a cheaper but distant station;
  * an expensive detour against a small detour;
  * identical mile markers;
  * a 5-stop route;
  * gaps over 500 miles;
  * a destination that can't be reached;
  * not enough starting fuel.
* An **independent exhaustive solver** that tries every subset of stations, in
  any order allowed by the model, and every whole-number purchase. With integer
  distances, an optimal plan with whole-number purchases always exists (the
  constraints form a totally unimodular system), so the brute force finds the
  true optimum. The DP matched it on **600 random cases in the suite** (282
  feasible) and on **5,000 more in a one-off sweep** (1,647 feasible), with
  0 mismatches. The sweep first exposed a tie-ordering bug, which was fixed and
  is now a regression test.
* Every plan's fuel ledger is checked: never below 0, never above capacity,
  and purchases minus fuel burned equal fuel left at the end.

Under this model, and within these tests, the optimiser returns the minimum
refuelling cost. Real-road optimality also depends on the estimated detours
and city-level station positions (see limitations).

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
   If the route has no corridor stations at all, the average price of all
   loaded stations is used and the response includes a warning.
3. Fuel is used from the tank in the order it was put in: starting fuel is
   burned first.
4. **Fuel burned** = (route miles + detour miles) / 10 mpg.
5. **Total fuel cost** = `min(50, gallons burned) × start price` + money
   spent at fuel stops. The optimal plan burns every gallon it buys, so this
   equals the cost of all fuel burned.
6. Fuel left in the tank at the destination is reported but **not credited**.
   Otherwise the optimiser would be rewarded for buying cheap fuel it never
   burns.

Consequences:
* A trip of 500 miles or less needs no stop. Its cost is `gallons burned × start price`.
* On longer trips the starting tank (50 gal) is always fully burned. So
  minimising the money spent at stops also minimises the total cost, and the
  best plan arrives at the destination with an empty tank.

## Station data preparation

`python manage.py build_station_dataset` (one-time, offline; outputs committed):

| Step | Result on the supplied file |
|---|---|
| Rows read and validated | 8,151 valid, 0 rejected |
| Canadian provinces removed | 620 rows |
| Exact duplicate rows removed (after whitespace clean-up) | 26 rows |
| One station per OPIS ID, **average** of its prices | 6,626 stations (487 had several prices) |
| Coordinates from city + state: US Census Gazetteer 2026 (places, then town/township subdivisions) | 3,664 of 3,808 city/state pairs |
| One-time fallback geocoder (Nominatim, 1 req/s, cached in `data/cache/`) | 139 more pairs |
| Not found → dropped and listed in `data/processed/unmatched_cities.csv` | 5 pairs / 6 stations |
| Outside its own state's bounding box → dropped | 0 |
| **Final stations** | **6,620** |

Full counts: `data/processed/ingestion_report.json`. Every dropped record and
the reason: `data/processed/rejected_records.csv`.

## Routing and station matching

* **Routing:** the OSRM public demo server (no key) by default, using
  `overview=full&geometries=geojson`. **One call per new start/finish pair.**
  Results are cached in the database cache (`ROUTE_CACHE_SECONDS`, default
  7 days), so a repeated request makes **zero** routing calls. Timeouts and
  errors become clear API errors (503/504/502/422). OpenRouteService can be used
  instead (`ROUTING_PROVIDER=openrouteservice`, `ORS_API_KEY`). It is tested
  with mocks only, because no key was available.
* **Start/finish:** `"lat,lng"` or `"City, ST"` are resolved **offline** from
  the Census gazetteer. Free text falls back to Nominatim, cached for 30 days.
  Inputs naming a Canadian province, Canada or Mexico are rejected without any
  network call.
* **Matching** (`route_api/services/route_matching.py`):
  1. Cumulative distance along the full-resolution route geometry is scaled to
     the provider's distance, then resampled to one point every 2 miles.
  2. Stations are rough-filtered to the route's bounding box plus the corridor.
  3. Each station is matched to its nearest route point, then projected onto
     the neighbouring route segments. This gives `off_route_miles` (station
     point to route) and `mile_marker` (driving miles from the start).
  4. Stations with `off_route_miles <= MAX_OFF_ROUTE_MILES` (default **15**)
     are kept.

### City-level coordinates: how they are handled cautiously

The source file has no coordinates, and its addresses are highway/exit
descriptions. Every station therefore sits at its **city's** reference point,
not at the actual truck stop. To stay safe:

* The corridor is wide (15 miles) so stations whose city point is off the
  highway are still considered.
* For every station used, the plan **adds a detour of 2 × `off_route_miles`**
  (drive to the city point and back). This counts towards reachability between
  stops and towards fuel burned. When the truck stop is actually at the exit,
  this overstates distance and cost slightly. It never understates them, so a
  plan that is feasible on paper should be feasible on the road.
* Each stop's `off_route_miles` is returned so the user can see the
  uncertainty.

## Testing

```bash
python manage.py test tests                                     # all mocked/offline tests
RUN_LIVE_TESTS=1 python manage.py test tests.test_live_integration   # real OSRM calls
```

*TODO: full test report (stage 4).*

## Known limitations

* Station coordinates are city-level (see above). The 6 stations whose city
  could not be found are excluded.
* Prices are averaged per station ID. The file does not say which duplicate
  row is current.
* The OSRM demo server has no SLA and is limited to 1 request per second for
  non-commercial use. Use a self-hosted OSRM or OpenRouteService in production.
* Coordinates are accepted as "in the USA" when a US Census place is within
  25 miles. Points just across the Canada/Mexico border can pass.
