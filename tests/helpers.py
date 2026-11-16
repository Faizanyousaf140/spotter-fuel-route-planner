"""Shared test helpers: fake HTTP session, synthetic stations and routes."""

from __future__ import annotations

import math

import numpy as np
import requests

from route_api.services.geo import EARTH_RADIUS_MILES, encode_polyline
from route_api.services.station_service import StationIndex, StationRecord

LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "tests"}}
MILES_PER_DEG = EARTH_RADIUS_MILES * math.pi / 180


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text_body=None):
        self.status_code = status_code
        self._payload = payload
        self._text = text_body

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Stands in for ``requests.Session``; returns queued responses or raises queued exceptions."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


def osrm_ok(coords_latlon, distance_m=100_000.0, duration_s=3600.0, steps=None):
    """A successful OSRM /route response in the format we request (polyline6 + steps).

    ``steps``: optional list of (distance_m, ref) tuples; defaults to one unnamed step.
    """
    steps = steps if steps is not None else [(distance_m, "")]
    return FakeResponse(200, {
        "code": "Ok",
        "routes": [{
            "distance": distance_m, "duration": duration_s,
            "geometry": encode_polyline(list(coords_latlon), precision=6),
            "legs": [{"steps": [{"distance": d, "ref": ref, "name": ""} for d, ref in steps]}],
        }],
        "waypoints": [],
    })


def meridian_route(lat_from=30.0, lat_to=40.0, lon=-100.0, points=101) -> np.ndarray:
    """Straight north-bound route along a meridian: easy to reason about distances."""
    return np.column_stack((np.linspace(lat_from, lat_to, points), np.full(points, lon)))


def lon_offset_for_miles(miles: float, lat: float) -> float:
    return miles / (MILES_PER_DEG * math.cos(math.radians(lat)))


def station(opis_id, lat, lon, price=3.0, name=None, city="Testville", state="TX") -> StationRecord:
    return StationRecord(opis_id, name or f"STATION {opis_id}", "I-1", city, state, price, lat, lon, "census_exact_place")


def index_of(*records) -> StationIndex:
    return StationIndex(list(records))


requests_timeout = requests.Timeout("timed out")
requests_conn_error = requests.ConnectionError("connection refused")
