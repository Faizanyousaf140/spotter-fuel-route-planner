"""
Robustness: concurrency (single-flight routing), cache corruption recovery, rate
limiting, zero-length routes, hostile input, highway confidence end-to-end and the
planner's invariant guard. External services are MOCKED.
"""

import threading
import time
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from route_api.services import routing_service
from route_api.services.errors import PlannerError
from route_api.services.fuel_optimizer import FuelCandidate, optimize_fuel_plan
from route_api.services.planner import _check_plan_invariants
from route_api.services.routing_service import OSRMProvider, get_route, route_cache_key

from .helpers import LOCMEM_CACHE, osrm_ok, requests_timeout
from .test_api import CHICAGO, DALLAS, ROUTE_MILES, RouteApiTestCase, add_station, straight_route

NYC, LA = (40.7128, -74.0060), (34.0522, -118.2437)


class SlowSession:
    """Thread-safe fake HTTP session that answers slowly and counts calls."""

    def __init__(self, delay=0.3, outcome_factory=None):
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()
        self.outcome_factory = outcome_factory or (lambda: osrm_ok([NYC, (37.0, -95.0), LA], distance_m=4_500_000))

    def request(self, method, url, **kwargs):
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        outcome = self.outcome_factory()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@override_settings(CACHES=LOCMEM_CACHE)
class SingleFlightTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def run_threads(self, n, target):
        results, errors = [], []

        def worker():
            try:
                results.append(target())
            except Exception as exc:  # collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        return results, errors

    def test_concurrent_identical_requests_make_one_provider_call(self):
        session = SlowSession()
        provider = OSRMProvider(settings.FUEL_PLANNER, session=session)
        results, errors = self.run_threads(8, lambda: get_route(NYC, LA, provider))
        self.assertEqual(errors, [])
        self.assertEqual(session.calls, 1)
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(r.provider_calls for r in results), 1)
        self.assertEqual(sum(r.from_cache for r in results), 7)
        self.assertEqual(routing_service._inflight, {})  # no lock objects leak

    def test_concurrent_different_routes_are_not_serialised_into_one(self):
        session = SlowSession(delay=0.1)
        provider = OSRMProvider(settings.FUEL_PLANNER, session=session)
        ends = [(30.0 + i, -90.0) for i in range(4)]
        results, errors = self.run_threads(1, lambda: [get_route(NYC, e, provider) for e in ends])
        self.assertEqual(errors, [])
        self.assertEqual(session.calls, 4)

    def test_failure_is_shared_but_not_cached(self):
        session = SlowSession(outcome_factory=lambda: requests_timeout)
        provider = OSRMProvider(settings.FUEL_PLANNER, session=session)
        results, errors = self.run_threads(4, lambda: get_route(NYC, LA, provider))
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 4)
        self.assertTrue(all(type(e).__name__ == "RoutingTimeout" for e in errors))
        self.assertIsNone(cache.get(route_cache_key("osrm", NYC, LA)))  # a later request retries


@override_settings(CACHES=LOCMEM_CACHE)
class CacheRecoveryTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_corrupted_entry_is_discarded_and_refetched(self):
        key = route_cache_key("osrm", NYC, LA)
        for corrupted in ({"coords": b"not zlib", "distance_miles": 1.0, "duration_seconds": 1.0},
                          {"unexpected": True}, {"coords": None, "distance_miles": "x"}):
            with self.subTest(corrupted=corrupted):
                cache.set(key, corrupted)
                session = SlowSession(delay=0)
                route = get_route(NYC, LA, OSRMProvider(settings.FUEL_PLANNER, session=session))
                self.assertEqual((session.calls, route.provider_calls, route.from_cache), (1, 1, False))
                again = get_route(NYC, LA, OSRMProvider(settings.FUEL_PLANNER, session=session))
                self.assertTrue(again.from_cache)
                cache.delete(key)

    def test_road_segments_survive_the_cache(self):
        session = SlowSession(delay=0, outcome_factory=lambda: osrm_ok(
            [NYC, LA], distance_m=4_500_000, steps=[(1_000_000, "I 80; US 6"), (3_500_000, "I 76")]))
        provider = OSRMProvider(settings.FUEL_PLANNER, session=session)
        first, second = get_route(NYC, LA, provider), get_route(NYC, LA, provider)
        self.assertEqual(second.road_segments, first.road_segments)
        self.assertEqual(first.road_segments[0].refs, frozenset({"I80", "US6"}))


class RateLimitTests(RouteApiTestCase):
    def setUp(self):
        super().setUp()
        add_station(1, 0.5, "3.0")

    def test_limit_and_spoofed_forwarded_for(self):
        cfg = {**settings.FUEL_PLANNER, "ROUTE_RATE_LIMIT": "2/min"}
        with override_settings(FUEL_PLANNER=cfg):
            body = {"start": "Chicago, IL"}  # invalid on purpose: throttling happens before validation
            self.assertEqual(self.post(body).status_code, 400)
            self.assertEqual(self.post(body).status_code, 400)
            r = self.post(body)
            self.assert_error(r, 429, "rate_limited")
            self.assertIn("Retry-After", r.headers)
            spoofed = self.client.post("/api/v1/route/", body, content_type="application/json",
                                       HTTP_X_FORWARDED_FOR="203.0.113.7")
            self.assertEqual(spoofed.status_code, 429)  # a fake X-Forwarded-For does not reset the limit

    def test_disabled_limit(self):
        cfg = {**settings.FUEL_PLANNER, "ROUTE_RATE_LIMIT": ""}
        with override_settings(FUEL_PLANNER=cfg):
            for _ in range(5):
                self.assertEqual(self.post({"start": "Chicago, IL"}).status_code, 400)


class EdgeCaseRequestTests(RouteApiTestCase):
    def test_zero_length_route(self):
        add_station(1, 0.0, "3.25", "HOME STOP")
        self.session.outcomes.append(osrm_ok([CHICAGO, CHICAGO], distance_m=0.0, duration_s=0.0))
        r = self.post({"start": "Chicago, IL", "finish": "Chicago, IL"})
        self.assertEqual(r.status_code, 201, r.content)
        b = r.json()
        self.assertEqual(b["fuel_stops"], [])
        self.assertEqual(b["fuel_cost"]["total_fuel_cost"], 0.0)
        self.assertEqual(b["fuel_usage"]["fuel_remaining_at_destination_gallons"], 50.0)

    def test_corridor_bounds(self):
        add_station(1, 480 / ROUTE_MILES, "3.0")
        for corridor, status in ((1, 201), (50, 201), (0.99, 400), (50.01, 400)):
            if status == 201:
                self.queue_route()
            with self.subTest(corridor=corridor):
                r = self.post({"start": "Chicago, IL", "finish": "Dallas, TX", "corridor_miles": corridor})
                self.assertEqual(r.status_code, status, r.content)
            cache.clear()

    @override_settings(FUEL_PLANNER={**settings.FUEL_PLANNER, "GEOCODER_FALLBACK_ENABLED": False})
    def test_hostile_strings_are_rejected_cleanly(self):
        for text in ("Robert'); DROP TABLE route_api_routeplan;--, TX", "<script>alert(1)</script>, TX",
                     "‮TX ,ogacihC", "\U0001F69A\U0001F69A, IL", "Chicago\x00, IL"):
            with self.subTest(text=text):
                r = self.post({"start": text, "finish": "Dallas, TX"})
                self.assertIn(r.status_code, (400, 422), r.content)
                self.assertIn(r.json()["error"]["code"], ("invalid_request", "location_not_found"))
        self.assertEqual(self.session.calls, [])

    def test_highway_confidence_end_to_end(self):
        add_station(1, 480 / ROUTE_MILES, "3.00000", "ON THE INTERSTATE")  # address "I-55, EXIT 1"
        self.session.outcomes.append(osrm_ok(straight_route(CHICAGO, DALLAS), distance_m=ROUTE_MILES * 1609.344,
                                             steps=[(ROUTE_MILES * 1609.344, "I 55")]))
        b = self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}).json()
        location = b["fuel_stops"][0]["location"]
        self.assertEqual((location["confidence"], location["matched_route_highways"]), ("high", ["I55"]))
        self.assertEqual(b["data_quality"]["location_confidence"], {"high": 1, "medium": 0, "low": 0})


class PlanInvariantGuardTests(SimpleTestCase):
    def test_guard_rejects_a_corrupted_plan(self):
        plan = optimize_fuel_plan([FuelCandidate("A", 400, 0, 3.0)], 800)
        _check_plan_invariants(plan)  # a genuine plan passes
        plan.stops[0].purchase_miles = 50.0  # too little fuel to finish
        with self.assertRaises(PlannerError):
            _check_plan_invariants(plan)
        with mock.patch.object(plan.stops[0], "purchase_miles", 600.0), self.assertRaises(PlannerError):
            _check_plan_invariants(plan)  # over capacity


class MinLocationConfidenceTests(RouteApiTestCase):
    def setUp(self):
        super().setUp()
        # Cheap station whose address highway is NOT on the route, 6 mi off -> low confidence;
        # pricier station at an I-55 exit -> high confidence.
        from route_api.models import FuelStation

        add_station(1, 480 / ROUTE_MILES, "3.40000", "ON I-55")
        cheap = add_station(2, 470 / ROUTE_MILES, "2.90000", "OFF-ROUTE CHEAP")
        FuelStation.objects.filter(pk=cheap.pk).update(address="US-999", longitude=cheap.longitude + 0.11)
        add_station(3, 0.0, "3.60000", "ORIGIN")

    def post_with(self, level):
        self.session.outcomes.append(osrm_ok(straight_route(CHICAGO, DALLAS), distance_m=ROUTE_MILES * 1609.344,
                                             steps=[(ROUTE_MILES * 1609.344, "I 55")]))
        cache.clear()
        from route_api.services.station_service import reset_station_index

        reset_station_index()
        return self.post({"start": "Chicago, IL", "finish": "Dallas, TX", "min_location_confidence": level})

    def test_filter_changes_stops_but_never_lowers_cost_or_changes_start_value(self):
        low = self.post_with("low").json()
        high = self.post_with("high").json()
        # Unfiltered: the cheap low-confidence station is used (it is the cheapest).
        self.assertIn("OFF-ROUTE CHEAP", [s["name"] for s in low["fuel_stops"]])
        self.assertIn("low", [s["location"]["confidence"] for s in low["fuel_stops"]])
        # min_location_confidence=high: only stations at an exit of a route highway are used.
        self.assertEqual([s["name"] for s in high["fuel_stops"]], ["ON I-55"])
        self.assertEqual({s["location"]["confidence"] for s in high["fuel_stops"]}, {"high"})
        self.assertEqual(low["fuel_cost"]["starting_fuel_estimate"], high["fuel_cost"]["starting_fuel_estimate"])
        self.assertGreaterEqual(high["fuel_cost"]["total_fuel_cost"], low["fuel_cost"]["total_fuel_cost"])
        self.assertEqual(high["assumptions"]["min_location_confidence"], "high")
        self.assertTrue(any("low location confidence" in w for w in low["warnings"]))

    def test_invalid_level(self):
        self.assert_error(self.post({"start": "Chicago, IL", "finish": "Dallas, TX",
                                     "min_location_confidence": "certain"}), 400, "invalid_request")


class TricklingResponse:
    """A provider response that keeps sending small chunks forever (each within the read timeout)."""

    status_code = 200

    def __init__(self, chunk_delay=0.05, chunk=b" ", total_chunks=None):
        self.chunk_delay, self.chunk, self.total_chunks = chunk_delay, chunk, total_chunks
        self.closed = False

    def iter_content(self, chunk_size=1):
        sent = 0
        while self.total_chunks is None or sent < self.total_chunks:
            time.sleep(self.chunk_delay)
            sent += 1
            yield self.chunk

    def close(self):
        self.closed = True


class OneShotSession:
    def __init__(self, response):
        self.response, self.calls = response, 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        assert kwargs.get("stream") is True  # body must be read under our own deadline
        return self.response


@override_settings(CACHES=LOCMEM_CACHE)
class ProviderDeadlineTests(SimpleTestCase):
    """Regression: a real OSRM call once took 30 minutes because the body trickled in."""

    def setUp(self):
        cache.clear()

    def test_trickling_response_hits_the_total_deadline(self):
        from route_api.services.errors import RoutingTimeout

        response = TricklingResponse()
        cfg = {**settings.FUEL_PLANNER, "ROUTING_TOTAL_TIMEOUT": 0.5}
        provider = OSRMProvider(cfg, session=OneShotSession(response))
        t0 = time.perf_counter()
        with self.assertRaises(RoutingTimeout) as ctx:
            provider.route(NYC, LA)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertIn("did not finish responding", ctx.exception.message)
        self.assertTrue(response.closed)

    def test_oversized_response_rejected(self):
        from route_api.services.errors import RoutingProviderError

        response = TricklingResponse(chunk_delay=0, chunk=b"x" * 65536, total_chunks=400)  # ~26 MB
        provider = OSRMProvider(settings.FUEL_PLANNER, session=OneShotSession(response))
        with self.assertRaises(RoutingProviderError):
            provider.route(NYC, LA)
        self.assertTrue(response.closed)

    def test_waiters_give_up_when_the_in_flight_call_hangs(self):
        from route_api.services.errors import RoutingTimeout

        cfg = {**settings.FUEL_PLANNER, "ROUTING_TOTAL_TIMEOUT": 0.2}
        slow = OSRMProvider(cfg, session=SlowSession(delay=6.0))   # holds the key lock ~6 s
        waiter = OSRMProvider(cfg, session=SlowSession(delay=0))
        first = threading.Thread(target=lambda: self._swallow(lambda: get_route(NYC, LA, slow)))
        first.start()
        time.sleep(0.2)
        t0 = time.perf_counter()
        with self.assertRaises(RoutingTimeout):
            get_route(NYC, LA, waiter)        # waits at most total_timeout + 5 s, not indefinitely
        self.assertLess(time.perf_counter() - t0, 6.0)
        first.join()

    @staticmethod
    def _swallow(fn):
        try:
            fn()
        except Exception:
            pass


class PlanCacheTests(RouteApiTestCase):
    def setUp(self):
        super().setUp()
        add_station(1, 480 / ROUTE_MILES, "3.00000", "MIDDLE")
        add_station(2, 0.0, "3.60000", "ORIGIN")

    def test_identical_request_skips_routing_matching_and_optimisation(self):
        self.queue_route()
        first = self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}).json()
        with mock.patch("route_api.services.planner.match_stations") as match, \
                mock.patch("route_api.services.planner.optimize_fuel_plan") as optimise:
            second = self.post({"start": "chicago, il", "finish": "Dallas, TX"}).json()
        match.assert_not_called()
        optimise.assert_not_called()
        self.assertEqual(len(self.session.calls), 1)
        self.assertTrue(second["meta"]["plan_cache_hit"])
        self.assertEqual(second["meta"]["map_api_calls"]["routing"], 0)
        self.assertEqual(second["fuel_cost"], first["fuel_cost"])
        self.assertEqual(second["fuel_stops"], first["fuel_stops"])
        self.assertEqual(second["start"]["query"], "chicago, il")       # echoes THIS request's input
        self.assertNotEqual(second["plan_id"], first["plan_id"])          # still its own saved plan + map
        self.assertEqual(self.client.get(f"/api/v1/route/{second['plan_id']}/map/").status_code, 200)

    def test_changed_station_data_invalidates_cached_plans(self):
        from route_api.models import FuelStation
        from route_api.services.station_service import reset_station_index

        self.queue_route()
        first = self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}).json()
        FuelStation.objects.filter(opis_id=1).update(price="2.50000")   # e.g. `load_stations` with new prices
        reset_station_index()
        second = self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}).json()
        self.assertFalse(second["meta"]["plan_cache_hit"])
        self.assertNotEqual(second["meta"]["station_data_fingerprint"], first["meta"]["station_data_fingerprint"])
        self.assertEqual(second["fuel_stops"][0]["price_per_gallon"], 2.5)
        self.assertEqual(len(self.session.calls), 1)                       # the ROUTE is still cached

    def test_different_options_are_cached_separately(self):
        self.queue_route()
        a = self.post({"start": "Chicago, IL", "finish": "Dallas, TX"}).json()
        b = self.post({"start": "Chicago, IL", "finish": "Dallas, TX", "corridor_miles": 20}).json()
        self.assertFalse(b["meta"]["plan_cache_hit"])
        self.assertEqual(b["assumptions"]["station_corridor_miles"], 20.0)
        self.assertEqual(a["assumptions"]["station_corridor_miles"], 15.0)
