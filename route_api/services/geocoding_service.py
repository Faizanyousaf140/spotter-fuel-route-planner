"""
Resolve a start/finish input to coordinates inside the USA.

Order (stops at the first success):
  1. ``"lat,lng"``             -> validated, checked to be in the USA (offline)
  2. ``"City, ST"``            -> offline Census gazetteer (no network)
  3. anything else / not found -> Nominatim fallback (if enabled; cached 30 days)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from . import nominatim
from .errors import GeocoderUnavailable, LocationNotFound, LocationOutsideUSA
from .gazetteer import CANADIAN_PROVINCES, US_STATES, get_place_index, parse_city_state
from .geo import is_valid_coordinate

# A coordinate is accepted as "in the USA" when a US Census place lies within
# this distance. Rejects oceans, Canada/Mexico interiors; border towns within a
# few miles of the US can pass (documented limitation).
MAX_MILES_TO_US_PLACE = 25.0

_COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_FOREIGN_SUFFIX = re.compile(
    r"(,|\s)\s*(canada|m.xico|" + "|".join(sorted(CANADIAN_PROVINCES)) + r")\.?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ResolvedLocation:
    query: str
    label: str
    lat: float
    lon: float
    source: str  # "coordinates" | "census_gazetteer" | "nominatim"
    external_calls: int = 0

    @property
    def point(self) -> tuple[float, float]:
        return (self.lat, self.lon)

    def as_dict(self) -> dict:
        return {"query": self.query, "label": self.label, "lat": round(self.lat, 6),
                "lon": round(self.lon, 6), "resolved_by": self.source}


def parse_coordinates(text: str) -> tuple[float, float] | None:
    match = _COORD_RE.match(text or "")
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def resolve_location(value, field_name: str = "location") -> ResolvedLocation:
    """``value`` is a string or a ``{"lat": .., "lon"/"lng": ..}`` mapping."""
    if isinstance(value, dict):
        lat = value.get("lat")
        lon = value.get("lon", value.get("lng"))
        return _from_coordinates(f"{lat},{lon}", lat, lon, field_name)

    text = str(value).strip()
    coords = parse_coordinates(text)
    if coords:
        return _from_coordinates(text, coords[0], coords[1], field_name)

    if _FOREIGN_SUFFIX.search(text):
        raise LocationOutsideUSA(f"{field_name} '{text}' is outside the USA. Only US locations are supported.")

    parsed = parse_city_state(text)
    if parsed:
        match = get_place_index().lookup(*parsed)
        if match:
            return ResolvedLocation(text, f"{parsed[0]}, {parsed[1]}", match.place.lat, match.place.lon,
                                    "census_gazetteer")

    return _from_fallback(text, field_name)


def _from_coordinates(text: str, lat, lon, field_name: str) -> ResolvedLocation:
    if not is_valid_coordinate(lat, lon):
        raise LocationNotFound(f"{field_name} has invalid coordinates: '{text}'. Use 'lat,lng' in decimal degrees.")
    lat, lon = float(lat), float(lon)
    place, miles = get_place_index().nearest(lat, lon)
    if miles > MAX_MILES_TO_US_PLACE:
        raise LocationOutsideUSA(
            f"{field_name} ({lat:.5f}, {lon:.5f}) does not appear to be in the USA "
            f"(nearest US place is {place.label}, {miles:.0f} miles away). "
            "Check the order: coordinates must be 'lat,lng'."
        )
    return ResolvedLocation(text, f"{lat:.5f},{lon:.5f} (near {place.label})", lat, lon, "coordinates")


def _from_fallback(text: str, field_name: str) -> ResolvedLocation:
    cfg = settings.FUEL_PLANNER
    if not cfg["GEOCODER_FALLBACK_ENABLED"]:
        raise LocationNotFound(
            f"Could not resolve {field_name} '{text}'. Use 'City, ST' (e.g. 'Dallas, TX') or 'lat,lng'."
        )
    key = "geocode:v1:" + hashlib.sha256(text.lower().encode()).hexdigest()[:32]
    cached = cache.get(key)
    calls = 0
    if cached is None:
        client = nominatim.NominatimClient(cfg["NOMINATIM_BASE_URL"], cfg["HTTP_USER_AGENT"], cfg["GEOCODER_TIMEOUT"])
        try:
            result = client.geocode_free_text(text)
        except nominatim.GeocoderUnavailable as exc:
            raise GeocoderUnavailable(f"Could not resolve {field_name} '{text}': geocoding service unavailable.") from exc
        calls = 1
        cached = (
            {"lat": result.lat, "lon": result.lon, "label": result.display_name, "state": result.state_code}
            if result else {}
        )
        cache.set(key, cached, timeout=cfg["GEOCODE_CACHE_SECONDS"])
    if not cached:
        raise LocationNotFound(
            f"Could not find {field_name} '{text}' in the USA. Use 'City, ST' (e.g. 'Dallas, TX') or 'lat,lng'."
        )
    if cached.get("state") not in US_STATES:
        raise LocationOutsideUSA(f"{field_name} '{text}' resolved outside the 50 US states/DC.")
    return ResolvedLocation(text, cached["label"], cached["lat"], cached["lon"], "nominatim", calls)
