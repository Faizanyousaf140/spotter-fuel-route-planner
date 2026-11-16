"""
One-time preparation of the OPIS fuel price file (offline, not on the request path).

Pipeline (each step reports what it removed):
  1. read + validate rows                 -> malformed rows rejected
  2. drop non-US rows (Canadian provinces)
  3. clean text, drop exact duplicates    (after whitespace normalisation)
  4. one record per OPIS Truckstop ID     (AVERAGE price, min/max kept)
  5. coordinates from city + state        (Census gazetteer, optional fallback geocoder)
  6. state bounding-box sanity check      -> outliers dropped
"""

from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .gazetteer import CANADIAN_PROVINCES, US_STATES, PlaceIndex, clean_text

REQUIRED_COLUMNS = [
    "OPIS Truckstop ID", "Truckstop Name", "Address", "City", "State", "Rack ID", "Retail Price",
]


class DatasetError(Exception):
    """The source file cannot be processed at all (missing file/columns, empty)."""


@dataclass(frozen=True)
class SourceRow:
    line_no: int
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: str
    price: float

    @property
    def identity(self) -> tuple:
        """All seven source fields, used for exact-duplicate detection."""
        return (self.opis_id, self.name, self.address, self.city, self.state, self.rack_id, self.price)


@dataclass
class Station:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_ids: list[str]
    prices: list[float]
    alt_names: list[str] = field(default_factory=list)
    latitude: float | None = None
    longitude: float | None = None
    geocode_source: str = ""
    geocode_match: str = ""

    @property
    def price(self) -> float:
        return sum(self.prices) / len(self.prices)


@dataclass
class Reject:
    stage: str
    reason: str
    line_no: int | str = ""
    opis_id: int | str = ""
    name: str = ""
    city: str = ""
    state: str = ""
    detail: str = ""


# --------------------------------------------------------------------------
# Steps 1-4
# --------------------------------------------------------------------------
def read_source_rows(path: Path) -> tuple[list[SourceRow], list[Reject]]:
    if not Path(path).exists():
        raise DatasetError(f"Fuel price file not found: {path}")
    rows: list[SourceRow] = []
    rejects: list[Reject] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = [clean_text(h) for h in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise DatasetError(f"Missing required column(s): {', '.join(missing)}")
        reader.fieldnames = header
        for raw in reader:
            line_no = reader.line_num
            if None in raw or any(v is None for v in raw.values()):
                rejects.append(Reject("read", "malformed_row", line_no, detail="wrong number of fields"))
                continue
            values = {k: clean_text(v) for k, v in raw.items()}
            reason = _validate(values)
            if reason:
                rejects.append(
                    Reject("read", reason, line_no, values.get("OPIS Truckstop ID", ""),
                           values.get("Truckstop Name", ""), values.get("City", ""),
                           values.get("State", ""), detail=values.get("Retail Price", ""))
                )
                continue
            rows.append(
                SourceRow(
                    line_no=line_no,
                    opis_id=int(values["OPIS Truckstop ID"]),
                    name=values["Truckstop Name"],
                    address=values["Address"],
                    city=values["City"],
                    state=values["State"].upper(),
                    rack_id=values["Rack ID"],
                    price=float(values["Retail Price"]),
                )
            )
    if not rows:
        raise DatasetError("No valid rows found in the fuel price file")
    return rows, rejects


def _validate(values: dict) -> str | None:
    if not values["OPIS Truckstop ID"].isdigit() or int(values["OPIS Truckstop ID"]) <= 0:
        return "invalid_opis_id"
    try:
        price = float(values["Retail Price"])
    except ValueError:
        return "invalid_price"
    if not math.isfinite(price) or price <= 0:
        return "invalid_price"
    if not values["Truckstop Name"]:
        return "missing_name"
    if not values["City"]:
        return "missing_city"
    if len(values["State"]) != 2 or not values["State"].isalpha():
        return "invalid_state"
    return None


def drop_non_us(rows: Iterable[SourceRow]) -> tuple[list[SourceRow], list[Reject]]:
    kept, rejects = [], []
    for row in rows:
        if row.state in US_STATES:
            kept.append(row)
        else:
            reason = "non_us_canadian_province" if row.state in CANADIAN_PROVINCES else "unknown_state"
            rejects.append(Reject("filter", reason, row.line_no, row.opis_id, row.name, row.city, row.state))
    return kept, rejects


def drop_exact_duplicates(rows: Iterable[SourceRow]) -> tuple[list[SourceRow], list[Reject]]:
    seen: set[tuple] = set()
    kept, rejects = [], []
    for row in rows:
        if row.identity in seen:
            rejects.append(Reject("dedupe", "exact_duplicate", row.line_no, row.opis_id, row.name, row.city, row.state))
            continue
        seen.add(row.identity)
        kept.append(row)
    return kept, rejects


def aggregate_by_station(rows: Iterable[SourceRow]) -> list[Station]:
    """One Station per OPIS ID; price = mean of that ID's (non-duplicate) rows."""
    groups: dict[int, list[SourceRow]] = defaultdict(list)
    for row in rows:
        groups[row.opis_id].append(row)
    stations: list[Station] = []
    for opis_id in sorted(groups):
        group = groups[opis_id]
        name_counts = Counter(r.name for r in group)
        name = name_counts.most_common(1)[0][0]  # most common; ties -> first seen
        location = Counter((r.address, r.city, r.state) for r in group).most_common(1)[0][0]
        stations.append(
            Station(
                opis_id=opis_id,
                name=name,
                address=location[0],
                city=location[1],
                state=location[2],
                rack_ids=sorted({r.rack_id for r in group}),
                prices=[r.price for r in group],
                alt_names=sorted(n for n in name_counts if n != name),
            )
        )
    return stations


# --------------------------------------------------------------------------
# Steps 5-6
# --------------------------------------------------------------------------
FallbackGeocoder = Callable[[str, str], "tuple[float, float, str] | None"]


def geocode_stations(
    stations: list[Station], index: PlaceIndex, fallback: FallbackGeocoder | None = None
) -> tuple[list[Station], list[Reject], dict[str, int]]:
    """
    Attach coordinates to each station from (city, state).

    Lookups are done once per unique (normalised city, state) pair. Pairs the
    Census gazetteer cannot resolve are passed to ``fallback`` (if given).
    """
    pair_result: dict[tuple[str, str], tuple[float, float, str, str] | None] = {}
    stats: Counter = Counter()
    for station in stations:
        pair = (station.city.lower(), station.state)
        if pair in pair_result:
            continue
        match = index.lookup(station.city, station.state)
        if match:
            pair_result[pair] = (
                match.place.lat, match.place.lon, f"census_{match.method}", match.place.label
            )
            stats[f"pairs_census_{match.method}"] += 1
        else:
            pair_result[pair] = None

    unresolved = [pair for pair, res in pair_result.items() if res is None]
    if fallback:
        display = {(s.city.lower(), s.state): s.city for s in stations}
        for pair in unresolved:
            found = fallback(display[pair], pair[1])
            if found:
                lat, lon, label = found
                pair_result[pair] = (lat, lon, "fallback_geocoder", label)
                stats["pairs_fallback_geocoder"] += 1
            else:
                stats["pairs_unresolved"] += 1
    else:
        stats["pairs_unresolved"] += len(unresolved)
    stats["pairs_total"] = len(pair_result)

    kept, rejects = [], []
    for station in stations:
        result = pair_result[(station.city.lower(), station.state)]
        if result is None:
            rejects.append(Reject("geocode", "city_not_found", "", station.opis_id, station.name, station.city, station.state))
            continue
        station.latitude, station.longitude, station.geocode_source, station.geocode_match = result
        kept.append(station)
    return kept, rejects, dict(stats)


def load_state_bounds(path: Path) -> dict[str, tuple[float, float, float, float]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return {
            r["state"]: (float(r["min_lat"]), float(r["max_lat"]), float(r["min_lon"]), float(r["max_lon"]))
            for r in csv.DictReader(handle)
        }


def check_state_bounds(
    stations: list[Station], bounds: dict[str, tuple[float, float, float, float]]
) -> tuple[list[Station], list[Reject]]:
    kept, rejects = [], []
    for s in stations:
        box = bounds.get(s.state)
        inside = box is not None and box[0] <= s.latitude <= box[1] and box[2] <= s.longitude <= box[3]
        if inside:
            kept.append(s)
        else:
            rejects.append(
                Reject("bounds", "outside_state_bounding_box", "", s.opis_id, s.name, s.city, s.state,
                       detail=f"{s.latitude:.5f},{s.longitude:.5f} via {s.geocode_source}")
            )
    return kept, rejects


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
STATION_FIELDS = [
    "opis_id", "name", "address", "city", "state", "rack_ids", "price", "price_min", "price_max",
    "price_observations", "latitude", "longitude", "geocode_source", "geocode_match", "alt_names",
]


def write_stations(stations: list[Station], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=STATION_FIELDS)
        writer.writeheader()
        for s in stations:
            writer.writerow({
                "opis_id": s.opis_id, "name": s.name, "address": s.address, "city": s.city,
                "state": s.state, "rack_ids": "|".join(s.rack_ids),
                "price": f"{s.price:.5f}", "price_min": f"{min(s.prices):.5f}",
                "price_max": f"{max(s.prices):.5f}", "price_observations": len(s.prices),
                "latitude": f"{s.latitude:.6f}", "longitude": f"{s.longitude:.6f}",
                "geocode_source": s.geocode_source, "geocode_match": s.geocode_match,
                "alt_names": "|".join(s.alt_names),
            })


def write_rejects(rejects: list[Reject], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["stage", "reason", "line_no", "opis_id", "name", "city", "state", "detail"]
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for r in rejects:
            writer.writerow({f: getattr(r, f) for f in fields})
