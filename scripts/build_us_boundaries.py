"""Build the offline boundary artifact from official Census polygon records.

Usage: python scripts/build_us_boundaries.py [--archive downloaded.zip]
Only stdlib parsing plus the existing requests dependency is required.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import struct
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

SOURCE = "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_state_500k.zip"
STATES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split())


def attributes(blob):
    count, header, width = struct.unpack_from("<IHH", blob, 4)
    fields, offset = [], 32
    while blob[offset] != 13:
        field = blob[offset:offset + 32]
        fields.append((field[:11].split(b"\0")[0].decode(), field[16]))
        offset += 32
    for i in range(count):
        record = blob[header + i * width:header + (i + 1) * width]
        offset, row = 1, {}
        for name, length in fields:
            row[name] = record[offset:offset + length].decode("utf-8").strip()
            offset += length
        yield row


def polygons(blob):
    offset = 100
    while offset < len(blob):
        _, size = struct.unpack_from(">II", blob, offset)
        shape = blob[offset + 8:offset + 8 + 2 * size]
        offset += 8 + 2 * size
        kind = struct.unpack_from("<I", shape)[0]
        if kind != 5:
            raise ValueError(f"Expected Polygon shape, received {kind}")
        parts, points = struct.unpack_from("<II", shape, 36)
        starts = list(struct.unpack_from(f"<{parts}I", shape, 44)) + [points]
        coordinates = list(struct.iter_unpack("<dd", shape[44 + parts * 4:44 + parts * 4 + points * 16]))
        yield [[[round(lon, 6), round(lat, 6)] for lon, lat in coordinates[a:b]]
               for a, b in zip(starts, starts[1:])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data/reference")
    args = parser.parse_args()
    if args.archive:
        archive = args.archive.read_bytes()
    else:
        response = requests.get(SOURCE, timeout=(5, 30))
        response.raise_for_status()
        archive = response.content
    import io
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        dbf = next(n for n in z.namelist() if n.endswith(".dbf"))
        shp = next(n for n in z.namelist() if n.endswith(".shp"))
        states = {row["STUSPS"]: rings for row, rings in zip(attributes(z.read(dbf)), polygons(z.read(shp)))
                  if row["STUSPS"] in STATES}
    if set(states) != STATES:
        raise ValueError("Boundary archive must contain all 50 states plus DC")
    content = json.dumps({"version": "census-cb2025-500k-r1", "states": states},
                         separators=(",", ":"), sort_keys=True).encode()
    compressed = gzip.compress(content, mtime=0)
    provenance = {
        "version": "census-cb2025-500k-r1", "source_url": SOURCE,
        "source_documentation": "https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.html",
        "retrieved_utc": datetime.now(timezone.utc).isoformat(),
        "source_sha256": hashlib.sha256(archive).hexdigest(),
        "artifact_sha256": hashlib.sha256(compressed).hexdigest(),
        "source_crs": "NAD83 geographic longitude/latitude (EPSG:4269)",
        "scale": "1:500,000", "coordinate_decimals": 6, "states": sorted(states),
        "rings": sum(len(r) for r in states.values()),
        "vertices": sum(len(r) for rings in states.values() for r in rings),
        "license": "US Census Bureau federal government public-domain data",
        "rebuild": "python scripts/build_us_boundaries.py --archive cb_2025_us_state_500k.zip",
        "limitation": "Generalized cartographic land boundaries; narrow coastal/border details may differ from surveyed boundaries. No distance buffer is applied. US territories are excluded."
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "us_boundaries.json.gz").write_bytes(compressed)
    (args.output_dir / "us_boundaries.provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"states": len(states), "vertices": provenance["vertices"], "artifact_bytes": len(compressed)}))


if __name__ == "__main__":
    main()
