"""
Minimal Nominatim (OpenStreetMap) client.

Usage policy (https://operations.osmfoundation.org/policies/nominatim/):
max 1 request/second, single thread, identifying User-Agent, results cached
by the caller. This client enforces the 1 req/s spacing per process.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

PLACE_CATEGORIES = {"place", "boundary"}


class GeocoderUnavailable(Exception):
    """Network error, timeout or non-200 response from the geocoder."""


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lon: float
    display_name: str
    state_code: str | None  # e.g. "TX" (from ISO3166-2-lvl4 "US-TX")
    category: str
    place_type: str


class NominatimClient:
    MIN_INTERVAL_SECONDS = 1.1

    _lock = threading.Lock()
    _last_request_at = 0.0

    def __init__(self, base_url: str, user_agent: str, timeout: float = 8.0, session=None):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.timeout = timeout
        self.session = session or requests.Session()

    def _get(self, params: dict) -> list[dict]:
        params = {**params, "format": "jsonv2", "addressdetails": 1, "countrycodes": "us", "limit": 1}
        with NominatimClient._lock:  # single in-flight request + spacing per process
            wait = NominatimClient._last_request_at + self.MIN_INTERVAL_SECONDS - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                response = self.session.get(
                    f"{self.base_url}/search",
                    params=params,
                    headers={"User-Agent": self.user_agent},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise GeocoderUnavailable(f"Geocoder request failed: {exc}") from exc
            finally:
                NominatimClient._last_request_at = time.monotonic()
        if response.status_code != 200:
            raise GeocoderUnavailable(f"Geocoder returned HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise GeocoderUnavailable("Geocoder returned invalid JSON") from exc

    @staticmethod
    def _to_result(item: dict) -> GeocodeResult:
        iso = (item.get("address") or {}).get("ISO3166-2-lvl4", "")
        return GeocodeResult(
            lat=float(item["lat"]),
            lon=float(item["lon"]),
            display_name=item.get("display_name", ""),
            state_code=iso[3:] if iso.startswith("US-") else None,
            category=item.get("category", ""),
            place_type=item.get("addresstype") or item.get("type", ""),
        )

    def geocode_city(self, city: str, state: str) -> GeocodeResult | None:
        """Structured city/state query. Returns None when nothing matches."""
        items = self._get({"city": city, "state": state, "country": "United States"})
        if not items:
            return None
        result = self._to_result(items[0])
        if result.category not in PLACE_CATEGORIES:
            logger.info("Nominatim result for %s, %s is %s/%s; rejected", city, state, result.category, result.place_type)
            return None
        return result

    def geocode_free_text(self, query: str) -> GeocodeResult | None:
        items = self._get({"q": query})
        return self._to_result(items[0]) if items else None
