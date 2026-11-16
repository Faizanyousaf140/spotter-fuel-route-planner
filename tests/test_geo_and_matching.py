"""Route geometry, distance calculations and station-to-route matching. (No network.)"""

import numpy as np
from django.test import SimpleTestCase

from route_api.services.errors import StationDataUnavailable
from route_api.services.geo import (
    decode_polyline, encode_polyline, haversine_miles, is_valid_coordinate, simplify_polyline,
)
from route_api.services.route_matching import build_route_profile, match_stations
from route_api.services.station_service import StationIndex

from .helpers import MILES_PER_DEG, index_of, lon_offset_for_miles, meridian_route, station


class GeoTests(SimpleTestCase):
    def test_haversine_known_distance(self):
        # One degree of latitude along a meridian.
        self.assertAlmostEqual(float(haversine_miles(30, -100, 31, -100)), MILES_PER_DEG, places=6)
        # New York City Hall -> Los Angeles City Hall great-circle distance is ~2,445 miles.
        self.assertAlmostEqual(float(haversine_miles(40.7128, -74.0060, 34.0522, -118.2437)), 2445, delta=3)

    def test_polyline_round_trip(self):
        coords = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
        self.assertEqual(encode_polyline(coords), "_p~iF~ps|U_ulLnnqC_mqNvxq`@")  # Google's reference example
        for (a, b), (c, d) in zip(decode_polyline(encode_polyline(coords)), coords):
            self.assertAlmostEqual(a, c, places=5)
            self.assertAlmostEqual(b, d, places=5)
        decoded6 = decode_polyline(encode_polyline(coords, precision=6), precision=6)
        self.assertAlmostEqual(decoded6[2][1], -126.453, places=6)

    def test_simplify_keeps_endpoints_and_shape(self):
        line = np.array([[0, 0], [0.0001, 1], [0, 2], [1, 3]], dtype=float)
        out = simplify_polyline(line, 0.01)
        self.assertEqual(out.tolist(), [[0, 0], [0, 2], [1, 3]])

    def test_valid_coordinate(self):
        self.assertTrue(is_valid_coordinate(40, -74))
        for lat, lon in [(91, 0), (0, 181), ("x", 0), (float("nan"), 0), (None, 1)]:
            self.assertFalse(is_valid_coordinate(lat, lon))


class RouteProfileTests(SimpleTestCase):
    def test_resampled_spacing_and_total(self):
        coords = meridian_route(30, 40)
        geometry_miles = 10 * MILES_PER_DEG
        profile = build_route_profile(coords, total_miles=geometry_miles, spacing_miles=2.0)
        self.assertAlmostEqual(profile.total_miles, geometry_miles)
        self.assertAlmostEqual(profile.miles[-1], geometry_miles)
        self.assertLessEqual(float(np.max(np.diff(profile.miles))), 2.0 + 1e-9)
        self.assertAlmostEqual(profile.lats[0], 30)
        self.assertAlmostEqual(profile.lats[-1], 40)

    def test_mile_markers_scaled_to_provider_distance(self):
        coords = meridian_route(30, 40)
        geometry_miles = 10 * MILES_PER_DEG
        profile = build_route_profile(coords, total_miles=geometry_miles * 1.1)  # roads longer than geometry
        self.assertAlmostEqual(profile.scale, 1.1, places=6)
        self.assertAlmostEqual(profile.miles[-1], geometry_miles * 1.1)

    def test_long_segments_are_densified(self):
        coords = np.array([[30.0, -100.0], [40.0, -100.0]])  # one 691-mile segment
        profile = build_route_profile(coords, total_miles=10 * MILES_PER_DEG, spacing_miles=2.0)
        self.assertGreater(len(profile.lats), 300)

    def test_duplicate_points_and_zero_length_route(self):
        coords = np.array([[30.0, -100.0], [30.0, -100.0], [31.0, -100.0]])
        profile = build_route_profile(coords, total_miles=MILES_PER_DEG)
        self.assertAlmostEqual(profile.miles[-1], MILES_PER_DEG)
        zero = build_route_profile(np.array([[30.0, -100.0], [30.0, -100.0]]), total_miles=0.0)
        self.assertEqual(len(zero.lats), 1)

    def test_invalid_geometry_rejected(self):
        with self.assertRaises(ValueError):
            build_route_profile(np.zeros((0, 2)), total_miles=10)
        with self.assertRaises(ValueError):
            build_route_profile(meridian_route(), total_miles=10, spacing_miles=0)


class StationMatchingTests(SimpleTestCase):
    def setUp(self):
        self.profile = build_route_profile(meridian_route(30, 40), total_miles=10 * MILES_PER_DEG)

    def test_mile_marker_and_off_route_distance(self):
        on_route = station(1, 35.0, -100.0)
        five_east = station(2, 33.0, -100.0 + lon_offset_for_miles(5, 33.0))
        matches = match_stations(self.profile, index_of(on_route, five_east), corridor_miles=15)
        by_id = {m.station.opis_id: m for m in matches}
        self.assertAlmostEqual(by_id[1].mile_marker, 5 * MILES_PER_DEG, delta=0.01)
        self.assertAlmostEqual(by_id[1].off_route_miles, 0.0, delta=0.01)
        self.assertAlmostEqual(by_id[2].mile_marker, 3 * MILES_PER_DEG, delta=0.05)
        self.assertAlmostEqual(by_id[2].off_route_miles, 5.0, delta=0.05)
        self.assertAlmostEqual(by_id[2].route_lon, -100.0, places=6)

    def test_corridor_boundary(self):
        inside = station(1, 35.0, -100.0 + lon_offset_for_miles(14.9, 35.0))
        outside = station(2, 35.0, -100.0 - lon_offset_for_miles(15.2, 35.0))
        matches = match_stations(self.profile, index_of(inside, outside), corridor_miles=15)
        self.assertEqual([m.station.opis_id for m in matches], [1])
        self.assertEqual(match_stations(self.profile, index_of(inside, outside), corridor_miles=20).__len__(), 2)

    def test_station_outside_bounding_box_and_far_away(self):
        far = station(1, 45.0, -80.0)
        self.assertEqual(match_stations(self.profile, index_of(far), corridor_miles=15), [])

    def test_inside_bounding_box_but_off_the_route(self):
        """Uses real geometry, not the box: an L-shaped route has an empty inner corner."""
        coords = np.vstack([meridian_route(30, 40, -100), np.array([[40.0, lon] for lon in np.linspace(-100, -90, 50)])])
        total = float(np.sum(haversine_miles(coords[:-1, 0], coords[:-1, 1], coords[1:, 0], coords[1:, 1])))
        profile = build_route_profile(coords, total)
        corner = station(1, 32.0, -92.0)  # inside the bbox, ~500 miles from both legs
        near_second_leg = station(2, 40.0 - 5 / MILES_PER_DEG, -95.0)
        matches = match_stations(profile, index_of(corner, near_second_leg), corridor_miles=15)
        self.assertEqual([m.station.opis_id for m in matches], [2])
        self.assertGreater(matches[0].mile_marker, 10 * MILES_PER_DEG)  # beyond the first leg
        self.assertAlmostEqual(matches[0].off_route_miles, 5.0, delta=0.05)

    def test_station_behind_the_start_projects_to_mile_zero(self):
        behind = station(1, 30.0 - 3 / MILES_PER_DEG, -100.0)
        match = match_stations(self.profile, index_of(behind), corridor_miles=15)[0]
        self.assertAlmostEqual(match.mile_marker, 0.0, places=6)
        self.assertAlmostEqual(match.off_route_miles, 3.0, delta=0.01)

    def test_results_sorted_by_mile_then_price(self):
        stations = [station(1, 36.0, -100.0, 3.5), station(2, 31.0, -100.0, 3.9), station(3, 36.0, -100.0, 3.1)]
        matches = match_stations(self.profile, index_of(*stations), corridor_miles=15)
        self.assertEqual([m.station.opis_id for m in matches], [2, 3, 1])

    def test_duplicate_coordinates_all_returned(self):
        stations = [station(i, 35.0, -100.0, 3.0 + i / 10) for i in range(1, 4)]
        self.assertEqual(len(match_stations(self.profile, index_of(*stations), corridor_miles=15)), 3)

    def test_no_stations_near_route(self):
        stations = [station(1, 35.0, -90.0)]
        self.assertEqual(match_stations(self.profile, index_of(*stations), corridor_miles=15), [])

    def test_empty_station_index_raises(self):
        with self.assertRaises(StationDataUnavailable):
            StationIndex([])

    def test_chunked_matching_handles_many_stations(self):
        rng = np.random.default_rng(1)
        stations = [station(i, float(lat), float(lon)) for i, (lat, lon) in
                    enumerate(zip(rng.uniform(25, 45, 3000), rng.uniform(-110, -90, 3000)), start=1)]
        matches = match_stations(self.profile, index_of(*stations), corridor_miles=15)
        self.assertTrue(all(m.off_route_miles <= 15 for m in matches))
        def exact_distance(s):  # distance to the meridian segment lat 30..40, lon -100
            if 30 <= s.latitude <= 40:
                return abs(s.longitude + 100) * MILES_PER_DEG * np.cos(np.radians(s.latitude))
            end = 30 if s.latitude < 30 else 40
            return float(haversine_miles(s.latitude, s.longitude, end, -100))

        brute = {s.opis_id for s in stations if exact_distance(s) <= 15}
        got = {m.station.opis_id for m in matches}
        # Planar approximation may differ only for stations within a hair of the boundary.
        self.assertLessEqual(len(brute ^ got), 1)
