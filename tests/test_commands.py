"""
Integration tests for the data pipeline commands:
CSV -> build_station_dataset (cleaning, de-duplication, offline geocoding with the
real Census reference, fallback geocoder MOCKED) -> load_stations -> database.
"""

import csv
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import TestCase

from route_api.models import FuelStation
from route_api.services.nominatim import GeocodeResult, NominatimClient

REFERENCE = Path(settings.BASE_DIR) / "data" / "reference"
CSV_TEXT = (
    "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
    '7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333\n'
    '105,TA SAGINAW I 75 TRAVEL CENTER,"I-75, EXIT 144-B",Bridgeport,MI,260,3.269\n'
    '105,TA SAGINAW I 75 TRAVEL CENTER,"I-75, EXIT 144-B",Bridgeport,MI,260,3.339\n'
    '20,PILOT TRAVEL CENTER #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.899\n'
    '20,PILOT #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.899\n'
    '20,PILOT #1243,"I-8, EXIT 119 & SR-85",Gila Bend                    ,AZ,930,3.899\n'
    "900,ESSO,HWY 1,Calgary,AB,1,1.5\n"
    "901,MYSTERY STOP,US-1,Zzyzx Nowhere,TX,1,3.1\n"
    "902,BAD PRICE,US-1,Austin,TX,1,n/a\n"
)


class BuildAndLoadStationsTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source = self.tmp / "prices.csv"
        self.source.write_text(CSV_TEXT, encoding="utf-8")
        self.out = self.tmp / "processed"

    def build(self, *extra):
        stdout = StringIO()
        call_command("build_station_dataset", "--source", str(self.source), "--reference-dir", str(REFERENCE),
                     "--output-dir", str(self.out), "--cache", str(self.tmp / "cache.json"), *extra, stdout=stdout)
        return json.loads((self.out / "ingestion_report.json").read_text(encoding="utf-8"))

    def test_offline_build_then_load(self):
        report = self.build("--no-fallback")
        self.assertEqual(report["source_rows_valid"], 8)                  # 9 data rows, 1 invalid
        self.assertEqual(report["source_rows_rejected_invalid"], 1)       # "n/a" price
        self.assertEqual(report["rows_dropped_non_us"], 1)                # Calgary, AB
        self.assertEqual(report["rows_dropped_exact_duplicate"], 1)       # Gila Bend padded duplicate
        self.assertEqual(report["stations_after_id_merge"], 4)            # 7, 20, 105, 901
        self.assertEqual(report["stations_dropped_city_not_found"], 1)    # Zzyzx Nowhere
        self.assertEqual(report["stations_final"], 3)

        with open(self.out / "fuel_stations.csv", newline="", encoding="utf-8") as handle:
            rows = {int(r["opis_id"]): r for r in csv.DictReader(handle)}
        self.assertEqual(set(rows), {7, 20, 105})
        self.assertEqual(rows[105]["price"], "3.30400")                   # average of 3.269 and 3.339
        self.assertEqual((rows[105]["price_min"], rows[105]["price_max"]), ("3.26900", "3.33900"))
        # After the exact duplicate is dropped each name appears once: tie -> first seen.
        self.assertEqual(rows[20]["name"], "PILOT TRAVEL CENTER #1243")
        self.assertEqual(rows[20]["alt_names"], "PILOT #1243")
        self.assertAlmostEqual(float(rows[7]["latitude"]), 36.5, delta=0.2)    # Big Cabin, OK
        self.assertAlmostEqual(float(rows[7]["longitude"]), -95.2, delta=0.2)
        self.assertTrue(rows[7]["geocode_source"].startswith("census_"))

        unmatched = (self.out / "unmatched_cities.csv").read_text(encoding="utf-8")
        self.assertIn("Zzyzx Nowhere", unmatched)
        rejected = (self.out / "rejected_records.csv").read_text(encoding="utf-8")
        for reason in ("invalid_price", "non_us_canadian_province", "exact_duplicate", "city_not_found"):
            self.assertIn(reason, rejected)

        call_command("load_stations", "--path", str(self.out / "fuel_stations.csv"), stdout=StringIO())
        self.assertEqual(FuelStation.objects.count(), 3)
        ta = FuelStation.objects.get(opis_id=105)
        self.assertEqual(str(ta.price), "3.30400")
        self.assertEqual(ta.price_observations, 2)
        call_command("load_stations", "--path", str(self.out / "fuel_stations.csv"), stdout=StringIO())
        self.assertEqual(FuelStation.objects.count(), 3)                  # idempotent

    def test_fallback_geocoder_is_cached_and_state_checked(self):
        found = GeocodeResult(31.0, -100.0, "Zzyzx Nowhere, Texas", "TX", "place", "hamlet")
        with mock.patch.object(NominatimClient, "geocode_city", return_value=found) as fallback:
            report = self.build()
        self.assertEqual(fallback.call_count, 1)
        self.assertEqual(report["stations_final"], 4)
        self.assertEqual(report["stations_by_geocode_source"]["fallback_geocoder"], 1)
        cache = json.loads((self.tmp / "cache.json").read_text(encoding="utf-8"))
        self.assertIn("zzyzx nowhere|TX", cache)
        with mock.patch.object(NominatimClient, "geocode_city") as fallback:
            report = self.build()                                         # second run: served from cache
        fallback.assert_not_called()
        self.assertEqual(report["fallback_geocoder_requests_this_run"], 0)

    def test_fallback_result_in_wrong_state_is_rejected(self):
        wrong = GeocodeResult(35.0, -97.0, "Zzyzx Nowhere, Oklahoma", "OK", "place", "hamlet")
        with mock.patch.object(NominatimClient, "geocode_city", return_value=wrong):
            report = self.build()
        self.assertEqual(report["stations_final"], 3)

    def test_load_stations_errors(self):
        with self.assertRaises(CommandError):
            call_command("load_stations", "--path", str(self.tmp / "missing.csv"), stdout=StringIO())
        bad = self.tmp / "bad.csv"
        bad.write_text("opis_id,name,address,city,state,rack_ids,price,price_min,price_max,price_observations,"
                       "latitude,longitude,geocode_source,geocode_match,alt_names\n"
                       "1,A,B,C,TX,1,3,3,3,1,999,0,x,y,\n", encoding="utf-8")
        with self.assertRaises(CommandError):   # only row has invalid coordinates -> nothing to load
            call_command("load_stations", "--path", str(bad), stdout=StringIO())

    def test_malformed_csv_is_fatal(self):
        self.source.write_text("Name,Price\nA,3\n", encoding="utf-8")
        with self.assertRaisesMessage(CommandError, "Missing required column"):
            self.build("--no-fallback")
