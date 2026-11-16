"""
Match fuel stations to the actual route geometry.

1. Route profile: cumulative driving distance along the full-resolution route
   geometry, scaled so the total equals the provider's route distance, then
   resampled to one point every ``spacing`` miles (default 2).
2. Rough filters: (a) the route's bounding box expanded by the corridor;
   (b) a grid whose cells are at least (corridor + sample spacing) wide: only
   stations in a cell next to a cell containing a route sample can be within
   the corridor. Both are exact pre-filters (no in-corridor station is lost).
3. Nearest route sample for each remaining station (vectorised, chunked).
4. Refinement: project the station onto the two route segments adjacent to
   that sample to get its perpendicular distance from the route
   (``off_route_miles``) and its position along the route (``mile_marker``).
5. Keep stations with ``off_route_miles <= corridor``.

Station coordinates are city-level approximations (see README), so
``off_route_miles`` measures the distance from the *city point* to the route.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .geo import EARTH_RADIUS_MILES, haversine_miles
from .station_service import StationIndex, StationRecord

MILES_PER_DEG_LAT = EARTH_RADIUS_MILES * math.pi / 180  # ~69.09
_CHUNK_ELEMENTS = 2_000_000  # stations x samples per vectorised block


@dataclass(frozen=True)
class RouteProfile:
    lats: np.ndarray        # resampled route points
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
    targets = np.linspace(0.0, total_miles, n_samples)
    lats = np.interp(targets, cumulative, coords[:, 0])
    lons = np.interp(targets, cumulative, coords[:, 1])
    return RouteProfile(lats, lons, targets, float(total_miles), geometry_miles)


def match_stations(profile: RouteProfile, index: StationIndex, corridor_miles: float) -> list[CorridorStation]:
    """Stations whose point lies within ``corridor_miles`` of the route, sorted by mile marker."""
    if corridor_miles < 0:
        raise ValueError("corridor_miles must be >= 0")

    # 1. bounding-box pre-filter
    lat_margin = corridor_miles / MILES_PER_DEG_LAT
    max_abs_lat = min(float(np.max(np.abs(profile.lats))) + lat_margin, 89.0)
    lon_margin = corridor_miles / (MILES_PER_DEG_LAT * math.cos(math.radians(max_abs_lat)))
    in_box = (
        (index.lats >= profile.lats.min() - lat_margin) & (index.lats <= profile.lats.max() + lat_margin)
        & (index.lons >= profile.lons.min() - lon_margin) & (index.lons <= profile.lons.max() + lon_margin)
    )
    candidates = np.flatnonzero(in_box)
    if candidates.size == 0:
        return []
    candidates = _grid_prefilter(candidates, index, profile, corridor_miles, max_abs_lat)
    if candidates.size == 0:
        return []

    # 2. nearest resampled route point (equirectangular distance, chunked)
    s_lat, s_lon = profile.lats, profile.lons
    cos_s = np.cos(np.radians(s_lat))
    nearest = np.empty(candidates.size, dtype=np.int64)
    rows_per_chunk = max(1, _CHUNK_ELEMENTS // len(s_lat))
    for start in range(0, candidates.size, rows_per_chunk):
        idx = candidates[start : start + rows_per_chunk]
        dy = index.lats[idx, None] - s_lat[None, :]
        dx = (index.lons[idx, None] - s_lon[None, :]) * cos_s[None, :]
        nearest[start : start + len(idx)] = np.argmin(dx * dx + dy * dy, axis=1)

    # 3. refine against the two adjacent segments
    off, miles, plat, plon = _project_to_adjacent_segments(
        index.lats[candidates], index.lons[candidates], nearest, profile
    )

    keep = off <= corridor_miles
    results = [
        CorridorStation(index.records[i], float(m), float(o), float(la), float(lo))
        for i, m, o, la, lo in zip(candidates[keep], miles[keep], off[keep], plat[keep], plon[keep])
    ]
    results.sort(key=lambda c: (c.mile_marker, c.price, c.station.opis_id))
    return results


def _grid_prefilter(candidates, index, profile: RouteProfile, corridor_miles: float, max_abs_lat: float):
    """Keep stations whose grid cell touches a cell containing a route sample."""
    reach = corridor_miles + profile.spacing + 1e-6  # station -> nearest sample is at most this far
    cell_lat = reach / MILES_PER_DEG_LAT
    cell_lon = reach / (MILES_PER_DEG_LAT * math.cos(math.radians(max_abs_lat)))

    def keys(lats, lons):
        return np.floor(lats / cell_lat).astype(np.int64) * 1_000_003 + np.floor(lons / cell_lon).astype(np.int64)

    sample_lat_idx = np.floor(profile.lats / cell_lat).astype(np.int64)
    sample_lon_idx = np.floor(profile.lons / cell_lon).astype(np.int64)
    dilated = np.unique(np.concatenate([
        (sample_lat_idx + dy) * 1_000_003 + (sample_lon_idx + dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)
    ]))
    mask = np.isin(keys(index.lats[candidates], index.lons[candidates]), dilated)
    return candidates[mask]


def _project_to_adjacent_segments(st_lat, st_lon, nearest, profile: RouteProfile):
    """Perpendicular projection onto segments (k-1, k) and (k, k+1) in a local plane (miles)."""
    n = len(profile.lats)
    if n == 1:
        off = haversine_miles(st_lat, st_lon, profile.lats[0], profile.lons[0])
        zeros = np.zeros_like(st_lat)
        return off, zeros, zeros + profile.lats[0], zeros + profile.lons[0]

    best_off = np.full(st_lat.shape, np.inf)
    best_mile = np.zeros(st_lat.shape)
    best_lat = np.zeros(st_lat.shape)
    best_lon = np.zeros(st_lat.shape)
    for a in (np.clip(nearest - 1, 0, n - 2), np.clip(nearest, 0, n - 2)):
        b = a + 1
        lat_a, lon_a, lat_b, lon_b = profile.lats[a], profile.lons[a], profile.lats[b], profile.lons[b]
        kx = MILES_PER_DEG_LAT * np.cos(np.radians(st_lat))  # local equirectangular scale at the station
        ax, ay = (lon_a - st_lon) * kx, (lat_a - st_lat) * MILES_PER_DEG_LAT
        bx, by = (lon_b - st_lon) * kx, (lat_b - st_lat) * MILES_PER_DEG_LAT
        vx, vy = bx - ax, by - ay
        seg_len2 = vx * vx + vy * vy
        with np.errstate(invalid="ignore", divide="ignore"):
            t = np.where(seg_len2 > 0, -(ax * vx + ay * vy) / seg_len2, 0.0)
        t = np.clip(t, 0.0, 1.0)
        px, py = ax + t * vx, ay + t * vy  # projected point relative to the station
        off = np.hypot(px, py)
        better = off < best_off
        best_off = np.where(better, off, best_off)
        best_mile = np.where(better, profile.miles[a] + t * (profile.miles[b] - profile.miles[a]), best_mile)
        best_lat = np.where(better, lat_a + t * (lat_b - lat_a), best_lat)
        best_lon = np.where(better, lon_a + t * (lon_b - lon_a), best_lon)
    return best_off, best_mile, best_lat, best_lon
