"""Highway parsing / location confidence, and the vectorised polyline decoder. (No network.)"""

import random

import numpy as np
from django.test import SimpleTestCase

from route_api.services.geo import decode_polyline, decode_polyline_array, encode_polyline
from route_api.services.highways import (
    CONFIDENCE_HIGH, CONFIDENCE_LOW, CONFIDENCE_MEDIUM, RoadSegment, assess_location, route_ref_tokens,
    station_highways,
)


class HighwayParsingTests(SimpleTestCase):
    def test_station_addresses_from_the_price_file(self):
        cases = {
            ("I-44, EXIT 283 & US-69", "OK"): {"I44", "US69"},
            ("I-94, EXIT 143 & US-12 & SR-21", "WI"): {"I94", "US12", "WI21"},
            ("I-8, EXIT 119 & SR-85", "AZ"): {"I8", "AZ85"},
            ("I-29 & I-80, EXIT 3", "IA"): {"I29", "I80"},
            ("I-35,  EXIT 271", "TX"): {"I35"},
            ("US-46", "NJ"): {"US46"},
            ("SR-63/SR-16", "MO"): {"MO63", "MO16"},
            ("HIGHWAY 11/17", "MS"): set(),          # ambiguous: not matched rather than guessed
            ("", "TX"): set(),
        }
        for (address, state), expected in cases.items():
            with self.subTest(address=address):
                self.assertEqual(station_highways(address, state), frozenset(expected))

    def test_route_refs_from_osrm(self):
        self.assertEqual(route_ref_tokens("I 80; I 94; US 6"), {"I80", "I94", "US6"})
        self.assertEqual(route_ref_tokens("PA 28 Truck"), {"PA28"})
        self.assertEqual(route_ref_tokens("US 1 Truck; US 9 Truck"), {"US1", "US9"})
        self.assertEqual(route_ref_tokens(""), frozenset())

    def test_confidence_levels(self):
        segments = [RoadSegment(0, 100, frozenset({"I80"})), RoadSegment(100, 300, frozenset({"I80", "US6"}))]
        high = assess_location(frozenset({"I80"}), 250, 12.0, segments)
        self.assertEqual((high.level, high.matched_highways), (CONFIDENCE_HIGH, ("I80",)))
        far = assess_location(frozenset({"I80"}), 400, 2.0, segments)          # highway not near this mile
        self.assertEqual(far.level, CONFIDENCE_MEDIUM)
        self.assertEqual(assess_location(frozenset({"I35"}), 50, 1.0, segments).level, CONFIDENCE_MEDIUM)
        self.assertEqual(assess_location(frozenset({"I35"}), 50, 9.0, segments).level, CONFIDENCE_LOW)
        self.assertEqual(assess_location(frozenset(), 50, 9.0, []).level, CONFIDENCE_LOW)


class PolylineArrayTests(SimpleTestCase):
    def test_matches_reference_decoder(self):
        rng = random.Random(3)
        for _ in range(300):
            pts = [(rng.uniform(-90, 90), rng.uniform(-180, 180)) for _ in range(rng.randint(1, 50))]
            for precision in (5, 6):
                encoded = encode_polyline(pts, precision)
                np.testing.assert_allclose(decode_polyline_array(encoded, precision),
                                           np.array(decode_polyline(encoded, precision)), atol=1e-12)

    def test_malformed_input_raises_value_error(self):
        good = encode_polyline([(40.7, -74.0), (34.05, -118.24)], 6)
        for bad in (good[:-1], "éabc", " ", good + "_"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                decode_polyline_array(bad, 6)
        self.assertEqual(decode_polyline_array("", 6).shape, (0, 2))
