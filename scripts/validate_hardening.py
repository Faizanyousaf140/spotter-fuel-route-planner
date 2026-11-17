"""Reproducible live route audit, independent solver sweep and benchmarks.

Run against an isolated local server with ROUTE_RATE_LIMIT empty during the
load benchmark. Public routing calls remain spaced by the normal provider cap.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import statistics
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ROUTES = [
    ("New York, NY", "Los Angeles, CA"), ("Chicago, IL", "Dallas, TX"),
    ("Washington, DC", "New York, NY"), ("Seattle, WA", "Miami, FL"),
    ("Boston, MA", "San Francisco, CA"), ("Portland, OR", "Denver, CO"),
    ("Denver, CO", "Atlanta, GA"), ("Houston, TX", "Phoenix, AZ"),
    ("Minneapolis, MN", "New Orleans, LA"), ("Detroit, MI", "Nashville, TN"),
    ("Las Vegas, NV", "Salt Lake City, UT"), ("Albuquerque, NM", "Oklahoma City, OK"),
    ("Charlotte, NC", "Tampa, FL"), ("Pittsburgh, PA", "Kansas City, MO"),
    ("Indianapolis, IN", "Memphis, TN"), ("Cleveland, OH", "Richmond, VA"),
    ("Milwaukee, WI", "Austin, TX"), ("Boise, ID", "Sacramento, CA"),
    ("Omaha, NE", "Birmingham, AL"), ("St. Louis, MO", "Jacksonville, FL"),
    ("Buffalo, NY", "Louisville, KY"), ("Raleigh, NC", "Columbus, OH"),
    ("San Antonio, TX", "El Paso, TX"), ("Reno, NV", "Spokane, WA"),
    ("Billings, MT", "Fargo, ND"), ("Little Rock, AR", "Charleston, SC"),
    ("Burlington, VT", "Harrisburg, PA"), ("Baltimore, MD", "Savannah, GA"),
    ("Wichita, KS", "Baton Rouge, LA"), ("Nashville, TN", "Phoenix, AZ"),
]


def dec(value):
    return Decimal(str(value))


def check_ledger(body):
    """Independent checks of published output; does not call planner helpers."""
    sf = body["fuel_cost"]["starting_fuel_estimate"]
    trip = body["fuel_cost"]["purchased_during_trip"]
    usage, stops = body["fuel_usage"], body["fuel_stops"]
    cap, mpg = dec(body["assumptions"]["tank_capacity_gallons"]), dec(body["assumptions"]["vehicle_mpg"])
    assert sum((dec(s["cost"]) for s in stops), Decimal(0)) == dec(trip["cost"])
    assert dec(sf["estimated_value_of_fuel_used"]) + dec(trip["cost"]) == dec(body["fuel_cost"]["total_fuel_cost"])
    assert sum((dec(s["gallons_purchased"]) for s in stops), Decimal(0)) == dec(trip["gallons"])
    fuel = dec(sf["gallons_in_tank_at_start"])
    previous_mile = Decimal(0)
    for stop in stops:
        arrival, departure = dec(stop["fuel_on_arrival_gallons"]), dec(stop["fuel_on_departure_gallons"])
        leg = stop["leg_from_previous"]
        assert fuel - dec(leg["fuel_used_gallons"]) == arrival
        assert arrival + dec(stop["gallons_purchased"]) == departure
        assert 0 <= arrival <= cap and 0 <= departure <= cap
        assert dec(leg["miles"]) <= dec(usage["max_range_miles"]) + dec(.005)
        assert abs(dec(leg["fuel_used_gallons"]) - dec(leg["miles"]) / mpg) <= dec(.0015)
        assert dec(stop["mile_marker"]) >= previous_mile
        expected_cost = (dec(stop["gallons_purchased"]) * dec(stop["price_per_gallon"])).quantize(dec(.01), rounding=ROUND_HALF_UP)
        assert expected_cost == dec(stop["cost"])
        fuel, previous_mile = departure, dec(stop["mile_marker"])
    assert fuel - dec(usage["final_leg"]["fuel_used_gallons"]) == dec(usage["fuel_remaining_at_destination_gallons"])
    assert dec(usage["longest_leg_miles"]) <= dec(usage["max_range_miles"]) + dec(.005)
    assert dec(sf["gallons_in_tank_at_start"]) + dec(trip["gallons"]) - dec(usage["fuel_remaining_at_destination_gallons"]) == dec(usage["fuel_burned_gallons"])
    assert abs(dec(usage["fuel_burned_gallons"]) - dec(usage["total_miles_driven"]) / mpg) <= dec(.001) * (2 * len(stops) + 2)
    assert abs(dec(usage["route_distance_miles"]) + dec(usage["estimated_detour_miles"]) - dec(usage["total_miles_driven"])) <= dec(.011)
    assert body["route"]["geometry"]["type"] == "LineString"
    assert len(body["route"]["geometry"]["coordinates"]) >= 2


def request_plan(base, route):
    started = time.perf_counter()
    response = requests.post(base + "/api/v1/route/", json={"start": route[0], "finish": route[1]}, timeout=(5, 45))
    elapsed = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    body = response.json()
    check_ledger(body)
    return body, elapsed


def summarize_times(values):
    values = sorted(values)
    return {"requests": len(values), "p50_ms": round(statistics.median(values), 1),
            "p95_ms": round(values[max(0, math.ceil(.95 * len(values)) - 1)], 1),
            "min_ms": round(values[0], 1), "max_ms": round(values[-1], 1)}


def optimizer_sweep(count):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    import random
    from route_api.services.fuel_optimizer import FuelCandidate, optimize_fuel_plan
    from route_api.services.errors import NoFeasiblePlan
    from tests.test_fuel_optimizer import brute_force_cost
    rng, feasible = random.Random(20261117), 0
    started = time.perf_counter()
    for case in range(count):
        capacity, route = rng.randint(3, 12), rng.randint(1, 30)
        start = rng.randint(0, capacity)
        cands = [FuelCandidate(i, rng.randint(0, route), rng.choice([0, 0, 1, 2, 3]),
                               round(rng.uniform(2.5, 4.5), 2)) for i in range(rng.randint(0, 9))]
        expected = brute_force_cost(cands, route, capacity, start)
        try:
            got = optimize_fuel_plan(cands, route, range_miles=capacity, mpg=1, start_fuel_miles=start).purchase_cost
        except NoFeasiblePlan:
            got = math.inf
        assert (math.isinf(expected) and math.isinf(got)) or abs(expected - got) < 1e-7, (case, expected, got)
        feasible += math.isfinite(expected)
    return {"cases": count, "feasible": feasible, "mismatches": 0, "elapsed_seconds": round(time.perf_counter() - started, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8011")
    parser.add_argument("--optimizer-cases", type=int, default=20000)
    parser.add_argument("--resume", action="store_true", help="Resume saved completed cold-route checks")
    parser.add_argument("--output", type=Path, default=ROOT / "validation/route_audit.json")
    args = parser.parse_args()
    report = json.loads(args.output.read_text()) if args.resume and args.output.exists() else {"route_audit": [], "benchmarks": {}, "optimizer": optimizer_sweep(args.optimizer_cases)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    cold = [r["elapsed_ms"] for r in report["route_audit"]]
    completed = len(report["route_audit"])
    for number, route in enumerate(ROUTES, 1):
        if number <= completed:
            continue
        body, elapsed = request_plan(args.base_url, route)
        assert body["meta"]["map_api_calls"]["routing"] == 1, (route, "expected an uncached route")
        assert body["meta"]["map_api_calls"]["geocoding"] == 0
        cold.append(elapsed)
        report["route_audit"].append({"start": route[0], "finish": route[1], "passed": True,
            "distance_miles": body["route"]["distance_miles"], "stops": len(body["fuel_stops"]),
            "total_cost": body["fuel_cost"]["total_fuel_cost"], "longest_leg_miles": body["fuel_usage"]["longest_leg_miles"],
            "routing_calls": body["meta"]["map_api_calls"]["routing"], "elapsed_ms": round(elapsed, 1),
            "timings_ms": body["meta"]["timings_ms"], "plan_id": body["plan_id"]})
        save()
        print(f"audit {number}/30: {route[0]} -> {route[1]}: {len(body['fuel_stops'])} stops, ${body['fuel_cost']['total_fuel_cost']:.2f}, {elapsed:.0f} ms", flush=True)
    report["benchmarks"]["cold_30_different_routes"] = summarize_times(cold)
    warm = []
    for route in ROUTES[:3]:
        for _ in range(5):
            body, elapsed = request_plan(args.base_url, route)
            assert body["meta"]["map_api_calls"]["routing"] == 0 and body["meta"]["plan_cache_hit"]
            warm.append(elapsed)
    report["benchmarks"]["warm"] = summarize_times(warm)
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: request_plan(args.base_url, ROUTES[i % 3]), range(48)))
    for body, elapsed in results:
        assert body["meta"]["map_api_calls"]["routing"] == 0 and body["meta"]["plan_cache_hit"]
    report["benchmarks"]["concurrent_cached_8_clients_48_requests"] = {
        **summarize_times([elapsed for _, elapsed in results]),
        "requests_per_second": round(48 / (time.perf_counter() - started), 2), "routing_calls": 0}
    save()
    print(json.dumps({"routes_passed": len(report["route_audit"]), "benchmarks": report["benchmarks"],
                      "optimizer": report["optimizer"]}, indent=2), flush=True)


if __name__ == "__main__":
    main()
