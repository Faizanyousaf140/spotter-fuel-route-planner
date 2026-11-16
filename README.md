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
        ──► fuel optimisation  ──► JSON + map              (TODO: stages 3–4)
```

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
