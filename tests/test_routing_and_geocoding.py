"""Routing provider handling, route cache, and start/finish resolution. (External calls mocked.)"""

from unittest import mock

import numpy as np
from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from route_api.models import FuelStation
from route_api.services import geocoding_service, nominatim
from route_api.services.errors import (
    GeocoderUnavailable, LocationNotFound, LocationOutsideUSA, RouteNotFound, RoutingProviderError,
    RoutingTimeout, RoutingUnavailable, StationDataUnavailable,
)
from route_api.services.routing_service import OpenRouteServiceProvider, OSRMProvider, get_provider, get_route
from route_api.services.station_service import StationIndex, get_station_index, reset_station_index

from .helpers import (
    LOCMEM_CACHE, FakeResponse, FakeSession, osrm_ok, requests_conn_error, requests_timeout,
)

CFG = settings.FUEL_PLANNER
NYC, LA = (40.7128, -74.0060), (34.0522, -118.2437)


def osrm(*outcomes):
    session = FakeSession(*outcomes)
    return OSRMProvider(CFG, session=session), session


@override_settings(CACHES=LOCMEM_CACHE)
class OSRMProviderTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_success_parses_distance_duration_and_swaps_lon_lat(self):
        provider, session = osrm(osrm_ok([NYC, (37.0, -95.0), LA], distance_m=4_523_000, duration_s=180_000))
        route = provider.route(NYC, LA)
        self.assertAlmostEqual(route.distance_miles, 4_523_000 / 1609.344)
        self.assertEqual(route.duration_seconds, 180_000)
        self.assertEqual(route.coordinates.tolist()[0], list(NYC))
        call = session.calls[0]
        self.assertIn("/route/v1/driving/-74.006000,40.712800;-118.243700,34.052200", call["url"])
        self.assertEqual(call["params"]["geometries"], "polyline6")
        self.assertEqual(call["params"]["steps"], "true")
        self.assertEqual(call["params"]["overview"], "full")
        self.assertIn("User-Agent", call["headers"])
        self.assertEqual(call["timeout"], (CFG["ROUTING_CONNECT_TIMEOUT"], CFG["ROUTING_READ_TIMEOUT"]))

    def test_no_route(self):
        provider, _ = osrm(FakeResponse(400, {"code": "NoRoute", "message": "Impossible route"}))
        with self.assertRaises(RouteNotFound) as ctx:
            provider.route(NYC, (21.3, -157.8))
        self.assertEqual(ctx.exception.status_code, 422)

    def test_no_segment(self):
        provider, _ = osrm(FakeResponse(400, {"code": "NoSegment", "message": "Could not find a matching segment"}))
        with self.assertRaises(RouteNotFound):
            provider.route(NYC, LA)

    def test_timeout(self):
        provider, _ = osrm(requests_timeout)
        with self.assertRaises(RoutingTimeout) as ctx:
            provider.route(NYC, LA)
        self.assertEqual(ctx.exception.status_code, 504)

    def test_connection_error(self):
        provider, _ = osrm(requests_conn_error)
        with self.assertRaises(RoutingUnavailable) as ctx:
            provider.route(NYC, LA)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_rate_limited(self):
        provider, _ = osrm(FakeResponse(429, None))
        with self.assertRaises(RoutingUnavailable):
            provider.route(NYC, LA)

    def test_server_error_with_html_body(self):
        provider, _ = osrm(FakeResponse(502, None))
        with self.assertRaises(RoutingProviderError) as ctx:
            provider.route(NYC, LA)
        self.assertEqual(ctx.exception.status_code, 502)

    def test_unexpected_payloads(self):
        for payload in ({"code": "Ok", "routes": []}, {"code": "Ok", "routes": [{"distance": 1}]},
                        {"code": "InvalidQuery"}, ["not", "a", "dict"]):
            provider, _ = osrm(FakeResponse(200, payload))
            with self.subTest(payload=payload), self.assertRaises(RoutingProviderError):
                provider.route(NYC, LA)

    def test_empty_or_invalid_geometry(self):
        from route_api.services.geo import encode_polyline

        one_point = encode_polyline([(40.7, -74.0)], precision=6)
        for geometry in ("", one_point, one_point[:-1], "not a polyline é", None, {"coordinates": []}):
            payload = {"code": "Ok", "routes": [{"distance": 1, "duration": 1, "geometry": geometry, "legs": []}]}
            provider, _ = osrm(FakeResponse(200, payload))
            with self.subTest(geometry=geometry), self.assertRaises(RoutingProviderError):
                provider.route(NYC, LA)


@override_settings(CACHES=LOCMEM_CACHE)
class RouteCacheTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_second_request_uses_cache_and_makes_no_call(self):
        coords = [NYC, (39.123456, -80.654321), LA]
        provider, session = osrm(osrm_ok(coords, distance_m=4_500_000))
        first = get_route(NYC, LA, provider)
        second = get_route(NYC, LA, provider)
        self.assertEqual((first.provider_calls, first.from_cache), (1, False))
        self.assertEqual((second.provider_calls, second.from_cache), (0, True))
        self.assertEqual(len(session.calls), 1)
        np.testing.assert_array_equal(second.coordinates, first.coordinates)  # lossless
        self.assertAlmostEqual(second.distance_miles, first.distance_miles)

    def test_failures_are_not_cached(self):
        provider, session = osrm(requests_timeout, osrm_ok([NYC, LA]))
        with self.assertRaises(RoutingTimeout):
            get_route(NYC, LA, provider)
        self.assertEqual(get_route(NYC, LA, provider).provider_calls, 1)
        self.assertEqual(len(session.calls), 2)

    def test_different_endpoints_different_cache_entries(self):
        provider, session = osrm(osrm_ok([NYC, LA]), osrm_ok([NYC, (41.0, -87.0)]))
        get_route(NYC, LA, provider)
        get_route(NYC, (41.0, -87.0), provider)
        self.assertEqual(len(session.calls), 2)


class OpenRouteServiceProviderTests(SimpleTestCase):
    """Mocked only: no ORS key was available, so this provider is not live-tested."""

    cfg = {**CFG, "ROUTING_PROVIDER": "openrouteservice", "ORS_API_KEY": "test-key"}

    def test_success(self):
        payload = {"features": [{"geometry": {"coordinates": [[NYC[1], NYC[0]], [LA[1], LA[0]]]},
                                 "properties": {"summary": {"distance": 4_500_000, "duration": 170_000}}}]}
        session = FakeSession(FakeResponse(200, payload))
        route = OpenRouteServiceProvider(self.cfg, session=session).route(NYC, LA)
        self.assertAlmostEqual(route.distance_miles, 4_500_000 / 1609.344)
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "test-key")
        self.assertEqual(session.calls[0]["json"]["coordinates"][0], [NYC[1], NYC[0]])

    def test_route_not_found_code(self):
        session = FakeSession(FakeResponse(404, {"error": {"code": 2010, "message": "Could not find routable point"}}))
        with self.assertRaises(RouteNotFound):
            OpenRouteServiceProvider(self.cfg, session=session).route(NYC, LA)

    def test_missing_key(self):
        with self.assertRaises(RoutingProviderError):
            OpenRouteServiceProvider({**self.cfg, "ORS_API_KEY": ""}, session=FakeSession()).route(NYC, LA)

    def test_unknown_provider_name(self):
        with self.assertRaises(RoutingProviderError):
            get_provider({**CFG, "ROUTING_PROVIDER": "nope"})


@override_settings(CACHES=LOCMEM_CACHE)
class LocationResolutionTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_city_state_resolved_offline(self):
        with mock.patch.object(nominatim.NominatimClient, "_get") as fallback:
            loc = geocoding_service.resolve_location("Los Angeles, CA", "finish")
        fallback.assert_not_called()
        self.assertEqual(loc.source, "census_gazetteer")
        self.assertEqual(loc.external_calls, 0)
        self.assertAlmostEqual(loc.lat, 34.0, delta=0.2)
        self.assertAlmostEqual(loc.lon, -118.3, delta=0.2)

    def test_coordinates_string_and_object(self):
        loc = geocoding_service.resolve_location("39.7392, -104.9903")
        self.assertEqual((loc.source, loc.lat, loc.lon), ("coordinates", 39.7392, -104.9903))
        loc = geocoding_service.resolve_location({"lat": 32.7767, "lng": -96.797})
        self.assertEqual(loc.point, (32.7767, -96.797))

    def test_invalid_coordinates(self):
        for value in ("95,10", "40.0,-200", {"lat": "abc", "lon": 1}):
            with self.subTest(value=value), self.assertRaises(LocationNotFound):
                geocoding_service.resolve_location(value)

    def test_coordinates_outside_usa(self):
        for value in ("51.5074,-0.1278", "19.4326,-99.1332", "-74.0060,40.7128"):  # London, Mexico City, swapped NYC
            with self.subTest(value=value), self.assertRaises(LocationOutsideUSA):
                geocoding_service.resolve_location(value)

    def test_foreign_text_rejected_without_network(self):
        with mock.patch.object(nominatim.NominatimClient, "_get") as fallback:
            for value in ("Toronto, ON", "Calgary, AB", "Monterrey, Mexico", "Vancouver, Canada"):
                with self.subTest(value=value), self.assertRaises(LocationOutsideUSA):
                    geocoding_service.resolve_location(value)
        fallback.assert_not_called()

    @override_settings(FUEL_PLANNER={**CFG, "GEOCODER_FALLBACK_ENABLED": False})
    def test_unknown_place_without_fallback(self):
        with self.assertRaises(LocationNotFound):
            geocoding_service.resolve_location("Empire State Building")

    def test_fallback_success_is_cached(self):
        item = {"lat": "40.7484", "lon": "-73.9857", "display_name": "Empire State Building, New York",
                "category": "tourism", "address": {"ISO3166-2-lvl4": "US-NY"}}
        with mock.patch.object(nominatim.NominatimClient, "_get", return_value=[item]) as fallback:
            first = geocoding_service.resolve_location("Empire State Building")
            second = geocoding_service.resolve_location("empire state building")
        self.assertEqual(fallback.call_count, 1)
        self.assertEqual((first.source, first.external_calls, second.external_calls), ("nominatim", 1, 0))
        self.assertAlmostEqual(second.lat, 40.7484)

    def test_fallback_not_found(self):
        with mock.patch.object(nominatim.NominatimClient, "_get", return_value=[]):
            with self.assertRaises(LocationNotFound):
                geocoding_service.resolve_location("Qwertyuiop Zxcvbnm")

    def test_fallback_outside_us_states(self):
        item = {"lat": "18.4", "lon": "-66.1", "display_name": "San Juan, Puerto Rico", "category": "place",
                "address": {"ISO3166-2-lvl4": "US-PR"}}
        with mock.patch.object(nominatim.NominatimClient, "_get", return_value=[item]):
            with self.assertRaises(LocationOutsideUSA):
                geocoding_service.resolve_location("San Juan Puerto Rico")

    def test_fallback_unavailable(self):
        with mock.patch.object(nominatim.NominatimClient, "_get", side_effect=nominatim.GeocoderUnavailable("down")):
            with self.assertRaises(GeocoderUnavailable) as ctx:
                geocoding_service.resolve_location("Some Unknown Landmark")
        self.assertEqual(ctx.exception.status_code, 503)


class StationIndexDatabaseTests(TestCase):
    def tearDown(self):
        reset_station_index()

    def test_empty_database_raises_clear_error(self):
        reset_station_index()
        with self.assertRaises(StationDataUnavailable) as ctx:
            get_station_index()
        self.assertIn("load_stations", ctx.exception.message)

    def test_loads_from_database_once(self):
        FuelStation.objects.create(opis_id=7, name="WOODSHED", address="I-44", city="Big Cabin", state="OK",
                                   rack_ids="307", price="3.00733", price_min="3.00733", price_max="3.00733",
                                   latitude=36.53, longitude=-95.22, geocode_source="census_exact_place")
        reset_station_index()
        with self.assertNumQueries(1):
            index = get_station_index()
            again = get_station_index()
        self.assertIs(index, again)
        self.assertIsInstance(index, StationIndex)
        self.assertAlmostEqual(index.prices[0], 3.00733)
