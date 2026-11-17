"""Regressions for independently reproduced review findings; no public HTTP calls."""
from __future__ import annotations

import dataclasses
import math
import threading
import time
from io import StringIO
from pathlib import Path
from unittest import mock

import numpy as np
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.db import transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings

from route_api.models import FuelStation, StationDatasetRevision
from route_api.services import nominatim, routing_service
from route_api.services.cost_calculator import choose_start_price, summarize
from route_api.services.errors import GeocoderUnavailable, LocationOutsideUSA, RouteNotFound, RoutingTimeout, RoutingUnavailable
from route_api.services.geocoding_service import resolve_location
from route_api.services.http_utils import active_http_workers
from route_api.services.route_matching import (MILES_PER_DEG_LAT, build_route_profile, match_stations,
                                              _project_segment_pairs)
from route_api.services.routing_service import OSRMProvider, get_route
from route_api.services.station_service import StationIndex, get_station_index, reset_station_index
from route_api.services.us_boundaries import BoundaryIndex, get_boundary_index

from .helpers import LOCMEM_CACHE, index_of, station
from .helpers import osrm_ok
from .local_http import local_routing_server
from .test_api import CHICAGO, DALLAS, ROUTE_MILES, RouteApiTestCase, add_station, straight_route
from .test_cost_calculator import corridor, plan


@override_settings(CACHES=LOCMEM_CACHE)
class RealHTTPDeadlineTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def provider(self, url, *, total=2, read=.2):
        return OSRMProvider({**settings.FUEL_PLANNER, "OSRM_BASE_URL": url,
                             "ROUTING_TOTAL_TIMEOUT": total, "ROUTING_READ_TIMEOUT": read,
                             "ROUTING_MIN_INTERVAL_SECONDS": 0})

    def test_real_trickling_body_is_bounded_and_worker_is_reaped(self):
        with local_routing_server() as (url, calls):
            start = time.perf_counter()
            with self.assertRaises(RoutingTimeout):
                self.provider(url + "/slow").route(CHICAGO, DALLAS)
            self.assertLess(time.perf_counter() - start, 2.4)
            self.assertEqual(len(calls), 1)
            self.assertEqual(active_http_workers(), ())

    def test_real_trickling_headers_are_in_total_deadline(self):
        with local_routing_server() as (url, calls):
            start = time.perf_counter()
            with self.assertRaises(RoutingTimeout):
                self.provider(url + "/headers").route(CHICAGO, DALLAS)
            self.assertLess(time.perf_counter() - start, 2.4)
            self.assertEqual(len(calls), 1)
            self.assertEqual(active_http_workers(), ())

    def test_actual_body_read_timeout_maps_to_504(self):
        with local_routing_server() as (url, calls):
            with self.assertRaises(RoutingTimeout) as error:
                self.provider(url + "/stall", total=4, read=.1).route(CHICAGO, DALLAS)
            self.assertEqual(error.exception.status_code, 504)
            self.assertEqual(len(calls), 1)
            self.assertEqual(active_http_workers(), ())

    def test_genuine_connection_refusal_stays_503(self):
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with self.assertRaises(RoutingUnavailable) as error:
            self.provider(f"http://127.0.0.1:{port}", total=3).route(CHICAGO, DALLAS)
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(active_http_workers(), ())

    def test_concurrent_waiters_share_one_failed_real_request_and_deadline(self):
        with local_routing_server() as (url, calls):
            barrier, errors = threading.Barrier(5), []
            # Include child startup in the budget, but allow enough time for
            # the real server to see the stream on a busy Windows machine.
            provider = self.provider(url + "/slow", total=2)
            def run():
                barrier.wait()
                try:
                    get_route(CHICAGO, DALLAS, provider)
                except Exception as error:
                    errors.append(error)
            threads = [threading.Thread(target=run) for _ in range(4)]
            for thread in threads:
                thread.start()
            start = time.perf_counter()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=2.5)
            self.assertFalse(any(t.is_alive() for t in threads))
            self.assertLess(time.perf_counter() - start, 2.4)
            self.assertEqual(len(errors), 4)
            self.assertTrue(all(isinstance(e, RoutingTimeout) for e in errors))
            self.assertEqual(len(calls), 1)
            self.assertEqual(active_http_workers(), ())
            self.assertEqual(routing_service._inflight, {})

    def test_real_success_is_one_call_then_zero_and_no_workers_remain(self):
        with local_routing_server() as (url, calls):
            provider = self.provider(url, total=3)
            first = get_route(CHICAGO, DALLAS, provider)
            second = get_route(CHICAGO, DALLAS, provider)
            self.assertEqual((first.provider_calls, second.provider_calls), (1, 0))
            self.assertEqual(len(calls), 1)
            self.assertEqual(active_http_workers(), ())

    def test_real_provider_http_error_body_is_preserved(self):
        with local_routing_server() as (url, calls):
            with self.assertRaises(RouteNotFound):
                self.provider(url + "/missing", total=3).route(CHICAGO, DALLAS)
            self.assertEqual(len(calls), 1)
            self.assertEqual(active_http_workers(), ())

    def test_requests_wrapped_body_timeout_is_also_504(self):
        import requests
        from urllib3.exceptions import ReadTimeoutError
        from .helpers import FakeSession
        error = requests.ConnectionError(ReadTimeoutError(None, None, "body timed out"))
        provider = OSRMProvider({**settings.FUEL_PLANNER, "ROUTING_MIN_INTERVAL_SECONDS": 0},
                                session=FakeSession(error))
        with self.assertRaises(RoutingTimeout):
            provider.route(CHICAGO, DALLAS)

    def test_spacing_lock_wait_uses_the_same_total_budget(self):
        provider = self.provider("http://127.0.0.1:1", total=.15)
        provider.cfg = {**provider.cfg, "ROUTING_MIN_INTERVAL_SECONDS": 1}
        routing_service.RoutingProvider._spacing_lock.acquire()
        try:
            start = time.perf_counter()
            with self.assertRaises(RoutingTimeout):
                provider.route(CHICAGO, DALLAS)
            self.assertLess(time.perf_counter() - start, .4)
        finally:
            routing_service.RoutingProvider._spacing_lock.release()


class OriginalGeometryTests(SimpleTestCase):
    def test_right_angle_original_distance_and_corner_mile_marker(self):
        lat = 35
        dy = 1 / MILES_PER_DEG_LAT
        dx = dy / math.cos(math.radians(lat))
        coords = np.array([[lat, -100], [lat + dy, -100], [lat + dy, -100 + dx]])
        profile = build_route_profile(coords, 2, 2)
        matches = match_stations(profile, index_of(station(1, lat + 1.6 * dy, -100 - .6 * dx)), 1)
        self.assertEqual(len(matches), 1)
        self.assertAlmostEqual(matches[0].off_route_miles, .8485, delta=.002)
        self.assertAlmostEqual(matches[0].mile_marker, 1, delta=.002)
        self.assertAlmostEqual(matches[0].route_lat, lat + dy, places=6)
        self.assertAlmostEqual(matches[0].route_lon, -100, places=6)

    def test_provider_distance_scales_corner_marker_without_moving_projection(self):
        coords = np.array([[35, -100], [35.01, -100], [35.01, -99.99]])
        record = station(1, 35.012, -100.002)
        one = match_stations(build_route_profile(coords, 2), index_of(record), 1)[0]
        two = match_stations(build_route_profile(coords, 4), index_of(record), 1)[0]
        self.assertAlmostEqual(two.mile_marker, 2 * one.mile_marker, places=8)
        self.assertAlmostEqual(two.off_route_miles, one.off_route_miles, places=8)

    def test_safe_grid_matches_all_segments_on_random_bends_and_parallel_roads(self):
        rng = np.random.default_rng(20261117)
        for latitude in (30, 65):
            coords = np.column_stack((latitude + np.cumsum(rng.uniform(-.03, .03, 80)),
                                      -100 + np.cumsum(rng.uniform(-.05, .05, 80))))
            profile = build_route_profile(coords, 150, 2)
            records = [station(i, float(la), float(lo)) for i, (la, lo) in enumerate(
                zip(rng.uniform(profile.lats.min() - .03, profile.lats.max() + .03, 120),
                    rng.uniform(profile.lons.min() - .03, profile.lons.max() + .03, 120)))]
            got = {m.station.opis_id: m for m in match_stations(profile, StationIndex(records), 1)}
            for record in records:
                segments = np.arange(len(profile.lats) - 1)
                off, mile, _, _ = _project_segment_pairs(np.full(len(segments), record.latitude),
                                                        np.full(len(segments), record.longitude), segments, profile)
                k = np.lexsort((mile, off))[0]
                self.assertEqual(record.opis_id in got, off[k] <= 1)
                if record.opis_id in got:
                    self.assertAlmostEqual(got[record.opis_id].off_route_miles, off[k], places=8)
                    self.assertAlmostEqual(got[record.opis_id].mile_marker, mile[k], places=8)


class USBoundaryTests(SimpleTestCase):
    def test_foreign_border_cities_rejected_remote_states_accepted(self):
        for name, lat, lon in (("Windsor", 42.3149, -83.0364), ("Tijuana", 32.5149, -117.0382),
                               ("Vancouver", 49.2827, -123.1207)):
            with self.subTest(name=name), self.assertRaises(LocationOutsideUSA):
                resolve_location({"lat": lat, "lon": lon})
        for name, lat, lon in (("Yellowstone", 44.6, -110.5), ("Alaska interior", 66, -150),
                               ("Anchorage", 61.2181, -149.9003), ("Hawaii island", 19.6, -155.5),
                               ("Honolulu", 21.3099, -157.8581), ("Attu Island", 52.85, 173.2),
                               ("Adak", 51.88, -176.65), ("Detroit", 42.3314, -83.0458),
                               ("San Diego", 32.7157, -117.1611)):
            with self.subTest(name=name):
                self.assertEqual(resolve_location({"lat": lat, "lon": lon}).source, "coordinates")

    def test_holes_edges_islands_and_antimeridian(self):
        outer = [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
        hole = [[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]]
        island = [[8, 0], [9, 0], [9, 1], [8, 1], [8, 0]]
        crossing = [[179, 10], [-179, 10], [-179, 12], [179, 12], [179, 10]]
        index = BoundaryIndex({"TEST": [outer, hole, island], "DATE": [crossing]})
        self.assertEqual(index.state_at(.5, .5), "TEST")
        self.assertIsNone(index.state_at(2, 2))
        self.assertEqual(index.state_at(0, 2), "TEST")
        self.assertEqual(index.state_at(.5, 8.5), "TEST")
        self.assertEqual(index.state_at(11, 179.5), "DATE")
        self.assertEqual(index.state_at(11, -179.5), "DATE")
        self.assertEqual(index.state_at(11, 180), "DATE")
        self.assertEqual(index.state_at(11, -180), "DATE")
        self.assertIsNone(index.state_at(11, 0))

    def test_real_boundary_vertices_are_included(self):
        index = get_boundary_index()
        for state in ("MI", "CA", "AK", "HI"):
            x, y, _ = index.states[state][0]
            self.assertIsNotNone(index.state_at(float(y[0]), float(x[0])))
            self.assertIsNotNone(index.state_at(float((y[0] + y[1]) / 2), float((x[0] + x[1]) / 2)))


class CacheIntegrityTests(RouteApiTestCase):
    def setUp(self):
        super().setUp()
        add_station(1, .5, "3.00000", "MIDDLE")
        add_station(2, 0, "3.60000", "ORIGIN")
        self.body = {"start": "Chicago, IL", "finish": "Dallas, TX"}

    def test_mpg_change_recomputes_and_then_hits_valid_cache(self):
        self.queue_route()
        first = self.post(self.body).json()
        with override_settings(FUEL_PLANNER={**settings.FUEL_PLANNER, "VEHICLE_MPG": 20}):
            second = self.post(self.body).json()
            third = self.post(self.body).json()
        self.assertFalse(second["meta"]["plan_cache_hit"])
        self.assertEqual(second["assumptions"]["vehicle_mpg"], 20)
        self.assertAlmostEqual(second["fuel_cost"]["total_fuel_cost"], first["fuel_cost"]["total_fuel_cost"] / 2, delta=.02)
        self.assertTrue(third["meta"]["plan_cache_hit"])
        self.assertEqual(len(self.session.calls), 1)

    def test_range_and_geometry_configuration_invalidate_plans(self):
        self.queue_route()
        first = self.post(self.body).json()
        for key, value in (("VEHICLE_RANGE_MILES", 600), ("RESPONSE_GEOMETRY_TOLERANCE_DEG", .001),
                           ("ROUTE_SAMPLE_SPACING_MILES", 1), ("MAX_OFF_ROUTE_MILES", 20)):
            with override_settings(FUEL_PLANNER={**settings.FUEL_PLANNER, key: value}):
                response = self.post(self.body).json()
            self.assertFalse(response["meta"]["plan_cache_hit"], key)
        self.assertEqual(len(self.session.calls), 1)

    def test_name_address_changes_refresh_index_and_cached_output_without_manual_reset(self):
        self.queue_route()
        first = self.post(self.body).json()
        FuelStation.objects.filter(opis_id=1).update(name="UPDATED MIDDLE", address="US-999")
        second = self.post(self.body).json()
        self.assertFalse(second["meta"]["plan_cache_hit"])
        self.assertNotEqual(first["meta"]["station_data_fingerprint"], second["meta"]["station_data_fingerprint"])
        self.assertEqual(second["fuel_stops"][0]["name"], "UPDATED MIDDLE")
        self.assertEqual(second["fuel_stops"][0]["address"], "US-999")

    def test_address_change_recomputes_high_confidence_eligibility(self):
        self.session.outcomes.append(osrm_ok(straight_route(CHICAGO, DALLAS),
            distance_m=ROUTE_MILES * 1609.344, steps=[(ROUTE_MILES * 1609.344, "I 55")]))
        body = {**self.body, "min_location_confidence": "high"}
        first = self.post(body).json()
        self.assertEqual(first["fuel_stops"][0]["location"]["confidence"], "high")
        FuelStation.objects.filter(opis_id=1).update(address="US-999")
        self.assert_error(self.post(body), 422, "no_feasible_fuel_plan")
        # The route itself is still cached: no second upstream call is needed.
        self.assertEqual(len(self.session.calls), 1)

    def test_reload_refreshes_index_without_manual_reset(self):
        original = get_station_index()
        call_command("load_stations", stdout=StringIO())
        refreshed = get_station_index()
        self.assertIsNot(original, refreshed)
        self.assertEqual(len(refreshed), 6620)
        self.assertNotEqual(original.revision, refreshed.revision)

    def test_rolled_back_station_changes_do_not_publish_revision(self):
        original = get_station_index()
        revision = StationDatasetRevision.objects.get(pk=1).generation
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                FuelStation.objects.filter(opis_id=1).update(price="1.00000")
                raise RuntimeError("rollback")
        self.assertEqual(StationDatasetRevision.objects.get(pk=1).generation, revision)
        self.assertEqual(get_station_index().fingerprint, original.fingerprint)


class TransactionSnapshotTests(TransactionTestCase):
    def tearDown(self):
        reset_station_index()

    def test_rollback_revision_reuse_cannot_publish_uncommitted_index(self):
        add_station(1, .5, "3.00000", "ORIGINAL")
        original = get_station_index()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                FuelStation.objects.filter(opis_id=1).update(name="UNCOMMITTED")
                private = get_station_index()
                self.assertEqual(private.records[0].name, "UNCOMMITTED")
                from route_api.services import station_service
                self.assertIs(station_service._index, original)
                raise RuntimeError("rollback")
        FuelStation.objects.filter(opis_id=1).update(name="COMMITTED")
        refreshed = get_station_index()
        self.assertEqual(refreshed.revision, private.revision)
        self.assertEqual(refreshed.records[0].name, "COMMITTED")

    def test_reload_between_revision_reads_retries_a_coherent_snapshot(self):
        add_station(1, .5, "3.00000", "BEFORE")
        from route_api.services import station_service
        real_revision = station_service._database_revision
        reads = 0
        def revision():
            nonlocal reads
            reads += 1
            if reads == 2:
                FuelStation.objects.filter(opis_id=1).update(name="AFTER")
            return real_revision()
        with mock.patch.object(station_service, "_database_revision", side_effect=revision):
            index = StationIndex.from_database()
        self.assertEqual(reads, 4)
        self.assertEqual(index.records[0].name, "AFTER")
        self.assertEqual(index.revision, real_revision())


class FingerprintTests(SimpleTestCase):
    def test_all_output_and_eligibility_fields_change_the_fingerprint(self):
        record = station(1, 35, -100)
        before = StationIndex([record]).fingerprint
        replacements = {"name": "changed", "address": "US-999", "city": "Other", "state": "NM",
                        "geocode_source": "other", "price": 2.5, "latitude": 35.1,
                        "longitude": -99.9, "highways": frozenset({"US999"}), "opis_id": 2}
        for field, value in replacements.items():
            with self.subTest(field=field):
                self.assertNotEqual(before, StationIndex([dataclasses.replace(record, **{field: value})]).fingerprint)


class ExplanationTests(SimpleTestCase):
    def test_cheaper_station_265_miles_away_is_not_called_unreachable(self):
        stations = [corridor(1, 400, 3, name="A"), corridor(2, 700, 3.1, name="B"),
                    corridor(3, 650, 2.99, off=15, name="CHEAPER C")]
        result = plan(stations, 1000)
        self.assertEqual([s.candidate.key.station.name for s in result.stops], ["A", "B"])
        self.assertAlmostEqual(result.purchase_cost, 151)
        reason = summarize(result, choose_start_price(stations, 3.4))["stops"][0]["reason"]
        self.assertNotIn("no cheaper station is reachable", reason)
        self.assertIn("detours", reason)


class InputAndGeocoderTests(RouteApiTestCase):
    def test_extreme_coordinate_inputs_return_controlled_400(self):
        for lat in (10**400, -(10**400), "1e99999", "NaN", "Infinity", True, [], {}):
            with self.subTest(lat=str(lat)[:30]):
                self.assert_error(self.post({"start": {"lat": lat, "lon": -74}, "finish": "Dallas, TX"}),
                                  400, "invalid_request")
        for start in ("1e9999,-74", "NaN,-74", "95,-74", "40,-200"):
            self.assert_error(self.post({"start": start, "finish": "Dallas, TX"}), 400, "invalid_request")
        self.assertEqual(self.session.calls, [])

    def test_malformed_nominatim_payload_is_controlled_503(self):
        import requests
        response = requests.Response()
        response.status_code = 200
        response._content = b"{broken JSON"
        with mock.patch.object(nominatim, "request_with_deadline", return_value=response):
            self.assert_error(self.post({"start": "Empire State Building", "finish": "Dallas, TX"}),
                              503, "geocoder_unavailable")
        for payload in ({"error": "bad"}, [None], [{}], [{"lat": "nan", "lon": "-74"}],
                        [{"lat": "40", "lon": "-74", "address": []}],
                        [{"lat": 10**400, "lon": -74}]):
            cache.clear()
            with self.subTest(payload=repr(payload)[:60]), mock.patch.object(nominatim.NominatimClient, "_get", return_value=payload):
                self.assert_error(self.post({"start": "Empire State Building", "finish": "Dallas, TX"}),
                                  503, "geocoder_unavailable")

    def test_cached_foreign_geocode_cannot_bypass_new_boundary_validation(self):
        import hashlib
        key = "geocode:v1:" + hashlib.sha256(b"cached place").hexdigest()[:32]
        cache.set(key, {"lat": 42.3149, "lon": -83.0364, "label": "Windsor", "state": "MI"})
        self.assert_error(self.post({"start": "cached place", "finish": "Dallas, TX"}),
                          422, "location_outside_usa")

    def test_missing_or_corrupt_boundary_data_returns_controlled_503(self):
        for error in (OSError("missing"), EOFError("truncated gzip"), ValueError("invalid JSON")):
            with self.subTest(error=type(error).__name__), mock.patch(
                    "route_api.services.geocoding_service.get_boundary_index", side_effect=error):
                self.assert_error(self.post({"start": {"lat": 44.6, "lon": -110.5}, "finish": "Dallas, TX"}),
                                  503, "location_validation_unavailable")


class LoomNarrationTests(SimpleTestCase):
    def test_script_explains_geocoding_fallback_and_allows_exactly_zero_fuel(self):
        script = (Path(settings.BASE_DIR) / "Loom_Script_Spotter.md").read_text(encoding="utf-8")
        self.assertNotIn("No geocoding call is made per request.", script)
        self.assertNotIn("never lets the tank go empty", script)
        self.assertIn("free-text inputs can use a cached geocoding fallback", script)
        self.assertIn("may reach exactly zero", script)
