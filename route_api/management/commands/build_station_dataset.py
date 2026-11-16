"""
One-time data preparation: raw OPIS CSV -> cleaned, geocoded station file.

    python manage.py build_station_dataset                # Census + cached fallback lookups
    python manage.py build_station_dataset --no-fallback  # Census gazetteer only, no network

Outputs (committed to the repo so reviewers need no downloads):
    data/processed/fuel_stations.csv       one row per station, with coordinates
    data/processed/rejected_records.csv    every dropped row/station and why
    data/processed/unmatched_cities.csv    city/state pairs that could not be located
    data/processed/ingestion_report.json   counts for every step

Fallback geocoder results are cached in data/cache/fallback_geocoder_cache.json,
so re-running never repeats a request (Nominatim usage policy).
"""

from __future__ import annotations

import csv
import json
import time
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from route_api.services import station_dataset as ds
from route_api.services.gazetteer import US_STATES, PlaceIndex
from route_api.services.nominatim import GeocoderUnavailable, NominatimClient


class Command(BaseCommand):
    help = "Clean, de-duplicate and geocode the fuel price CSV (offline, one-time)."

    def add_arguments(self, parser):
        base = Path(settings.BASE_DIR)
        parser.add_argument("--source", type=Path, default=base / "fuel-prices-for-be-assessment.csv")
        parser.add_argument("--reference-dir", type=Path, default=base / "data" / "reference")
        parser.add_argument("--output-dir", type=Path, default=base / "data" / "processed")
        parser.add_argument("--cache", type=Path, default=base / "data" / "cache" / "fallback_geocoder_cache.json")
        parser.add_argument("--no-fallback", action="store_true", help="Do not query the fallback geocoder")
        parser.add_argument("--retry-failed", action="store_true", help="Re-query cached fallback misses")

    def handle(self, *args, **opts):
        out: Path = opts["output_dir"]
        report: dict = {"source_file": str(opts["source"].name)}
        try:
            rows, rejects = ds.read_source_rows(opts["source"])
        except ds.DatasetError as exc:
            raise CommandError(str(exc)) from exc
        report["source_rows_valid"] = len(rows)
        report["source_rows_rejected_invalid"] = len(rejects)

        rows, non_us = ds.drop_non_us(rows)
        rejects += non_us
        report["rows_dropped_non_us"] = len(non_us)

        rows, dupes = ds.drop_exact_duplicates(rows)
        rejects += dupes
        report["rows_dropped_exact_duplicate"] = len(dupes)
        report["us_rows_after_dedupe"] = len(rows)

        stations = ds.aggregate_by_station(rows)
        report["stations_after_id_merge"] = len(stations)
        report["stations_with_multiple_rows"] = sum(1 for s in stations if len(s.prices) > 1)
        report["stations_with_multiple_prices"] = sum(1 for s in stations if len(set(s.prices)) > 1)
        report["stations_with_name_variants"] = sum(1 for s in stations if s.alt_names)

        index = PlaceIndex.from_csv(opts["reference_dir"] / "us_places.csv")
        fallback = None if opts["no_fallback"] else self._fallback(opts["cache"], opts["retry_failed"])
        stations, geo_rejects, geo_stats = ds.geocode_stations(stations, index, fallback)
        rejects += geo_rejects
        report["geocoding"] = geo_stats
        report["stations_dropped_city_not_found"] = len(geo_rejects)
        if fallback:
            report["fallback_geocoder_requests_this_run"] = self.requests_made
            report["fallback_geocoder_cached_answers"] = self.cache_size()

        bounds = ds.load_state_bounds(opts["reference_dir"] / "us_state_bounds.csv")
        stations, out_of_bounds = ds.check_state_bounds(stations, bounds)
        rejects += out_of_bounds
        report["stations_dropped_outside_state_bounds"] = len(out_of_bounds)

        report["stations_final"] = len(stations)
        report["stations_by_geocode_source"] = dict(Counter(s.geocode_source for s in stations))
        prices = [s.price for s in stations]
        report["price_usd_per_gallon"] = {
            "min": round(min(prices), 5), "max": round(max(prices), 5),
            "mean": round(sum(prices) / len(prices), 5),
        }
        report["rejects_by_reason"] = dict(Counter(r.reason for r in rejects))

        ds.write_stations(stations, out / "fuel_stations.csv")
        ds.write_rejects(rejects, out / "rejected_records.csv")
        self._write_unmatched(geo_rejects + out_of_bounds, out / "unmatched_cities.csv")
        (out / "ingestion_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

        self.stdout.write(json.dumps(report, indent=2))
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(stations)} stations to {out / 'fuel_stations.csv'}"))

    # ------------------------------------------------------------------
    def _fallback(self, cache_path: Path, retry_failed: bool):
        cfg = settings.FUEL_PLANNER
        # Offline batch job: a longer timeout than the request path is acceptable.
        client = NominatimClient(cfg["NOMINATIM_BASE_URL"], cfg["HTTP_USER_AGENT"], timeout=20.0)
        cache: dict = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        self.requests_made = 0

        def lookup(city: str, state: str):
            key = f"{city.lower()}|{state}"
            if key in cache and not (retry_failed and cache[key] is None):
                hit = cache[key]
            else:
                result = None
                for attempt in (1, 2):  # one retry after a pause; still single-threaded
                    self.requests_made += 1
                    try:
                        result = client.geocode_city(city, US_STATES[state])
                        break
                    except GeocoderUnavailable as exc:
                        self.stderr.write(f"  fallback attempt {attempt} failed for {city}, {state}: {exc}")
                        if attempt == 2:
                            self.stderr.write("  giving up on this city for now (not cached; re-run to retry)")
                            return None
                        time.sleep(5)
                hit = None
                if result and result.state_code == state:
                    hit = {"lat": result.lat, "lon": result.lon, "label": result.display_name,
                           "type": result.place_type}
                elif result:
                    self.stdout.write(f"  rejected {city}, {state}: result is in {result.state_code}")
                cache[key] = hit
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
                self.stdout.write(f"  fallback {city}, {state}: {'found' if hit else 'not found'}")
            return (hit["lat"], hit["lon"], hit["label"]) if hit else None

        self.cache_size = lambda: len(cache)
        return lookup

    @staticmethod
    def _write_unmatched(rejects: list, path: Path) -> None:
        counts = Counter((r.city, r.state, r.reason) for r in rejects)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["city", "state", "reason", "stations"])
            for (city, state, reason), n in sorted(counts.items()):
                writer.writerow([city, state, reason, n])
