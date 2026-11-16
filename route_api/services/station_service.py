"""
In-memory station index.

All ~6,600 stations are read from the database once per process (a single
query) into numpy arrays; route matching then runs entirely in memory.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass

import numpy as np

from .errors import StationDataUnavailable
from .highways import station_highways


@dataclass(frozen=True)
class StationRecord:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    price: float
    latitude: float
    longitude: float
    geocode_source: str
    highways: frozenset = frozenset()  # e.g. {"I44", "US69"}, parsed from the address


class StationIndex:
    def __init__(self, records: list[StationRecord]):
        if not records:
            raise StationDataUnavailable(
                "No fuel stations are loaded. Run `python manage.py load_stations`."
            )
        self.records = records
        self.lats = np.array([r.latitude for r in records], dtype=float)
        self.lons = np.array([r.longitude for r in records], dtype=float)
        self.prices = np.array([r.price for r in records], dtype=float)
        # Identifies this exact station data (ids, prices, positions): part of the plan cache key,
        # so reloading stations can never serve a plan computed from old data.
        digest = hashlib.sha256(np.array([r.opis_id for r in records], dtype=np.int64).tobytes())
        for arr in (self.prices, self.lats, self.lons):
            digest.update(arr.tobytes())
        self.fingerprint = digest.hexdigest()[:16]

    def __len__(self) -> int:
        return len(self.records)

    @classmethod
    def from_database(cls) -> "StationIndex":
        from route_api.models import FuelStation

        fields = ("opis_id", "name", "address", "city", "state", "price",
                  "latitude", "longitude", "geocode_source")
        rows = FuelStation.objects.order_by("opis_id").values_list(*fields)
        records = [
            StationRecord(opis_id, name, address, city, state, float(price), lat, lon, source,
                          station_highways(address, state))
            for opis_id, name, address, city, state, price, lat, lon, source in rows
        ]
        return cls(records)


_lock = threading.Lock()
_index: StationIndex | None = None


def get_station_index() -> StationIndex:
    """Process-wide cached index (built on first use)."""
    global _index
    with _lock:
        if _index is None:
            _index = StationIndex.from_database()
        return _index


def reset_station_index() -> None:
    """Drop the cached index (used by tests and after reloading stations)."""
    global _index
    with _lock:
        _index = None
