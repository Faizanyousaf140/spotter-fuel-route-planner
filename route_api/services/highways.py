"""
Highway matching: does a station's address name a highway the route actually uses?

The price file has no coordinates, but its addresses describe where the stop is
("I-44, EXIT 283 & US-69"). The routing provider returns the highway refs of the
route ("I 44", "US 69; US 75"). When a station's address names a highway the
route drives within a few miles of the station's mile marker, the stop is very
likely at an exit on the route. This raises confidence; it does NOT give exact
coordinates and is not used to shrink the estimated detour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Interstates, US routes and state routes as written in the OPIS addresses.
_ADDRESS_HIGHWAY = re.compile(r"\b(I|US|SR|SH|STATE\s+ROUTE)\s*-?\s*(\d{1,4})\b", re.IGNORECASE)
# One ref token from OSM/OSRM, e.g. "I 80", "US 6", "PA 28 Truck", "NJ 139".
_ROUTE_REF = re.compile(r"^\s*([A-Z]{1,3})\s*-?\s*(\d{1,4})\b")

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_RANK = {CONFIDENCE_LOW: 0, CONFIDENCE_MEDIUM: 1, CONFIDENCE_HIGH: 2}
NEAR_ROUTE_MILES = 3.0          # city point this close to the route -> medium confidence
MATCH_WINDOW_MILES = 5.0        # extra mile-marker tolerance when looking for the highway


def station_highways(address: str, state: str) -> frozenset[str]:
    """``"I-44, EXIT 283 & US-69"`` -> {"I44", "US69"}; ``"SR-85"`` in AZ -> {"AZ85"}."""
    found = set()
    for prefix, number in _ADDRESS_HIGHWAY.findall(address or ""):
        prefix = prefix.upper()
        number = number.lstrip("0") or "0"
        if prefix == "I":
            found.add(f"I{number}")
        elif prefix == "US":
            found.add(f"US{number}")
        else:  # state route: OSM refs use the state's postal code ("AZ 85")
            found.add(f"{state.upper()}{number}")
    return frozenset(found)


def route_ref_tokens(ref: str) -> frozenset[str]:
    """``"I 80; I 94; US 6"`` -> {"I80", "I94", "US6"}."""
    tokens = set()
    for part in (ref or "").upper().split(";"):
        match = _ROUTE_REF.match(part)
        if match:
            tokens.add(f"{match.group(1)}{match.group(2).lstrip('0') or '0'}")
    return frozenset(tokens)


@dataclass(frozen=True)
class RoadSegment:
    start_mile: float
    end_mile: float
    refs: frozenset[str]


@dataclass(frozen=True)
class LocationConfidence:
    level: str
    matched_highways: tuple[str, ...]
    reason: str


def assess_location(highways: frozenset[str], mile_marker: float, off_route_miles: float,
                    road_segments: list[RoadSegment]) -> LocationConfidence:
    window = off_route_miles + MATCH_WINDOW_MILES
    lo, hi = mile_marker - window, mile_marker + window
    matched = set()
    if highways:
        for seg in road_segments:
            if seg.end_mile >= lo and seg.start_mile <= hi:
                matched |= highways & seg.refs
    if matched:
        names = tuple(sorted(matched))
        return LocationConfidence(
            CONFIDENCE_HIGH, names,
            f"Address names {', '.join(names)}, which the route uses near mile {mile_marker:.0f}: "
            "the stop is very likely at an exit on the route.")
    if off_route_miles <= NEAR_ROUTE_MILES:
        return LocationConfidence(
            CONFIDENCE_MEDIUM, (),
            f"The station's city point is {off_route_miles:.1f} mi from the route, but its address highway "
            "could not be matched to the route.")
    return LocationConfidence(
        CONFIDENCE_LOW, (),
        f"The station's city point is {off_route_miles:.1f} mi from the route and its address highway is not "
        "on the route nearby: the real detour may be longer than estimated.")
