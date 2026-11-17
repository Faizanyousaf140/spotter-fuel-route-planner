# Loom script: Spotter Backend Django Assessment (target 4:30, max 5:00)

Tools that must appear: **GitHub, VS Code, Terminal, Postman** (plus a quick look at the browser map).

## Before you press record (2 minutes)
1. Terminal 1 (project folder, venv active): `python manage.py runserver`. Wait for "Starting WSGI development server".
2. Postman open with the collection imported, `base_url` = `http://127.0.0.1:8000`.
3. VS Code open on the project folder, with `route_api/services/planner.py` and `route_api/services/fuel_optimizer.py` in tabs.
4. Browser tabs: the GitHub repo, and `http://127.0.0.1:8000/`.
5. Do NOT run `Nashville, TN -> Phoenix, AZ` beforehand. It is your "fresh route" for the cache demo.
6. Close notifications. Zoom the terminal and VS Code fonts up so they are readable.

---

## 0:00-0:30 | GitHub: the problem and the solution
**Show:** the GitHub repo page, scroll slowly through the README top.

> "Hi, I'm Faizan. This is my Spotter backend assessment. It's a Django REST API: you give it a start and a finish in the USA, and it returns the route, the cheapest fuel stops for a truck with a 500-mile range at 10 miles per gallon, the cost of every stop, and the total fuel cost, plus an interactive map.
> The key constraints were speed and as few routing calls as possible, so it makes one OSRM call per new route and none for a repeat."

## 0:30-1:15 | VS Code: architecture
**Show:** VS Code explorer, open `route_api/services/`, then `planner.py`.

> "The project is Django 6 with Django REST Framework. The view only validates input and calls `planner.py`, which runs the steps in order.
> One: locations are resolved offline from US Census data. City and state inputs resolve offline; free-text inputs can use a cached geocoding fallback.
> Two: one OSRM routing call, cached in the database.
> Three: the 6,620 fuel stations are matched to the actual route geometry, with a mile marker for each.
> Four: the optimizer picks where and how much to buy.
> Five: the plan is saved in the database, so its map link works after a restart.
> The price file had no coordinates, so I prepared it once, offline: removed Canada, merged duplicate IDs by average price, and located each station by its city."

## 1:15-1:35 | Terminal: the server
**Show:** Terminal with the running server, then `/api/v1/health/` or just point at the startup lines.

> "I'm running it locally with `manage.py runserver`. At startup it preloads the station index, and the health endpoint shows 6,620 stations loaded."

## 1:35-3:00 | Postman: the API
**Show:** Postman, one request at a time.

1. **New York to Los Angeles** → Send.
   > "A 2,810-mile trip. Nine fuel stops, and the total estimated cost is $874.84."
   Scroll to `fuel_cost`:
   > "The cost is split in two: $161.95 is the estimated value of the starting tank, and $712.89 is what's actually bought on the trip. Added together that's the total."
   Scroll to one stop in `fuel_stops`:
   > "Each stop has the price, the gallons bought, the cost, the fuel on arrival and departure, and the reason it was chosen. No leg is longer than 500 miles."
   Scroll to `meta`:
   > "One routing call."
2. **Fresh route** (`Nashville, TN` → `Phoenix, AZ`) → Send, point at `map_api_calls.routing: 1` and the ~2-3 second time. Send the same request again:
   > "Same request again: zero routing calls, plan cache hit, and about a tenth of a second."
3. **An error** (`Toronto, ON` as the start):
   > "Clear errors with consistent codes: here a 422, location outside the USA. An impossible fuel plan, like Anchorage to Seattle, returns 422 and names the exact gap."

**Optional, 15 seconds:** open the `map_url` in the browser.
> "The same plan on a map: numbered stops, a timeline, and each stop's location confidence. Clicking a card jumps to its marker."

## 3:00-3:50 | VS Code: the optimizer
**Show:** `fuel_optimizer.py`, scroll to the docstring at the top.

> "The optimizer is an exact dynamic programme from the gas-station-problem literature. If the next stop is cheaper, buy just enough to reach it; otherwise fill the tank. That keeps the state small, so it runs in milliseconds for hundreds of stations.
> The tank may reach exactly zero, but never goes below zero or above 50 gallons, and every stop is reachable within 500 miles."

## 3:50-4:20 | Terminal: the tests
**Show:** Terminal 2 → run `python manage.py test tests` (allow about a minute; keep talking).

> "To trust the optimizer, I compare it with an independent exhaustive search on thousands of random cases, with zero mismatches. In total there are 178 tests: 175 offline, plus 3 that call the real OSRM server. They cover concurrency, timeouts, caching, and a property test that every number in the response adds up exactly."

If it has not finished, say "it passes" only after you see `OK`.

## 4:20-4:45 | GitHub: limitations and close
**Show:** the GitHub README "Known limitations" section.

> "The main limitation is honest: the price file has no coordinates, so stations sit at their city's reference point. Detours are an approximation, every response says so, and each stop carries a location-confidence rating.
> The code, the README with setup steps, and the Postman collection are all in this repository. Thanks for watching."

---

## If you are running long
Cut the optional map demo and the error demo (saves about 30 s). Keep the cache demo and the tests.

## Numbers you can quote (all measured)
| What | Value |
|---|---|
| NY to LA | 9 stops, $874.84 total ($161.95 starting tank + $712.89 bought) |
| Chicago to Dallas | 3 stops, $306.55 |
| Fresh route | 1 routing call; median 3 s in the 30-route audit (provider latency varies) |
| Repeat route | 0 routing calls, about 0.1 s |
| Tests | 178 (175 offline + 3 live) |
