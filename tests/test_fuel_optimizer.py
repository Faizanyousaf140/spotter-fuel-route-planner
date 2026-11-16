"""
Fuel optimisation tests.

Model under test (README, "Fuel model and assumptions"):
  * fuel is measured in miles of range; capacity = range (500 mi = 50 gal at 10 mpg)
  * stops are visited in non-decreasing (mile, off_route) order (nearest-first at equal miles)
  * legs between consecutive stops i -> j cost  off_i + (mile_j - mile_i) + off_j  miles of fuel
    (start and destination have off = 0); off-route distances are estimates
  * fuel must never go below 0 or above capacity; purchases are continuous
  * objective: minimise money spent at stops (sum gallons x price)

Hand-calculated cases use range 500 mi / 10 mpg. Randomised cases compare the
dynamic programme against an independent exhaustive solver.
"""

import itertools
import math
import random
import time

from django.test import SimpleTestCase

from route_api.services.errors import NoFeasiblePlan
from route_api.services.fuel_optimizer import FuelCandidate, optimize_fuel_plan


def cand(name, mile, price, off=0.0):
    return FuelCandidate(key=name, mile=float(mile), off_route=float(off), price=float(price))


def plan_for(cands, route_miles, **kw):
    kw.setdefault("range_miles", 500.0)
    kw.setdefault("mpg", 10.0)
    return optimize_fuel_plan(cands, route_miles, **kw)


def stops(plan):
    return [(s.candidate.key, round(s.purchase_gallons, 6)) for s in plan.stops]


def assert_ledger_consistent(test, plan):
    """Fuel never negative / above capacity; purchases and burn balance."""
    eps = 1e-6
    fuel = plan.start_fuel_miles
    position = (0.0, 0.0)  # (mile, off) of the previous node; start has off 0
    for stop in plan.stops:
        leg = position[1] + (stop.candidate.mile - position[0]) + stop.candidate.off_route
        fuel -= leg
        test.assertGreaterEqual(fuel, -eps, "fuel went negative")
        test.assertAlmostEqual(fuel, stop.arrival_fuel_miles, places=6)
        test.assertGreater(stop.purchase_miles, 0, "a stop must buy fuel")
        fuel += stop.purchase_miles
        test.assertLessEqual(fuel, plan.range_miles + eps, "tank over capacity")
        test.assertAlmostEqual(fuel, stop.departure_fuel_miles, places=6)
        test.assertAlmostEqual(stop.cost, stop.purchase_miles / plan.mpg * stop.candidate.price, places=6)
        position = (stop.candidate.mile, stop.candidate.off_route)
    fuel -= position[1] + (plan.route_miles - position[0])
    test.assertGreaterEqual(fuel, -eps, "fuel went negative on the final leg")
    test.assertAlmostEqual(fuel, plan.end_fuel_miles, places=6)
    test.assertAlmostEqual(
        plan.start_fuel_miles + plan.purchased_miles - plan.burned_miles, plan.end_fuel_miles, places=6
    )
    test.assertAlmostEqual(plan.purchase_cost, sum(s.cost for s in plan.stops), places=6)


class HandCalculatedTests(SimpleTestCase):
    def test_under_500_miles_needs_no_stop(self):
        plan = plan_for([cand("A", 100, 2.0)], 300)
        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.purchase_cost, 0)
        self.assertAlmostEqual(plan.burned_miles, 300)
        self.assertAlmostEqual(plan.end_fuel_miles, 200)  # 20 gallons left, not credited
        assert_ledger_consistent(self, plan)

    def test_exactly_500_miles_needs_no_stop(self):
        plan = plan_for([cand("A", 250, 2.0)], 500)
        self.assertEqual(plan.stops, [])
        self.assertAlmostEqual(plan.end_fuel_miles, 0)
        assert_ledger_consistent(self, plan)

    def test_just_over_500_miles_buys_only_what_is_needed(self):
        # Arrive at A (mile 250) with 250 mi; need 250.5 to finish -> buy 0.5 mi = 0.05 gal.
        plan = plan_for([cand("A", 250, 3.0)], 500.5)
        self.assertEqual(stops(plan), [("A", 0.05)])
        self.assertAlmostEqual(plan.purchase_cost, 0.15)
        self.assertAlmostEqual(plan.end_fuel_miles, 0)
        assert_ledger_consistent(self, plan)

    def test_cheaper_station_ahead_buy_just_enough_to_reach_it(self):
        # A@400 $4.00, B@800 $3.00, trip 1200. Arrive A with 100 mi. Cheaper B is in range ->
        # buy 300 mi (30 gal x $4 = $120). Arrive B empty, buy 400 mi (40 gal x $3 = $120). Total $240.
        # (Filling up at A instead would cost $160 + $90 = $250.)
        plan = plan_for([cand("A", 400, 4.0), cand("B", 800, 3.0)], 1200)
        self.assertEqual(stops(plan), [("A", 30), ("B", 40)])
        self.assertAlmostEqual(plan.purchase_cost, 240.0)
        self.assertEqual([s.decision for s in plan.stops], ["reach_cheaper_station", "reach_destination"])
        assert_ledger_consistent(self, plan)

    def test_no_cheaper_station_ahead_fill_up(self):
        # A@400 $3.00, B@800 $4.00, trip 1200. At A fill up: 400 mi (40 gal x $3 = $120).
        # Arrive B with 100, buy 300 mi (30 gal x $4 = $120). Total $240.
        # (Buying just enough at A would cost $90 + $160 = $250.)
        plan = plan_for([cand("A", 400, 3.0), cand("B", 800, 4.0)], 1200)
        self.assertEqual(stops(plan), [("A", 40), ("B", 30)])
        self.assertAlmostEqual(plan.purchase_cost, 240.0)
        self.assertEqual(plan.stops[0].decision, "fill_up")
        assert_ledger_consistent(self, plan)

    def test_cheaper_distant_station_is_preferred(self):
        # A@100 $3.50 is skipped: B@450 $2.50 is reachable on the starting tank.
        # B: arrive 50, destination 550 away (> range) and next station C is pricier -> fill up:
        #    450 mi = 45 gal x $2.50 = $112.50. C@600: arrive 350, buy 50 mi = 5 gal x $3.60 = $18.
        plan = plan_for([cand("A", 100, 3.5), cand("B", 450, 2.5), cand("C", 600, 3.6)], 1000)
        self.assertEqual(stops(plan), [("B", 45), ("C", 5)])
        self.assertAlmostEqual(plan.purchase_cost, 130.5)
        assert_ledger_consistent(self, plan)

    def test_expensive_detour_makes_cheaper_price_lose(self):
        # X@400 on-route $3.50: arrive 100, buy 300 mi = 30 gal -> $105.
        # Y@400, 20 mi off-route, $3.20: leg 420 -> arrive 80, then 420 to destination
        #   -> buy 340 mi = 34 gal -> $108.80. X wins despite the higher price.
        plan = plan_for([cand("X", 400, 3.5), cand("Y", 400, 3.2, off=20)], 800)
        self.assertEqual(stops(plan), [("X", 30)])
        self.assertAlmostEqual(plan.purchase_cost, 105.0)
        self.assertAlmostEqual(plan.detour_miles, 0)

    def test_small_detour_to_cheaper_station_wins(self):
        # Y 5 mi off-route: leg 405 -> arrive 95, 405 to destination -> buy 310 mi = 31 gal x $3.20 = $99.20.
        plan = plan_for([cand("X", 400, 3.5), cand("Y", 400, 3.2, off=5)], 800)
        self.assertEqual(stops(plan), [("Y", 31)])
        self.assertAlmostEqual(plan.purchase_cost, 99.2)
        self.assertAlmostEqual(plan.detour_miles, 10)
        self.assertAlmostEqual(plan.burned_miles, 810)
        assert_ledger_consistent(self, plan)

    def test_identical_mile_markers_choose_cheapest(self):
        plan = plan_for([cand("P", 400, 3.4), cand("Q", 400, 3.1), cand("R", 400, 3.25)], 800)
        self.assertEqual(stops(plan), [("Q", 30)])
        assert_ledger_consistent(self, plan)

    def test_multiple_stops_cross_country(self):
        # Stations every 300 mi with varying prices on a 1,800-mile trip.
        prices = {300: 3.9, 600: 3.1, 900: 3.6, 1200: 2.9, 1500: 3.8}
        cands = [cand(f"S{m}", m, p) for m, p in prices.items()]
        plan = plan_for(cands, 1800)
        # Hand solution: start 500 -> S300 (arrive 200; S600 cheaper & in range -> buy 100 mi @3.9 = $39)
        # S600 arrive 0 -> S1200 cheaper but 600 away (> range), S900 pricier -> fill 500 mi @3.1 = $155
        # S900 arrive 200 -> S1200 cheaper, buy 100 mi @3.6 = $36; S1200 arrive 0 -> fill? destination
        # 600 away, S1500 pricier -> fill 500 mi @2.9 = $145; S1500 arrive 200, buy 100 mi @3.8 = $38.
        self.assertEqual(stops(plan), [("S300", 10), ("S600", 50), ("S900", 10), ("S1200", 50), ("S1500", 10)])
        self.assertAlmostEqual(plan.purchase_cost, 39 + 155 + 36 + 145 + 38)
        assert_ledger_consistent(self, plan)


    def test_same_mile_marker_visited_nearest_first(self):
        # Regression (found by the brute-force comparison): only b is reachable on the starting fuel;
        # the optimum visits b (on-route, pricey), buys just enough to reach a (same mile, 2 mi off,
        # cheap), fills there. Capacity 8, start fuel 1, route 13, mpg 1 -> optimal cost 50.
        cands = [cand("a", 1, 2.0, off=2), cand("b", 1, 4.25), cand("c", 3, 4.25), cand("d", 4, 4.25, off=1),
                 cand("e", 7, 4.25), cand("f", 13, 2.0), cand("g", 13, 4.0)]
        plan = optimize_fuel_plan(cands, 13, range_miles=8, mpg=1, start_fuel_miles=1)
        self.assertAlmostEqual(plan.purchase_cost, 50.0)
        self.assertEqual([s.candidate.key for s in plan.stops], ["b", "a", "e"])
        assert_ledger_consistent(self, plan)


class InfeasibleTests(SimpleTestCase):
    def test_gap_over_500_miles(self):
        with self.assertRaises(NoFeasiblePlan) as ctx:
            plan_for([cand("A", 400, 3.0), cand("B", 950, 3.0)], 1200)
        details = ctx.exception.details
        self.assertEqual((details["gap_from_mile"], details["gap_to_mile"]), (400.0, 950.0))
        self.assertIn("550", ctx.exception.message)

    def test_destination_unreachable_after_last_station(self):
        with self.assertRaises(NoFeasiblePlan) as ctx:
            plan_for([cand("A", 100, 3.0)], 700)
        self.assertEqual(ctx.exception.details["gap_to_mile"], 700.0)

    def test_no_stations_on_long_route(self):
        with self.assertRaises(NoFeasiblePlan):
            plan_for([], 501)

    def test_insufficient_starting_fuel_to_reach_any_station(self):
        with self.assertRaises(NoFeasiblePlan) as ctx:
            plan_for([cand("A", 150, 3.0)], 400, start_fuel_miles=100)
        self.assertEqual(ctx.exception.details["gap_from_mile"], 0.0)

    def test_detour_makes_station_unreachable(self):
        # Station at mile 495 but 10 mi off-route: needs 505 mi on a 500 mi tank.
        with self.assertRaises(NoFeasiblePlan):
            plan_for([cand("A", 495, 3.0, off=10)], 900)

    def test_empty_start_with_station_at_origin(self):
        plan = plan_for([cand("A", 0, 3.0)], 300, start_fuel_miles=0)
        self.assertEqual(stops(plan), [("A", 30)])
        assert_ledger_consistent(self, plan)

    def test_invalid_inputs(self):
        for kwargs in ({"range_miles": 0}, {"mpg": 0}, {"start_fuel_miles": 600}, {"start_fuel_miles": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                plan_for([], 100, **kwargs)
        with self.assertRaises(ValueError):
            plan_for([cand("A", 10, float("nan"))], 100)
        with self.assertRaises(ValueError):
            plan_for([cand("A", -5, 3.0)], 100)


# ---------------------------------------------------------------------------
# Independent exhaustive solver (shares no code with the optimiser).
# With integer distances, capacity and start fuel, the fixed-stop-set problem is
# an LP with a consecutive-ones (totally unimodular) constraint matrix, so an
# optimal solution with integer purchases exists; enumerating every subset of
# stations and every integer purchase therefore finds the true optimum.
# ---------------------------------------------------------------------------
def brute_force_cost(cands, route_miles, capacity, start_fuel, mpg=1.0):
    best = math.inf

    def dfs(used, prev_mile, prev_off, fuel, cost):
        nonlocal best
        if cost >= best:
            return
        # option: drive straight to the destination
        if fuel - (prev_off + route_miles - prev_mile) >= 0:
            best = min(best, cost)
        # next stop: ANY unused station not behind us in (mile, off_route) order
        # (any order among stations at an identical position)
        for j, c in enumerate(cands):
            if j in used or (c.mile, c.off_route) < (prev_mile, prev_off):
                continue
            arrive = fuel - (prev_off + c.mile - prev_mile + c.off_route)
            if arrive < 0:
                continue
            for buy in range(0, int(capacity - arrive) + 1):
                dfs(used | {j}, c.mile, c.off_route, arrive + buy, cost + buy / mpg * c.price)

    dfs(frozenset(), 0.0, 0.0, start_fuel, 0.0)
    return best


class DPMatchesBruteForceTests(SimpleTestCase):
    def test_random_small_instances(self):
        rng = random.Random(20261116)
        checked = feasible = 0
        for _ in range(600):
            capacity = rng.randint(4, 9)
            route = rng.randint(1, 20)
            n = rng.randint(0, 7)
            cands = [
                cand(f"c{k}", rng.randint(0, route), rng.choice([2.0, 2.5, 3.0, 3.5, 4.0, 4.25]),
                     off=rng.choice([0, 0, 0, 1, 2]))
                for k in range(n)
            ]
            start = rng.randint(0, capacity)
            expected = brute_force_cost(cands, route, capacity, start)
            try:
                plan = optimize_fuel_plan(cands, route, range_miles=capacity, mpg=1.0, start_fuel_miles=start)
                got = plan.purchase_cost
                assert_ledger_consistent(self, plan)
            except NoFeasiblePlan:
                got = math.inf
            with self.subTest(case=(capacity, route, start, [(c.mile, c.off_route, c.price) for c in cands])):
                if math.isinf(expected):
                    self.assertTrue(math.isinf(got), "optimiser found a plan the brute force says is impossible")
                else:
                    self.assertAlmostEqual(got, expected, places=9)
                    feasible += 1
            checked += 1
        print(f"\n  brute-force comparison: {checked} cases, {feasible} feasible, {checked - feasible} infeasible")
        self.assertEqual(checked, 600)
        self.assertGreater(feasible, 250)  # make sure the sample is not dominated by infeasible cases

    def test_identical_markers_and_ties_against_brute_force(self):
        rng = random.Random(7)
        for _ in range(150):
            mile = rng.randint(1, 5)
            cands = [cand(f"t{k}", mile + rng.choice([0, 0, 3]), rng.choice([3.0, 3.0, 2.5]), off=rng.choice([0, 1]))
                     for k in range(rng.randint(1, 5))]
            route, capacity, start = rng.randint(9, 14), rng.randint(5, 8), rng.randint(2, 8)
            start = min(start, capacity)
            expected = brute_force_cost(cands, route, capacity, start)
            try:
                got = optimize_fuel_plan(cands, route, range_miles=capacity, mpg=1.0, start_fuel_miles=start).purchase_cost
            except NoFeasiblePlan:
                got = math.inf
            if math.isinf(expected):
                self.assertTrue(math.isinf(got))
            else:
                self.assertAlmostEqual(got, expected, places=9)


class PerformanceTests(SimpleTestCase):
    def test_realistic_route_with_600_candidates_is_fast(self):
        rng = random.Random(1)
        route = 2800.0
        cands = [cand(f"r{k}", rng.uniform(0, route), rng.uniform(2.7, 4.5), off=rng.uniform(0, 15))
                 for k in range(600)]
        t0 = time.perf_counter()
        plan = plan_for(cands, route)
        elapsed = time.perf_counter() - t0
        assert_ledger_consistent(self, plan)
        self.assertGreater(len(plan.stops), 5)
        self.assertLess(elapsed, 1.0, f"optimiser took {elapsed:.3f}s")
