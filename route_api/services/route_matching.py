"""
Match fuel stations to the actual route geometry.

1. Preserve all provider vertices and cumulative distances, scaled to the
   provider's route distance. Regular points only densify long segments.
2. Index every preserved segment's bounding box in a spatial grid.
3. Query the station's conservative corridor box against that grid, including
   long overflow segments. This never discards a segment inside the corridor.
4. Project onto all eligible segments; choose the nearest and use its original
   cumulative distance for the mile marker. Numerical ties choose the earliest.

Station coordinates are city-level approximations (see README), so
``off_route_miles`` measures the distance from the *city point* to the route.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cached_property

import numpy as np

from .geo import EARTH_RADIUS_MILES, haversine_miles
from .station_service import StationIndex, StationRecord

MILES_PER_DEG_LAT = EARTH_RADIUS_MILES * math.pi / 180  # ~69.09
_CHUNK_ELEMENTS = 2_000_000  # station/segment pairs per vectorised block


@dataclass(frozen=True)
class RouteProfile:
    lats: np.ndarray        # original vertices plus optional densification
    lons: np.ndarray
    miles: np.ndarray       # cumulative driving miles at each resampled point
    total_miles: float
    geometry_miles: float   # length of the provider geometry (before scaling)

    @property
    def spacing(self) -> float:
        return float(self.miles[1] - self.miles[0]) if len(self.miles) > 1 else 0.0

    @property
    def scale(self) -> float:
        return self.total_miles / self.geometry_miles if self.geometry_miles else 1.0

    @cached_property
    def segment_grid(self):
        """Index every original segment's bounding box; never sample away bends.

        Long segments use a small overflow list, avoiding unbounded grid growth.
        Querying the station's corridor box is conservative for the same local
        projection used below, so it cannot remove a station inside the corridor.
        """
        cell = 0.25
        cells, overflow = {}, []
        lo_lat = np.floor(np.minimum(self.lats[:-1], self.lats[1:]) / cell).astype(int)
        hi_lat = np.floor(np.maximum(self.lats[:-1], self.lats[1:]) / cell).astype(int)
        lo_lon = np.floor(np.minimum(self.lons[:-1], self.lons[1:]) / cell).astype(int)
        hi_lon = np.floor(np.maximum(self.lons[:-1], self.lons[1:]) / cell).astype(int)
        for segment, (la, lb, xa, xb) in enumerate(zip(lo_lat, hi_lat, lo_lon, hi_lon)):
            if (lb - la + 1) * (xb - xa + 1) > 64:
                overflow.append(segment)
                continue
            for y in range(la, lb + 1):
                for x in range(xa, xb + 1):
                    cells.setdefault((y, x), []).append(segment)
        return cell, cells, overflow


@dataclass(frozen=True)
class CorridorStation:
    station: StationRecord
    mile_marker: float       # driving miles from the start to the route point nearest the station
    off_route_miles: float   # straight-line miles from the station's (city-level) point to the route
    route_lat: float         # that nearest route point
    route_lon: float

    @property
    def price(self) -> float:
        return self.station.price


def build_route_profile(coordinates: np.ndarray, total_miles: float, spacing_miles: float = 2.0) -> RouteProfile:
    """``coordinates``: (N, 2) array of [lat, lon] from the routing provider."""
    coords = np.asarray(coordinates, dtype=float)
    if coords.ndim != 2 or coords.shape[0] < 1 or coords.shape[1] != 2:
        raise ValueError("Route geometry must be an (N, 2) array of [lat, lon].")
    if spacing_miles <= 0:
        raise ValueError("spacing_miles must be positive")

    seg = haversine_miles(coords[:-1, 0], coords[:-1, 1], coords[1:, 0], coords[1:, 1]) if len(coords) > 1 else np.zeros(0)
    keep = np.concatenate(([True], seg > 0))  # drop zero-length segments (duplicate points)
    coords = coords[keep]
    cumulative = np.concatenate(([0.0], np.cumsum(seg[seg > 0])))
    geometry_miles = float(cumulative[-1])

    if geometry_miles == 0 or total_miles <= 0:
        return RouteProfile(coords[:1, 0].copy(), coords[:1, 1].copy(), np.zeros(1), max(total_miles, 0.0), geometry_miles)

    cumulative *= total_miles / geometry_miles  # mile markers consistent with the provider's distance
    n_samples = max(2, int(math.ceil(total_miles / spacing_miles)) + 1)
    # Keep EVERY provider vertex. Regular points only densify long segments;
    # removing a corner here would replace the actual road with a shortcut.
    targets = np.unique(np.concatenate((cumulative, np.linspace(0.0, total_miles, n_samples))))
    lats = np.interp(targets, cumulative, coords[:, 0])
    unwrapped_lons = np.degrees(np.unwrap(np.radians(coords[:, 1])))
    lons = np.interp(targets, cumulative, unwrapped_lons)
    return RouteProfile(lats, lons, targets, float(total_miles), geometry_miles)


def match_stations(profile: RouteProfile, index: StationIndex, corridor_miles: float) -> list[CorridorStation]:
    """Stations whose point lies within ``corridor_miles`` of the route, sorted by mile marker."""
    if corridor_miles < 0:
        raise ValueError("corridor_miles must be >= 0")

    # Shift longitudes to the route's unwrapped interval (Aleutian routes may
    # cross +/-180). All projection and grid coordinates use the same interval.
    center_lon = (profile.lons.min() + profile.lons.max()) / 2
    station_lons = index.lons + 360 * np.rint((center_lon - index.lons) / 360)
    # 1. Conservative bounding-box pre-filter over the preserved route.
    lat_margin = corridor_miles / MILES_PER_DEG_LAT
    max_abs_lat = min(float(np.max(np.abs(profile.lats))) + lat_margin, 89.0)
    lon_margin = corridor_miles / (MILES_PER_DEG_LAT * math.cos(math.radians(max_abs_lat)))
    in_box = (
        (index.lats >= profile.lats.min() - lat_margin) & (index.lats <= profile.lats.max() + lat_margin)
        & (station_lons >= profile.lons.min() - lon_margin) & (station_lons <= profile.lons.max() + lon_margin)
    )
    candidates = np.flatnonzero(in_box)
    if candidates.size == 0:
        return []
    if len(profile.lats) == 1:
        off = haversine_miles(index.lats[candidates], station_lons[candidates], profile.lats[0], profile.lons[0])
        results = [CorridorStation(index.records[i], 0.0, float(o), float(profile.lats[0]),
                                   float((profile.lons[0] + 180) % 360 - 180))
                   for i, o in zip(candidates, off) if o <= corridor_miles]
        results.sort(key=lambda c: (c.mile_marker, c.price, c.station.opis_id))
        return results

    cell, cells, overflow = profile.segment_grid
    pair_stations, pair_segments = [], []
    for i in candidates:
        lat, lon = index.lats[i], station_lons[i]
        dy = corridor_miles / MILES_PER_DEG_LAT + 1e-10
        dx = corridor_miles / (MILES_PER_DEG_LAT * max(math.cos(math.radians(lat)), 1e-10)) + 1e-10
        found = set(overflow)
        for y in range(math.floor((lat - dy) / cell), math.floor((lat + dy) / cell) + 1):
            for x in range(math.floor((lon - dx) / cell), math.floor((lon + dx) / cell) + 1):
                found.update(cells.get((y, x), ()))
        pair_stations.extend([int(i)] * len(found))
        pair_segments.extend(found)
    if not pair_stations:
        return []
    pair_stations = np.asarray(pair_stations, dtype=np.int64)
    pair_segments = np.asarray(pair_segments, dtype=np.int64)
    best = {}
    for start in range(0, len(pair_stations), _CHUNK_ELEMENTS):
        ids = pair_stations[start:start + _CHUNK_ELEMENTS]
        segments = pair_segments[start:start + _CHUNK_ELEMENTS]
        off, miles, plat, plon = _project_segment_pairs(index.lats[ids], station_lons[ids], segments, profile)
        keep = off <= corridor_miles
        ids, off, miles, plat, plon = ids[keep], off[keep], miles[keep], plat[keep], plon[keep]
        # Deterministic ties choose the earliest point on the route.
        order = np.lexsort((miles, off, ids))
        ordered_ids = ids[order]
        unique = np.concatenate(([True], ordered_ids[1:] != ordered_ids[:-1])) if len(order) else []
        for k in order[unique]:
            i = int(ids[k])
            value = (float(off[k]), float(miles[k]), float(plat[k]), float(plon[k]))
            if i not in best or value[:2] < best[i][:2]:
                best[i] = value
    results = [CorridorStation(index.records[i], m, o, la, (lo + 180) % 360 - 180)
               for i, (o, m, la, lo) in best.items()]
    results.sort(key=lambda c: (c.mile_marker, c.price, c.station.opis_id))
    return results


def _project_segment_pairs(st_lat, st_lon, a, profile):
    """Vectorized clamped projection onto every spatially eligible segment."""
    b = a + 1
    lat_a, lon_a, lat_b, lon_b = profile.lats[a], profile.lons[a], profile.lats[b], profile.lons[b]
    kx = MILES_PER_DEG_LAT * np.cos(np.radians(st_lat))
    ax, ay = (lon_a - st_lon) * kx, (lat_a - st_lat) * MILES_PER_DEG_LAT
    bx, by = (lon_b - st_lon) * kx, (lat_b - st_lat) * MILES_PER_DEG_LAT
    vx, vy = bx - ax, by - ay
    length2 = vx * vx + vy * vy
    with np.errstate(invalid="ignore", divide="ignore"):
        t = np.where(length2 > 0, -(ax * vx + ay * vy) / length2, 0.0)
    t = np.clip(t, 0.0, 1.0)
    off = np.hypot(ax + t * vx, ay + t * vy)
    miles = profile.miles[a] + t * (profile.miles[b] - profile.miles[a])
    return off, miles, lat_a + t * (lat_b - lat_a), lon_a + t * (lon_b - lon_a)
