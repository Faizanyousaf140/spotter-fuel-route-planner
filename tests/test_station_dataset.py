"""Data preparation: CSV validation, filtering, de-duplication, geocoding. (No network.)"""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from route_api.services import station_dataset as ds
from route_api.services.gazetteer import (
    Place, PlaceIndex, census_name_aliases, normalize_place_name, parse_city_state,
)

HEADER = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"


def write_csv(text: str) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8", newline="")
    handle.write(text)
    handle.close()
    return Path(handle.name)


def place(state, name, lat, lon, kind="place", incorporated=True, area=1.0):
    return Place(state, name, lat, lon, kind, incorporated, area)


class ReadSourceRowsTests(SimpleTestCase):
    def test_reads_and_cleans_whitespace(self):
        path = write_csv(HEADER + '7,WOODSHED  OF BIG CABIN,"I-44,  EXIT 283",Big Cabin      ,OK,307,3.00733333\n')
        rows, rejects = ds.read_source_rows(path)
        self.assertEqual(rejects, [])
        self.assertEqual(rows[0].name, "WOODSHED OF BIG CABIN")
        self.assertEqual(rows[0].address, "I-44, EXIT 283")
        self.assertEqual(rows[0].city, "Big Cabin")
        self.assertAlmostEqual(rows[0].price, 3.00733333)

    def test_missing_column_is_fatal(self):
        path = write_csv("OPIS Truckstop ID,Truckstop Name,City,State,Retail Price\n1,A,B,TX,3.0\n")
        with self.assertRaisesMessage(ds.DatasetError, "Missing required column(s): Address, Rack ID"):
            ds.read_source_rows(path)

    def test_missing_file_is_fatal(self):
        with self.assertRaises(ds.DatasetError):
            ds.read_source_rows(Path("does-not-exist.csv"))

    def test_empty_file_is_fatal(self):
        with self.assertRaisesMessage(ds.DatasetError, "No valid rows"):
            ds.read_source_rows(write_csv(HEADER))

    def test_invalid_and_malformed_rows_are_rejected_not_fatal(self):
        path = write_csv(
            HEADER
            + "1,GOOD,US-1,Austin,TX,1,3.10\n"
            + "2,NO PRICE,US-1,Austin,TX,1,\n"
            + "3,BAD PRICE,US-1,Austin,TX,1,abc\n"
            + "4,NEG PRICE,US-1,Austin,TX,1,-1\n"
            + "5,NAN PRICE,US-1,Austin,TX,1,nan\n"
            + "x,BAD ID,US-1,Austin,TX,1,3.0\n"
            + "6,SHORT ROW,US-1,Austin\n"
            + "7,EXTRA,US-1,Austin,TX,1,3.0,unexpected\n"
            + "8,BAD STATE,US-1,Austin,T1,1,3.0\n"
        )
        rows, rejects = ds.read_source_rows(path)
        self.assertEqual([r.opis_id for r in rows], [1])
        reasons = sorted(r.reason for r in rejects)
        self.assertEqual(
            reasons,
            ["invalid_opis_id", "invalid_price", "invalid_price", "invalid_price", "invalid_price",
             "invalid_state", "malformed_row", "malformed_row"],
        )


class FilterAndDedupeTests(SimpleTestCase):
    def rows(self, text):
        return ds.read_source_rows(write_csv(HEADER + text))[0]

    def test_drops_canadian_provinces(self):
        rows = self.rows("1,A,HWY 1,Calgary,AB,1,3.0\n2,B,I-35,Austin,TX,1,3.0\n")
        kept, rejects = ds.drop_non_us(rows)
        self.assertEqual([r.state for r in kept], ["TX"])
        self.assertEqual(rejects[0].reason, "non_us_canadian_province")

    def test_exact_duplicates_dropped_after_whitespace_normalisation(self):
        rows = self.rows("20,PILOT #1243,I-8,Gila Bend,AZ,930,3.899\n20,PILOT  #1243,I-8,Gila Bend ,AZ,930,3.899\n")
        kept, rejects = ds.drop_exact_duplicates(rows)
        self.assertEqual(len(kept), 1)
        self.assertEqual(rejects[0].reason, "exact_duplicate")

    def test_same_id_different_price_is_not_an_exact_duplicate(self):
        rows = self.rows("105,TA,I-75,Bridgeport,MI,260,3.269\n105,TA,I-75,Bridgeport,MI,260,3.339\n")
        kept, _ = ds.drop_exact_duplicates(rows)
        self.assertEqual(len(kept), 2)

    def test_one_station_per_id_with_average_price(self):
        rows = self.rows(
            "105,TA SAGINAW,I-75,Bridgeport,MI,260,3.20\n"
            "105,TA SAGINAW,I-75,Bridgeport,MI,260,3.30\n"
            "105,TA SAGINAW TC,I-75,Bridgeport,MI,261,3.40\n"
            "9,KWIK TRIP,I-94,Tomah,WI,420,3.0\n"
        )
        stations = ds.aggregate_by_station(rows)
        self.assertEqual([s.opis_id for s in stations], [9, 105])
        ta = stations[1]
        self.assertAlmostEqual(ta.price, 3.30)
        self.assertEqual((min(ta.prices), max(ta.prices)), (3.20, 3.40))
        self.assertEqual(ta.name, "TA SAGINAW")
        self.assertEqual(ta.alt_names, ["TA SAGINAW TC"])
        self.assertEqual(ta.rack_ids, ["260", "261"])


class GazetteerTests(SimpleTestCase):
    def test_normalisation(self):
        self.assertEqual(normalize_place_name("Mc Calla"), normalize_place_name("McCalla"))
        self.assertEqual(normalize_place_name("St. Louis"), "saintlouis")
        self.assertEqual(normalize_place_name("Ft Worth"), "fortworth")
        self.assertEqual(normalize_place_name("Cañon City"), "canoncity")
        self.assertEqual(normalize_place_name("  O'Fallon  "), "ofallon")

    def test_census_aliases(self):
        self.assertEqual(census_name_aliases("Tomah city")[0], "tomah")
        primary, aliases = census_name_aliases("Nashville-Davidson metropolitan government (balance)")
        self.assertIn("nashville", aliases)
        self.assertIn("boise", census_name_aliases("Boise City city")[1])
        self.assertIn("carsoncity", census_name_aliases("Carson City")[1])

    def test_lookup_prefers_place_then_incorporated_then_area(self):
        index = PlaceIndex([
            place("TX", "Springfield township", 1, 1, kind="cousub"),
            place("TX", "Springfield CDP", 2, 2, incorporated=False, area=50),
            place("TX", "Springfield city", 3, 3, area=5),
        ])
        match = index.lookup("Springfield", "TX")
        self.assertEqual(match.place.lat, 3)
        self.assertEqual(match.method, "exact_place")
        self.assertEqual(match.candidates, 2)

    def test_lookup_respects_state(self):
        index = PlaceIndex([place("OK", "Big Cabin town", 36.5, -95.2)])
        self.assertIsNone(index.lookup("Big Cabin", "TX"))
        self.assertIsNotNone(index.lookup("big cabin", "ok"))

    def test_cousub_used_only_when_no_place(self):
        index = PlaceIndex([place("NJ", "Mahwah township", 41.1, -74.2, kind="cousub")])
        self.assertEqual(index.lookup("Mahwah", "NJ").method, "exact_cousub")
        self.assertIsNone(index.lookup("Mahwah", "NJ", allow_cousub=False))

    def test_parse_city_state(self):
        self.assertEqual(parse_city_state("New York, NY"), ("New York", "NY"))
        self.assertEqual(parse_city_state("Washington, DC"), ("Washington", "DC"))
        self.assertEqual(parse_city_state("Salt Lake City, Utah"), ("Salt Lake City", "UT"))
        self.assertEqual(parse_city_state("Fargo North Dakota"), ("Fargo", "ND"))
        self.assertEqual(parse_city_state("Austin, TX 78701"), ("Austin", "TX"))
        self.assertEqual(parse_city_state("Chicago, IL, USA"), ("Chicago", "IL"))
        self.assertIsNone(parse_city_state("Toronto, ON"))
        self.assertIsNone(parse_city_state("Paris"))


class GeocodeAndBoundsTests(SimpleTestCase):
    def setUp(self):
        self.index = PlaceIndex([place("OK", "Big Cabin town", 36.53, -95.22)])
        self.stations = [
            ds.Station(1, "A", "I-44", "Big Cabin", "OK", ["1"], [3.0]),
            ds.Station(2, "B", "I-44", "Big  Cabin", "OK", ["1"], [3.1]),
            ds.Station(3, "C", "US-1", "Nowhere", "TX", ["1"], [3.2]),
            ds.Station(4, "D", "US-1", "Nowhere", "TX", ["1"], [3.3]),
        ]

    def test_census_match_and_unresolved_dropped_without_fallback(self):
        kept, rejects, stats = ds.geocode_stations(self.stations, self.index)
        self.assertEqual([s.opis_id for s in kept], [1, 2])
        self.assertEqual(kept[0].geocode_source, "census_exact_place")
        self.assertEqual({r.reason for r in rejects}, {"city_not_found"})
        self.assertEqual(stats["pairs_unresolved"], 1)

    def test_fallback_called_once_per_unresolved_pair(self):
        calls = []

        def fallback(city, state):
            calls.append((city, state))
            return (31.0, -99.0, "Nowhere, Texas")

        kept, rejects, stats = ds.geocode_stations(self.stations, self.index, fallback)
        self.assertEqual(calls, [("Nowhere", "TX")])
        self.assertEqual(len(kept), 4)
        self.assertEqual(kept[2].geocode_source, "fallback_geocoder")
        self.assertEqual(stats["pairs_fallback_geocoder"], 1)

    def test_state_bounds_outliers_dropped(self):
        stations, _, _ = ds.geocode_stations(self.stations[:1], self.index)
        bounds = {"OK": (33.4, 37.2, -103.2, -94.2)}
        kept, rejects = ds.check_state_bounds(stations, bounds)
        self.assertEqual(len(kept), 1)
        stations[0].latitude = 45.0  # pretend the geocoder put it in the wrong state
        kept, rejects = ds.check_state_bounds(stations, bounds)
        self.assertEqual(kept, [])
        self.assertEqual(rejects[0].reason, "outside_state_bounding_box")
