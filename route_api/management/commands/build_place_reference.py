"""
Build ``data/reference/us_places.csv`` and ``data/reference/us_state_bounds.csv``
from the US Census Bureau Gazetteer files (public domain).

The generated files are committed, so this only needs to be re-run to refresh
the reference data:

    python manage.py build_place_reference                 # downloads from census.gov
    python manage.py build_place_reference --source-dir X  # uses local *.txt / *.zip
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from route_api.services.gazetteer import US_STATES, clean_text

CENSUS_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{year}_Gazetteer/{year}_Gaz_{layer}_national.zip"
LAYERS = ("place", "cousubs")
# Functional status codes for county subdivisions that are real local
# governments (towns/townships). Statistical CCDs ("S") cover large areas whose
# internal point can be far from any town, so they are excluded.
COUSUB_FUNCSTAT_KEEP = {"A", "B"}
STATE_BOUNDS_MARGIN_DEG = 0.25


class Command(BaseCommand):
    help = "Build the offline US places reference (Census Gazetteer) used for geocoding."

    def add_arguments(self, parser):
        parser.add_argument("--year", default="2026", help="Census Gazetteer vintage (default 2026)")
        parser.add_argument("--source-dir", type=Path, help="Directory containing the Gazetteer .txt or .zip files")
        parser.add_argument(
            "--output-dir", type=Path, default=Path(settings.BASE_DIR) / "data" / "reference"
        )

    def handle(self, *args, **opts):
        out_dir: Path = opts["output_dir"]
        out_dir.mkdir(parents=True, exist_ok=True)
        rows: list[dict] = []
        for layer in LAYERS:
            text = self._read_layer(layer, opts["year"], opts["source_dir"])
            layer_rows = list(self._parse(layer, text))
            self.stdout.write(f"{layer}: {len(layer_rows)} rows kept")
            rows.extend(layer_rows)

        places_path = out_dir / "us_places.csv"
        fields = ["state", "name", "kind", "incorporated", "land_sqmi", "lat", "lon"]
        with open(places_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

        bounds_path = out_dir / "us_state_bounds.csv"
        with open(bounds_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["state", "min_lat", "max_lat", "min_lon", "max_lon"])
            for state in sorted(US_STATES):
                pts = [(r["lat"], r["lon"]) for r in rows if r["state"] == state]
                if not pts:
                    raise CommandError(f"No gazetteer points for state {state}")
                lats = [p[0] for p in pts]
                lons = [p[1] for p in pts]
                m = STATE_BOUNDS_MARGIN_DEG
                writer.writerow(
                    [state, round(min(lats) - m, 4), round(max(lats) + m, 4),
                     round(min(lons) - m, 4), round(max(lons) + m, 4)]
                )
        self.stdout.write(self.style.SUCCESS(f"Wrote {places_path} ({len(rows)} rows) and {bounds_path}"))

    def _read_layer(self, layer: str, year: str, source_dir: Path | None) -> str:
        if source_dir:
            txt = source_dir / f"{year}_Gaz_{layer}_national.txt"
            zpath = source_dir / f"{year}_Gaz_{layer}_national.zip"
            if txt.exists():
                return txt.read_text(encoding="utf-8")
            if zpath.exists():
                return self._unzip(zpath.read_bytes())
            raise CommandError(f"Neither {txt} nor {zpath} exists")
        url = CENSUS_URL.format(year=year, layer=layer)
        self.stdout.write(f"Downloading {url}")
        response = requests.get(url, timeout=60)
        response.raise_for_status()
        return self._unzip(response.content)

    @staticmethod
    def _unzip(blob: bytes) -> str:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            name = next(n for n in archive.namelist() if n.endswith(".txt"))
            return archive.read(name).decode("utf-8")

    @staticmethod
    def _parse(layer: str, text: str):
        reader = csv.DictReader(io.StringIO(text), delimiter="|")
        for raw in reader:
            row = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
            state = row["USPS"]
            if state not in US_STATES:  # drops PR and other territories
                continue
            funcstat = row.get("FUNCSTAT", "")
            if layer == "cousubs" and funcstat not in COUSUB_FUNCSTAT_KEEP:
                continue
            yield {
                "state": state,
                "name": clean_text(row["NAME"]),
                "kind": "place" if layer == "place" else "cousub",
                "incorporated": "1" if funcstat == "A" else "0",
                "land_sqmi": row.get("ALAND_SQMI", "0") or "0",
                "lat": float(row["INTPTLAT"]),
                "lon": float(row["INTPTLONG"]),
            }
