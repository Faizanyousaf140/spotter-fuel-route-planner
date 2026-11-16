"""
REAL external-API integration tests (not mocked). Skipped unless RUN_LIVE_TESTS=1.

    RUN_LIVE_TESTS=1 python manage.py test tests.test_live_integration

Each test makes at most one call to the public OSRM demo server (requests are spaced >= 1.1 s).
"""

import csv
import os
import time
import unittest
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from route_api.services.geocoding_service import resolve_location
from route_api.services.route_matching import build_route_profile, match_stations
from route_api.services.routing_service import get_route
from route_api.services.station_service import StationIndex, StationRecord, reset_station_index

from .helpers import LOCMEM_CACHE

LIVE = os.environ.get("RUN_LIVE_TESTS") == "1"


def stations_from_processed_csv() -> StationIndex:
    path = Path(settings.BASE_DIR) / "data" / "processed" / "fuel_stations.csv"
    with open(path, newline="", encoding="utf-8") as handle:
        return StationIndex([
            StationRecord(int(r["opis_id"]), r["name"], r["address"], r["city"], r["state"], float(r["price"]),
                          float(r["latitude"]), float(r["longitude"]), r["geocode_source"])
            for r in csv.DictReader(handle)
        ])


@unittest.skipUnless(LIVE, "set RUN_LIVE_TESTS=1 to call the real OSRM demo server")
@override_settings(CACHES=LOCMEM_CACHE)
class LiveOSRMTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        time.sleep(1.1)  # OSRM demo server policy: max 1 request/second

    def test_real_route_and_matching_chicago_to_st_louis(self):
        start, finish = resolve_location("Chicago, IL"), resolve_location("St. Louis, MO")
        route = get_route(start.point, finish.point)
        self.assertEqual(route.provider_calls, 1)
        self.assertGreater(route.distance_miles, 250)   # ~300 road miles
        self.assertLess(route.distance_miles, 350)
        self.assertGreater(len(route.coordinates), 100)
        refs = set().union(*(seg.refs for seg in route.road_segments))
        self.assertIn("I55", refs)  # polyline6 + steps parsing against the real provider

        profile = build_route_profile(route.coordinates, route.distance_miles)
        self.assertAlmostEqual(profile.scale, 1.0, delta=0.01)  # geometry length ~= reported distance
        matches = match_stations(profile, stations_from_processed_csv(), corridor_miles=15)
        self.assertGreater(len(matches), 10)
        markers = [m.mile_marker for m in matches]
        self.assertEqual(markers, sorted(markers))
        self.assertTrue(all(0 <= m <= route.distance_miles + 1e-6 for m in markers))
        self.assertIn("IL", {m.station.state for m in matches})

        again = get_route(start.point, finish.point)
        self.assertEqual((again.provider_calls, again.from_cache), (0, True))

    def test_real_no_route_hawaii(self):
        from route_api.services.errors import RouteNotFound

        with self.assertRaises(RouteNotFound):
            get_route(resolve_location("Los Angeles, CA").point, resolve_location("Honolulu, HI").point)


@unittest.skipUnless(LIVE, "set RUN_LIVE_TESTS=1 to call the real OSRM demo server")
@override_settings(CACHES=LOCMEM_CACHE)
class LiveEndToEndApiTests(TestCase):
    """Full HTTP request -> real OSRM -> real station data -> saved plan -> map page."""

    @classmethod
    def setUpTestData(cls):
        from io import StringIO

        from django.core.management import call_command

        call_command("load_stations", stdout=StringIO())

    def setUp(self):
        cache.clear()
        reset_station_index()
        self.addCleanup(reset_station_index)
        time.sleep(1.1)

    def test_post_route_multi_stop_and_cache(self):
        body = {"start": "Chicago, IL", "finish": "Dallas, TX"}
        first = self.client.post("/api/v1/route/", body, content_type="application/json")
        self.assertEqual(first.status_code, 201, first.content)
        b = first.json()
        self.assertEqual(b["meta"]["map_api_calls"]["routing"], 1)
        self.assertGreater(b["route"]["distance_miles"], 850)
        self.assertGreaterEqual(len(b["fuel_stops"]), 1)
        cost = b["fuel_cost"]
        self.assertAlmostEqual(cost["total_fuel_cost"], cost["starting_fuel_estimate"]["estimated_value_of_fuel_used"]
                               + cost["purchased_during_trip"]["cost"], places=1)
        self.assertEqual(self.client.get(f"/api/v1/route/{b['plan_id']}/map/").status_code, 200)
        self.assertGreater(b["data_quality"]["location_confidence"]["high"], 0)

        second = self.client.post("/api/v1/route/", body, content_type="application/json").json()
        self.assertEqual(second["meta"]["map_api_calls"]["routing"], 0)
        self.assertEqual(second["fuel_cost"], b["fuel_cost"])
