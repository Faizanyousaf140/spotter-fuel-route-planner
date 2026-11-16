"""
HTTP API tests (Django test client + DRF). The routing provider is MOCKED
(FakeSession); start/finish resolve offline from the real Census gazetteer;
stations are synthetic rows placed along the mocked route.
"""

from unittest import mock
from uuid import uuid4

import numpy as np
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from route_api.models import FuelStation, RoutePlan
from route_api.services import routing_service
from route_api.services.gazetteer import get_place_index
from route_api.services.routing_service import OSRMProvider
from route_api.services.station_service import reset_station_index

from .helpers import LOCMEM_CACHE, FakeResponse, FakeSession, osrm_ok, requests_conn_error, requests_timeout

ROUTE_URL = "/api/v1/route/"


def city(name, state):
    place = get_place_index().lookup(name, state).place
    return place.lat, place.lon


CHICAGO, DALLAS = city("Chicago", "IL"), city("Dallas", "TX")
ROUTE_MILES = 960.0


def straight_route(a, b, points=200):
    return [(float(lat), float(lon)) for lat, lon in zip(np.linspace(a[0], b[0], points), np.linspace(a[1], b[1], points))]


def add_station(opis_id, fraction, price, name=None):
    lat = CHICAGO[0] + (DALLAS[0] - CHICAGO[0]) * fraction
    lon = CHICAGO[1] + (DALLAS[1] - CHICAGO[1]) * fraction
    return FuelStation.objects.create(
        opis_id=opis_id, name=name or f"STATION {opis_id}", address="I-55, EXIT 1", city="Testville", state="MO",
        rack_ids="1", price=price, price_min=price, price_max=price, latitude=lat, longitude=lon,
        geocode_source="census_exact_place",
    )


@override_settings(CACHES=LOCMEM_CACHE)
class RouteApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        reset_station_index()
        self.session = FakeSession()
        patcher = mock.patch.object(routing_service, "get_provider",
                                    side_effect=lambda cfg=None: OSRMProvider(routing_service.settings.FUEL_PLANNER,
                                                                              session=self.session))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(reset_station_index)

    def queue_route(self, n=1):
        for _ in range(n):
            self.session.outcomes.append(osrm_ok(straight_route(CHICAGO, DALLAS), distance_m=ROUTE_MILES * 1609.344,
                                                 duration_s=15 * 3600))

    def post(self, body):
        return self.client.post(ROUTE_URL, body, content_type="application/json")

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        body = response.json()
        self.assertEqual(set(body), {"error"})
        self.assertEqual(body["error"]["code"], code)
        self.assertIsInstance(body["error"]["message"], str)
        self.assertIsInstance(body["error"]["details"], dict)
        return body["error"]


class SuccessfulPlanTests(RouteApiTestCase):
    def setUp(self):
        super().setUp()
        # Chicago -> Dallas, 960 mi: stations at ~mile 300 ($3.50), 480 ($3.00), 720 ($3.20).
        add_station(1, 300 / ROUTE_MILES, "3.50000", "NORTH STOP")
        add_station(2, 480 / ROUTE_MILES, "3.00000", "MIDDLE STOP")
        add_station(3, 720 / ROUTE_MILES, "3.20000", "SOUTH STOP")
        add_station(4, 0.0, "3.60000", "ORIGIN STOP")
        self.queue_route()

    def test_response_structure_and_values(self):
        r = self.post({"start": "Chicago, IL", "finish": "Dallas, TX"})
        self.assertEqual(r.status_code, 201, r.content)
        b = r.json()
        for key in ("plan_id", "map_url", "plan_url", "start", "finish", "route", "fuel_stops", "fuel_cost",
                    "fuel_usage", "assumptions", "warnings", "meta"):
            self.assertIn(key, b)
        self.assertEqual(r["Location"], b["plan_url"])
        self.assertTrue(b["map_url"].endswith(f"/api/v1/route/{b['plan_id']}/map/"))
        self.assertEqual(b["start"]["resolved_by"], "census_gazetteer")
        self.assertEqual(b["route"]["geometry"]["type"], "LineString")
        self.assertGreaterEqual(len(b["route"]["geometry"]["coordinates"]), 2)
        lon, lat = b["route"]["geometry"]["coordinates"][0]
        self.assertAlmostEqual(lat, CHICAGO[0], places=3)  # GeoJSON order is [lon, lat]
        self.assertAlmostEqual(b["route"]["distance_miles"], ROUTE_MILES, places=1)

        # Hand check: start full (500 mi). Middle stop (~mile 480) is the cheapest reachable;
        # from there 480 mi to Dallas fits in one tank -> one stop, buy just enough.
        stops = b["fuel_stops"]
        self.assertEqual([s["name"] for s in stops], ["MIDDLE STOP"])
        stop = stops[0]
        for key in ("sequence", "opis_id", "name", "address", "city", "state", "location", "price_per_gallon",
                    "mile_marker", "distance_from_route_miles", "estimated_detour_miles", "fuel_on_arrival_gallons",
                    "gallons_purchased", "fuel_on_departure_gallons", "cost", "decision", "reason"):
            self.assertIn(key, stop)
        # Stations are placed by lat/lon fraction, which is not exactly the distance fraction.
        self.assertAlmostEqual(stop["mile_marker"], 480, delta=10)
        self.assertAlmostEqual(stop["gallons_purchased"], 46.0, delta=0.2)   # (960 - 500) / 10 + tiny detour
        self.assertAlmostEqual(stop["cost"], stop["gallons_purchased"] * 3.0, delta=0.01)

        cost = b["fuel_cost"]
        self.assertEqual(cost["starting_fuel_estimate"]["valued_at_station"]["name"], "ORIGIN STOP")
        self.assertEqual(cost["starting_fuel_estimate"]["estimated_value_of_fuel_used"], 180.0)  # 50 gal x $3.60
        self.assertAlmostEqual(cost["total_fuel_cost"],
                               cost["starting_fuel_estimate"]["estimated_value_of_fuel_used"]
                               + cost["purchased_during_trip"]["cost"], places=2)
        self.assertEqual(cost["purchased_during_trip"]["number_of_stops"], 1)
        usage = b["fuel_usage"]
        self.assertAlmostEqual(usage["fuel_burned_gallons"], 50 + cost["purchased_during_trip"]["gallons"], places=2)
        self.assertEqual(usage["fuel_remaining_at_destination_gallons"], 0.0)
        self.assertEqual(b["meta"]["map_api_calls"], {"routing": 1, "geocoding": 0})
        self.assertFalse(b["meta"]["route_cache_hit"])
        self.assertTrue(any("approximate" in w for w in b["warnings"]))

    def test_plan_is_persisted_and_retrievable(self):
        b = self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}).json()
        plan = RoutePlan.objects.get(pk=b["plan_id"])
        self.assertEqual(plan.start_query, "Chicago, IL")
        self.assertEqual(float(plan.total_fuel_cost), b["fuel_cost"]["total_fuel_cost"])
        detail = self.client.get(f"{ROUTE_URL}{b['plan_id']}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["fuel_stops"], b["fuel_stops"])

    def test_saved_map_renders_after_simulated_restart(self):
        b = self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}).json()
        cache.clear()            # cache lost
        reset_station_index()    # in-memory state lost
        r = self.client.get(reverse("route-plan-map", args=[b["plan_id"]]))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("leaflet@1.9.4", html)
        self.assertIn("MIDDLE STOP", html)
        self.assertIn('id="plan-data"', html)
        self.assertIn("OpenStreetMap contributors", html)

    def test_unknown_plan_ids(self):
        self.assertEqual(self.client.get(reverse("route-plan-map", args=[uuid4()])).status_code, 404)
        self.assert_error(self.client.get(f"{ROUTE_URL}{uuid4()}/"), 404, "not_found")

    def test_second_identical_request_uses_route_cache(self):
        first = self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}).json()
        second = self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}).json()
        self.assertEqual(len(self.session.calls), 1)   # only one provider call in total
        self.assertEqual(second["meta"]["map_api_calls"]["routing"], 0)
        self.assertTrue(second["meta"]["route_cache_hit"])
        self.assertNotEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(first["fuel_cost"], second["fuel_cost"])
        self.assertEqual(RoutePlan.objects.count(), 2)

    def test_coordinate_and_object_inputs(self):
        r = self.post({"start": f"{CHICAGO[0]},{CHICAGO[1]}", "finish": {"lat": DALLAS[0], "lng": DALLAS[1]}})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["start"]["resolved_by"], "coordinates")

    def test_health(self):
        body = self.client.get("/api/v1/health/").json()
        self.assertEqual((body["status"], body["stations_loaded"]), ("ok", 4))


class ValidationErrorTests(RouteApiTestCase):
    def test_missing_start_and_finish(self):
        err = self.assert_error(self.post({"finish": "Dallas, TX"}), 400, "invalid_request")
        self.assertIn("start", err["details"])
        err = self.assert_error(self.post({"start": "Chicago, IL"}), 400, "invalid_request")
        self.assertIn("finish", err["details"])
        err = self.assert_error(self.post({}), 400, "invalid_request")
        self.assertEqual(set(err["details"]), {"start", "finish"})

    def test_invalid_values(self):
        err = self.assert_error(self.post({"start": 42, "finish": "Dallas, TX"}), 400, "invalid_request")
        self.assertIn('{"lat"', err["details"]["start"][0])  # message renders literal braces
        for body in ({"start": "", "finish": "Dallas, TX"}, {"start": 42, "finish": "Dallas, TX"},
                     {"start": ["Chicago"], "finish": "Dallas, TX"}, {"start": {"lat": 95, "lon": 0}, "finish": "Dallas, TX"},
                     {"start": "x" * 201, "finish": "Dallas, TX"},
                     {"start": "Chicago, IL", "finish": "Dallas, TX", "corridor_miles": 0},
                     {"start": "Chicago, IL", "finish": "Dallas, TX", "corridor_miles": "wide"}):
            with self.subTest(body=body):
                self.assert_error(self.post(body), 400, "invalid_request")
        self.assertEqual(self.session.calls, [])  # validation happens before any external call

    def test_malformed_json_and_wrong_method(self):
        self.assert_error(self.client.post(ROUTE_URL, "{oops", content_type="application/json"), 400, "malformed_request")
        self.assert_error(self.client.get(ROUTE_URL), 405, "method_not_allowed")
        self.assert_error(self.client.post(ROUTE_URL, "start=a", content_type="text/plain"), 415, "unsupported_media_type")

    def test_non_usa_locations(self):
        add_station(1, 0.5, "3.0")
        for body in ({"start": "Toronto, ON", "finish": "Dallas, TX"}, {"start": "Chicago, IL", "finish": "51.5074,-0.1278"}):
            with self.subTest(body=body):
                self.assert_error(self.post(body), 422, "location_outside_usa")
        self.assertEqual(self.session.calls, [])

    @override_settings(FUEL_PLANNER={**routing_service.settings.FUEL_PLANNER, "GEOCODER_FALLBACK_ENABLED": False})
    def test_unknown_location(self):
        self.assert_error(self.post({"start": "Nowhereville Zzz", "finish": "Dallas, TX"}), 422, "location_not_found")


class ProviderAndDataFailureTests(RouteApiTestCase):
    def setUp(self):
        super().setUp()
        add_station(1, 0.5, "3.0")

    def test_no_route(self):
        self.session.outcomes.append(FakeResponse(400, {"code": "NoRoute", "message": "Impossible route"}))
        err = self.assert_error(self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}), 422, "route_not_found")
        self.assertEqual(err["details"]["provider_code"], "NoRoute")

    def test_provider_timeout(self):
        self.session.outcomes.append(requests_timeout)
        self.assert_error(self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}), 504, "routing_timeout")

    def test_provider_unreachable(self):
        self.session.outcomes.append(requests_conn_error)
        self.assert_error(self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}), 503, "routing_unavailable")

    def test_provider_bad_gateway(self):
        self.session.outcomes.append(FakeResponse(502, None))
        self.assert_error(self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}), 502, "routing_provider_error")
        self.assertEqual(RoutePlan.objects.count(), 0)   # nothing fabricated or saved

    def test_no_feasible_fuel_plan(self):
        # Only one station at the middle of a 960-mile route: 480 + 480 is fine, so use a long route instead.
        self.session.outcomes.append(osrm_ok(straight_route(CHICAGO, DALLAS), distance_m=1300 * 1609.344))
        err = self.assert_error(self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}), 422, "no_feasible_fuel_plan")
        self.assertEqual(err["details"]["route_distance_miles"], 1300.0)
        self.assertIn("gap_from_mile", err["details"])
        self.assertEqual(RoutePlan.objects.count(), 0)

    def test_empty_station_dataset(self):
        FuelStation.objects.all().delete()
        reset_station_index()
        self.queue_route()
        self.assert_error(self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}), 503, "station_data_unavailable")
        health = self.client.get("/api/v1/health/")
        self.assertEqual(health.status_code, 503)
        self.assertEqual(health.json()["status"], "degraded")

    def test_unexpected_error_returns_json_500(self):
        with mock.patch("route_api.views.plan_route", side_effect=RuntimeError("boom")):
            self.assert_error(self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}), 500, "internal_error")


class DemoPageTests(TestCase):
    def test_demo_page(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"/api/v1/route/", r.content)
