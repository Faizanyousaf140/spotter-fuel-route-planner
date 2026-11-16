"""
Minimum-cost refuelling along a fixed route (exact dynamic programme).

Model (all fuel quantities in *miles of range*; gallons = miles / mpg):
  * Capacity U = range_miles (500). The vehicle starts with F0 (default U).
  * Candidate station i: mile marker m_i, off-route distance o_i, price c_i ($/gal).
  * Stops are visited in increasing (mile marker, off-route distance) order:
    stations whose nearest route point coincides (common at mile 0 and at the
    destination) are visited nearest-to-the-route first. A leg from stop i to
    stop j uses
        d(i, j) = o_i + (m_j - m_i) + o_j
    (leave i back to the route, drive along it, detour to j). The start and the
    destination have o = 0. The o values are ESTIMATES (city-level coordinates).
  * Fuel never goes below 0 or above U. Any amount up to the free space can be
    bought. Objective: minimise money spent at stops.

Algorithm (Khuller, Malekian & Mestre, "To fill or not to fill: the gas station
problem", ACM TALG 2011): some optimal plan has this form at every stop u with
next stop v:
  * if c_v <= c_u (or v is the destination): buy just enough to reach v (arrive empty);
  * if c_v >  c_u: fill the tank.
So the fuel on arrival at u is 0, U - d(w, u) for a cheaper predecessor w, or
F0 - d(start, u). The value function C(u, g) (min cost from arriving at u with
g miles of fuel) is then
    C(u, g) = min( -g*p_u + min_{v cheaper/equal, d(u,v) >= g} [d(u,v)*p_u + C(v, 0)],
                   (U - g)*p_u + min_{v pricier} C(v, U - d(u,v)) )
with p_u = c_u / mpg (cost per mile). It is computed backwards over stations
sorted by mile marker. Using suffix minima, each C(u, .) is a function
evaluated in O(log k).
Total time O(n k log k), where n = candidate stations and k = stations within
range of a station. Memory O(n k).

Exactness is checked in tests/test_fuel_optimizer.py against an independent
exhaustive solver on randomised small instances.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Any, Sequence

from .errors import NoFeasiblePlan

EPS = 1e-9
DESTINATION = -1

DECISION_REACH_CHEAPER = "reach_cheaper_station"
DECISION_REACH_EQUAL = "reach_equal_price_station"
DECISION_FILL_UP = "fill_up"
DECISION_REACH_DESTINATION = "reach_destination"


@dataclass(frozen=True)
class FuelCandidate:
    key: Any            # caller's reference (e.g. a CorridorStation)
    mile: float         # mile marker along the route
    off_route: float    # estimated one-way miles from the route to the station
    price: float        # USD per gallon


@dataclass
class PlannedStop:
    candidate: FuelCandidate
    arrival_fuel_miles: float
    purchase_miles: float
    departure_fuel_miles: float
    cost: float
    decision: str
    next_candidate: FuelCandidate | None  # None -> destination
    mpg: float

    @property
    def purchase_gallons(self) -> float:
        return self.purchase_miles / self.mpg

    @property
    def arrival_gallons(self) -> float:
        return self.arrival_fuel_miles / self.mpg

    @property
    def departure_gallons(self) -> float:
        return self.departure_fuel_miles / self.mpg

    @property
    def detour_miles(self) -> float:
        return 2 * self.candidate.off_route


@dataclass
class FuelPlan:
    stops: list[PlannedStop]
    route_miles: float
    range_miles: float
    mpg: float
    start_fuel_miles: float
    candidates_input: int
    candidates_considered: int
    purchase_cost: float = field(init=False)
    purchased_miles: float = field(init=False)
    detour_miles: float = field(init=False)
    burned_miles: float = field(init=False)
    end_fuel_miles: float = field(init=False)

    def __post_init__(self):
        self.purchase_cost = sum(s.cost for s in self.stops)
        self.purchased_miles = sum(s.purchase_miles for s in self.stops)
        self.detour_miles = sum(s.detour_miles for s in self.stops)
        self.burned_miles = self.route_miles + self.detour_miles
        self.end_fuel_miles = self.start_fuel_miles + self.purchased_miles - self.burned_miles
        if abs(self.end_fuel_miles) < 1e-7:
            self.end_fuel_miles = 0.0


def optimize_fuel_plan(
    candidates: Sequence[FuelCandidate],
    route_miles: float,
    *,
    range_miles: float = 500.0,
    mpg: float = 10.0,
    start_fuel_miles: float | None = None,
) -> FuelPlan:
    U = float(range_miles)
    F0 = U if start_fuel_miles is None else float(start_fuel_miles)
    D = float(route_miles)
    _validate(candidates, D, U, mpg, F0)

    nodes = _prune(candidates, U)
    plan_args = dict(route_miles=D, range_miles=U, mpg=mpg, start_fuel_miles=F0,
                     candidates_input=len(candidates), candidates_considered=len(nodes))
    if D <= F0 + EPS:
        return FuelPlan(stops=[], **plan_args)

    n = len(nodes)
    miles = [c.mile for c in nodes]
    offs = [c.off_route for c in nodes]
    ppm = [c.price / mpg for c in nodes]

    # Per-station structures for evaluating C(u, g):
    a_dist: list[list[float]] = [[] for _ in range(n)]     # sorted d(u, v) for cheaper/equal v (+ destination)
    a_val: list[list[float]] = [[] for _ in range(n)]      # suffix-min of d*p_u + C(v, 0)
    a_arg: list[list[int]] = [[] for _ in range(n)]        # argmin of that suffix (node index or DESTINATION)
    b_val = [math.inf] * n                                 # min over pricier v of C(v, U - d(u, v))
    b_arg = [DESTINATION] * n
    c_empty = [math.inf] * n                               # cached C(u, 0)

    def evaluate(u: int, g: float) -> tuple[float, str, int]:
        """(min cost, decision, next node) when arriving at u with g miles of fuel."""
        best, decision, nxt = math.inf, "", DESTINATION
        dists = a_dist[u]
        i = bisect_left(dists, g - EPS)
        if i < len(dists):
            best = a_val[u][i] - g * ppm[u]
            nxt = a_arg[u][i]
            decision = DECISION_REACH_DESTINATION if nxt == DESTINATION else (
                DECISION_REACH_CHEAPER if ppm[nxt] < ppm[u] else DECISION_REACH_EQUAL)
        if b_val[u] < math.inf and g <= U + EPS:
            fill = (U - g) * ppm[u] + b_val[u]
            if fill < best - EPS:
                best, decision, nxt = fill, DECISION_FILL_UP, b_arg[u]
        return best, decision, nxt

    for u in range(n - 1, -1, -1):
        reach_limit = miles[u] + U - offs[u]
        hi = bisect_right(miles, reach_limit + EPS)
        options: list[tuple[float, float, int]] = []
        for v in range(u + 1, hi):
            d = offs[u] + miles[v] - miles[u] + offs[v]
            if d > U + EPS:
                continue
            if ppm[v] <= ppm[u]:
                c0 = c_empty[v]
                if c0 < math.inf:
                    options.append((d, d * ppm[u] + c0, v))
            else:
                val = evaluate(v, U - d)[0]
                if val < b_val[u]:
                    b_val[u], b_arg[u] = val, v
        d_dest = offs[u] + D - miles[u]
        if d_dest <= U + EPS:
            options.append((d_dest, d_dest * ppm[u], DESTINATION))
        options.sort(key=lambda o: o[0])
        a_dist[u] = [o[0] for o in options]
        vals = [0.0] * len(options)
        args = [DESTINATION] * len(options)
        running, running_arg = math.inf, DESTINATION
        for k in range(len(options) - 1, -1, -1):
            if options[k][1] < running:
                running, running_arg = options[k][1], options[k][2]
            vals[k], args[k] = running, running_arg
        a_val[u], a_arg[u] = vals, args
        c_empty[u] = evaluate(u, 0.0)[0]

    # First stop: any station reachable on the starting fuel.
    best_cost, first, first_g = math.inf, None, 0.0
    for v in range(n):
        d = miles[v] + offs[v]
        if d > F0 + EPS:
            continue
        g = max(F0 - d, 0.0)
        cost = evaluate(v, g)[0]
        if cost < best_cost - EPS:
            best_cost, first, first_g = cost, v, g
    if first is None:
        _raise_infeasible(nodes, D, U, F0)

    stops = _reconstruct(nodes, first, first_g, evaluate, D, U, F0, mpg)
    return FuelPlan(stops=stops, **plan_args)


# ---------------------------------------------------------------------------
def _validate(candidates, D, U, mpg, F0):
    if not (math.isfinite(U) and U > 0):
        raise ValueError("range_miles must be a positive number")
    if not (math.isfinite(mpg) and mpg > 0):
        raise ValueError("mpg must be a positive number")
    if not (math.isfinite(D) and D >= 0):
        raise ValueError("route_miles must be a non-negative number")
    if not (math.isfinite(F0) and 0 <= F0 <= U + EPS):
        raise ValueError("start_fuel_miles must be between 0 and range_miles")
    for c in candidates:
        if not all(math.isfinite(x) for x in (c.mile, c.off_route, c.price)):
            raise ValueError(f"candidate {c.key!r} has a non-finite value")
        if c.price <= 0 or c.off_route < 0 or c.mile < -EPS or c.mile > D + 1e-6:
            raise ValueError(f"candidate {c.key!r} has an invalid mile/off-route/price")


def _prune(candidates, U) -> list[FuelCandidate]:
    """Drop stations that can never be reached and keep only the cheapest of
    stations sharing an identical (mile, off-route) position (visiting a pricier
    twin can only add detour, so it is dominated). Order: (mile, off-route) ascending."""
    best: dict[tuple[float, float], FuelCandidate] = {}
    for c in candidates:
        if c.off_route > U:
            continue
        pos = (round(c.mile, 9), round(c.off_route, 9))
        if pos not in best or c.price < best[pos].price:
            best[pos] = c
    return sorted(best.values(), key=lambda c: (c.mile, c.off_route, c.price))


def _reconstruct(nodes, first, first_g, evaluate, D, U, F0, mpg) -> list[PlannedStop]:
    stops: list[PlannedStop] = []
    u, g = first, first_g
    while True:
        cost_from_u, decision, nxt = evaluate(u, g)
        if not math.isfinite(cost_from_u):  # pragma: no cover - guarded by the DP value
            raise NoFeasiblePlan("Internal error reconstructing the fuel plan.")
        here = nodes[u]
        d_next = (here.off_route + D - here.mile) if nxt == DESTINATION else \
            (here.off_route + nodes[nxt].mile - here.mile + nodes[nxt].off_route)
        if decision == DECISION_FILL_UP:
            purchase, arrival_next = U - g, U - d_next
        else:
            purchase, arrival_next = max(d_next - g, 0.0), 0.0
        stops.append(PlannedStop(
            candidate=here, arrival_fuel_miles=g, purchase_miles=purchase,
            departure_fuel_miles=g + purchase, cost=purchase / mpg * here.price, decision=decision,
            next_candidate=None if nxt == DESTINATION else nodes[nxt], mpg=mpg,
        ))
        if nxt == DESTINATION:
            return _drop_zero_purchase_stops(stops, U, F0)
        u, g = nxt, max(arrival_next, 0.0)


def _drop_zero_purchase_stops(stops: list[PlannedStop], U: float, F0: float) -> list[PlannedStop]:
    """
    Ties can produce a stop where nothing is bought. Skipping it never shortens
    the remaining fuel (d(p, n) <= d(p, s) + d(s, n)), so it is removed and the
    ledger is re-simulated with the same purchases (capped at tank capacity).
    """
    kept = [s for s in stops if s.purchase_miles > 1e-9]
    if len(kept) == len(stops):
        return stops
    fuel, prev_mile, prev_off = F0, 0.0, 0.0  # forward simulation from the start
    for i, s in enumerate(kept):
        fuel -= prev_off + s.candidate.mile - prev_mile + s.candidate.off_route
        s.arrival_fuel_miles = fuel
        s.purchase_miles = min(s.purchase_miles, U - fuel)
        s.departure_fuel_miles = fuel + s.purchase_miles
        s.cost = s.purchase_miles / s.mpg * s.candidate.price
        s.next_candidate = kept[i + 1].candidate if i + 1 < len(kept) else None
        fuel, prev_mile, prev_off = s.departure_fuel_miles, s.candidate.mile, s.candidate.off_route
    return kept


def _raise_infeasible(nodes, D, U, F0):
    """Explain where the chain of reachable stations breaks."""
    reachable = [False] * len(nodes)
    for v, c in enumerate(nodes):
        if c.mile + c.off_route <= F0 + EPS:
            reachable[v] = True
            continue
        for w in range(v):
            if reachable[w] and nodes[w].off_route + c.mile - nodes[w].mile + c.off_route <= U + EPS:
                reachable[v] = True
                break
    last_mile = max([nodes[i].mile for i in range(len(nodes)) if reachable[i]], default=0.0)
    blocked = [nodes[i].mile for i in range(len(nodes)) if not reachable[i] and nodes[i].mile > last_mile]
    gap_to = min(blocked, default=D)
    raise NoFeasiblePlan(
        f"No fuel plan can complete this {D:.1f}-mile route with a {U:.0f}-mile range: "
        f"there is no usable fuel station between mile {last_mile:.1f} and mile {gap_to:.1f} "
        f"({gap_to - last_mile:.1f} miles, including estimated detours).",
        details={"gap_from_mile": round(last_mile, 1), "gap_to_mile": round(gap_to, 1),
                 "range_miles": U, "start_fuel_miles": F0},
    )
