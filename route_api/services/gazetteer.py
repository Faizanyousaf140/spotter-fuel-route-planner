"""
Offline US place gazetteer built from the US Census Bureau Gazetteer files
(public domain): incorporated places / CDPs and county subdivisions.

Used for two things:
  * geocoding the truck-stop cities during offline ingestion, and
  * resolving "City, ST" start/finish inputs at request time without calling
    any external geocoding API.
"""

from __future__ import annotations

import csv
import re
import threading
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from .geo import haversine_miles

US_STATES: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
STATE_NAME_TO_CODE = {name.lower(): code for code, name in US_STATES.items()}
STATE_NAME_TO_CODE["washington dc"] = "DC"
STATE_NAME_TO_CODE["d.c."] = "DC"

CANADIAN_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}

# Legal/statistical area descriptions appended to Census names
# ("Tomah city", "Big Cabin town", "Abanda CDP", ...). Longest first.
_CENSUS_SUFFIXES = sorted(
    [
        "city and borough", "consolidated government (balance)",
        "unified government (balance)", "metro government (balance)",
        "metropolitan government (balance)", "urban county", "city (balance)",
        "(balance)", "city", "town", "village", "borough", "cdp", "municipality",
        "comunidad", "zona urbana", "charter township", "township", "plantation",
        "gore", "grant", "purchase", "location", "ccd", "ut", "district", "precinct",
        "reservation", "unorganized territory", "corporation", "census subarea",
    ],
    key=len,
    reverse=True,
)

_TOKEN_ALIASES = {"st": "saint", "ste": "sainte", "ft": "fort", "mt": "mount", "pt": "point"}


def clean_text(value: str) -> str:
    """Trim and collapse internal whitespace."""
    return re.sub(r"\s+", " ", (value or "").strip())


def normalize_place_name(name: str) -> str:
    """
    Canonical comparison key for a place name.

    ``"Mc Calla"`` / ``"McCalla"`` -> ``"mccalla"``; ``"St. Louis"`` ->
    ``"saintlouis"``; ``"Ft Worth"`` -> ``"fortworth"``.
    """
    text = unicodedata.normalize("NFKD", clean_text(name))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))  # strip accents (n-tilde -> n)
    text = text.lower().replace("&", " and ")
    text = re.sub("[.'`\u2019]", "", text)
    text = re.sub(r"[-/,()]", " ", text)
    tokens = [_TOKEN_ALIASES.get(tok, tok) for tok in text.split()]
    return "".join(tokens)


def strip_census_suffix(name: str) -> str:
    lowered = name.lower()
    for suffix in _CENSUS_SUFFIXES:
        if lowered.endswith(" " + suffix):
            return name[: -len(suffix) - 1].strip()
    return name


def census_name_aliases(census_name: str) -> tuple[str, list[str]]:
    """
    Return ``(primary_key, alias_keys)`` for a Census place name.

    Aliases cover consolidated city-counties and similar naming, e.g.
    ``"Nashville-Davidson metropolitan government (balance)"`` -> ``"nashville"``,
    ``"Boise City city"`` -> ``"boise"``, ``"Macon-Bibb County"`` -> ``"macon"``.
    """
    base = strip_census_suffix(census_name)
    primary = normalize_place_name(base)
    aliases: list[str] = [normalize_place_name(census_name)]  # "Carson City" (suffix is part of name)
    if "-" in base:
        aliases.append(normalize_place_name(base.split("-", 1)[0]))
    if base.lower().endswith(" city"):
        aliases.append(normalize_place_name(base[:-5]))
    if base.lower().endswith(" county"):
        aliases.append(normalize_place_name(base[:-7]))
    return primary, list(dict.fromkeys(a for a in aliases if a and a != primary))


@dataclass(frozen=True)
class Place:
    state: str
    name: str
    lat: float
    lon: float
    kind: str  # "place" (incorporated place / CDP) or "cousub" (county subdivision)
    incorporated: bool
    land_sqmi: float
    # Precomputed lookup keys (see census_name_aliases); computed on demand when empty.
    name_key: str = ""
    alias_keys: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}"


@dataclass(frozen=True)
class PlaceMatch:
    place: Place
    method: str  # e.g. "exact_place", "alias_place", "exact_cousub"
    candidates: int


class PlaceIndex:
    """In-memory lookup of US places keyed by (normalized name, state)."""

    def __init__(self, places: list[Place]):
        self.places = places
        self._exact: dict[tuple[str, str], list[Place]] = {}
        self._alias: dict[tuple[str, str], list[Place]] = {}
        for place in places:
            if place.name_key:
                primary, aliases = place.name_key, place.alias_keys
            else:
                primary, aliases = census_name_aliases(place.name)
            self._exact.setdefault((primary, place.state), []).append(place)
            for alias in aliases:
                self._alias.setdefault((alias, place.state), []).append(place)
        rank = lambda p: (p.kind != "place", not p.incorporated, -p.land_sqmi)  # noqa: E731
        for bucket in (*self._exact.values(), *self._alias.values()):
            bucket.sort(key=rank)
        nearest_candidates = [p for p in places if p.kind == "place"]
        self._nearest_places = nearest_candidates
        self._nearest_lats = np.array([p.lat for p in nearest_candidates], dtype=float)
        self._nearest_lons = np.array([p.lon for p in nearest_candidates], dtype=float)

    @classmethod
    def from_csv(cls, path: Path) -> "PlaceIndex":
        places: list[Place] = []
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                places.append(
                    Place(
                        state=row["state"],
                        name=row["name"],
                        lat=float(row["lat"]),
                        lon=float(row["lon"]),
                        kind=row["kind"],
                        incorporated=row["incorporated"] == "1",
                        land_sqmi=float(row["land_sqmi"] or 0),
                        name_key=row.get("name_key", ""),
                        alias_keys=tuple(a for a in row.get("alias_keys", "").split("|") if a),
                    )
                )
        return cls(places)

    def lookup(self, city: str, state: str, *, allow_cousub: bool = True) -> PlaceMatch | None:
        state = (state or "").strip().upper()
        key = normalize_place_name(city)
        if not key or not state:
            return None
        kinds = ("place", "cousub") if allow_cousub else ("place",)
        for kind in kinds:
            for table, method in ((self._exact, "exact"), (self._alias, "alias")):
                bucket = [p for p in table.get((key, state), []) if p.kind == kind]
                if bucket:
                    return PlaceMatch(bucket[0], f"{method}_{kind}", len(bucket))
        return None

    def nearest(self, lat: float, lon: float) -> tuple[Place, float]:
        """Nearest Census place to a coordinate and its distance in miles (vectorised scan)."""
        distances = haversine_miles(lat, lon, self._nearest_lats, self._nearest_lons)
        idx = int(np.argmin(distances))
        return self._nearest_places[idx], float(distances[idx])


_index_lock = threading.Lock()


@lru_cache(maxsize=4)
def _load_index(path_str: str) -> PlaceIndex:
    return PlaceIndex.from_csv(Path(path_str))


def get_place_index(path: Path | None = None) -> PlaceIndex:
    if path is None:
        from django.conf import settings

        path = settings.FUEL_PLANNER["PLACES_REFERENCE_PATH"]
    with _index_lock:
        return _load_index(str(path))


_STATE_TAIL = re.compile(r"^(?P<city>.+?)[\s,]+(?P<state>[A-Za-z][A-Za-z .]*?)(?:[\s,]+\d{5}(?:-\d{4})?)?$")


def parse_city_state(text: str) -> tuple[str, str] | None:
    """
    Parse ``"City, ST"``, ``"City, State Name"``, ``"City ST 12345"`` or
    ``"City, ST, USA"`` into ``(city, state_code)``. Returns ``None`` when the
    text does not end in a recognisable US state.
    """
    value = clean_text(text)
    value = re.sub(r"[,\s]+(usa|us|u\.s\.a\.|u\.s\.|united states(?: of america)?)$", "", value, flags=re.I)
    if "," in value:
        city, _, state = value.rpartition(",")
        state = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", state.strip())
        code = _state_code(state)
        if code and city.strip():
            return clean_text(city), code
    match = _STATE_TAIL.match(value)
    if match:
        city, state = match.group("city"), match.group("state")
        # Try progressively longer state-name tails ("Salt Lake City Utah", "Fargo North Dakota").
        words = value.split()
        for size in (3, 2, 1):
            if len(words) > size:
                code = _state_code(" ".join(words[-size:]))
                if code:
                    return clean_text(" ".join(words[:-size]).rstrip(",")), code
        code = _state_code(state)
        if code:
            return clean_text(city), code
    return None


def _state_code(value: str) -> str | None:
    value = clean_text(value).strip(",")
    if len(value) == 2 and value.upper() in US_STATES:
        return value.upper()
    return STATE_NAME_TO_CODE.get(value.lower().rstrip("."))
