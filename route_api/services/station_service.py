"""
In-memory station index.

All ~6,600 stations are kept in numpy arrays. A small database revision
check on each access detects committed changes made by any process.
Transaction-local snapshots never enter the shared process cache.
"""

from __future__ import annotations

import hashlib
import json
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
    def __init__(self, records: list[StationRecord], *, revision=None):
        if not records:
            raise StationDataUnavailable(
                "No fuel stations are loaded. Run `python manage.py load_stations`."
            )
        self.records = records
        self.revision = revision
        self.lats = np.array([r.latitude for r in records], dtype=float)
        self.lons = np.array([r.longitude for r in records], dtype=float)
        self.prices = np.array([r.price for r in records], dtype=float)
        # Canonical snapshot includes every field used by matching, confidence,
        # optimization or output. A renamed station must invalidate saved cache
        # content even when its coordinates and price did not change.
        snapshot = [[r.opis_id, r.name, r.address, r.city, r.state, r.price,
                     r.latitude, r.longitude, r.geocode_source, sorted(r.highways)]
                    for r in sorted(records, key=lambda r: r.opis_id)]
        self.fingerprint = hashlib.sha256(json.dumps(snapshot, separators=(",", ":"),
                                                     ensure_ascii=False).encode()).hexdigest()[:24]

    def __len__(self) -> int:
        return len(self.records)

    @classmethod
    def from_database(cls) -> "StationIndex":
        from route_api.models import FuelStation

        fields = ("opis_id", "name", "address", "city", "state", "price",
                  "latitude", "longitude", "geocode_source")
        for _ in range(3):
            before = _database_revision()
            rows = list(FuelStation.objects.order_by("opis_id").values_list(*fields))
            after = _database_revision()
            if before == after:
                records = [
                    StationRecord(opis_id, name, address, city, state, float(price), lat, lon, source,
                                  station_highways(address, state))
                    for opis_id, name, address, city, state, price, lat, lon, source in rows
                ]
                return cls(records, revision=after)
        raise StationDataUnavailable("Station data changed repeatedly while loading; retry shortly.")


_lock = threading.Lock()
_index: StationIndex | None = None


def _database_revision():
    from route_api.models import StationDatasetRevision

    revision = StationDatasetRevision.objects.filter(pk=1).values_list("generation", flat=True).first()
    if revision is None:
        # The row is restored lazily after a test flush or an empty fresh DB.
        revision = StationDatasetRevision.objects.get_or_create(pk=1)[0].generation
    return revision


def get_station_index() -> StationIndex:
    """One small revision query per access; rebuild after another process writes."""
    global _index
    from django.db import connection

    # Never publish a transaction's uncommitted rows globally. A rollback can
    # reuse an integer revision, and another connection must not see its data.
    if connection.in_atomic_block:
        return StationIndex.from_database()
    with _lock:
        revision = _database_revision()
        if _index is None or _index.revision != revision:
            _index = StationIndex.from_database()
        return _index


def reset_station_index() -> None:
    """Drop the cached index (used by tests and after reloading stations)."""
    global _index
    with _lock:
        _index = None
