"""Offline point-in-state polygons, including holes and Aleutian longitudes.

The committed Census 1:500,000 artifact covers 50 states and DC. Ring edges
are included with numerical tolerance only: there is no permissive mile buffer.
"""
from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path

import numpy as np

from .geo import is_valid_coordinate

BOUNDARY_VERSION = "census-cb2025-500k-r1"
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data/reference/us_boundaries.json.gz"
EPS = 1e-10


class BoundaryIndex:
    def __init__(self, states):
        self.states = {}
        for state, rings in states.items():
            prepared = []
            for ring in rings:
                points = np.asarray(ring, dtype=float)
                x = np.degrees(np.unwrap(np.radians(points[:, 0])))
                y = points[:, 1]
                prepared.append((x, y, (x.min(), y.min(), x.max(), y.max())))
            self.states[state] = prepared

    def state_at(self, lat, lon):
        if not is_valid_coordinate(lat, lon):
            return None
        lat, lon = float(lat), float(lon)
        for state, rings in self.states.items():
            inside = False
            for x, y, (xmin, ymin, xmax, ymax) in rings:
                if lat < ymin - EPS or lat > ymax + EPS:
                    continue
                qx = lon + 360 * round(((xmin + xmax) / 2 - lon) / 360)
                if qx < xmin - EPS or qx > xmax + EPS:
                    continue
                x1, y1, x2, y2 = x[:-1], y[:-1], x[1:], y[1:]
                dx, dy = x2 - x1, y2 - y1
                length = np.hypot(dx, dy)
                on_line = np.abs((qx - x1) * dy - (lat - y1) * dx) <= EPS * np.maximum(length, EPS)
                on_segment = ((qx >= np.minimum(x1, x2) - EPS) & (qx <= np.maximum(x1, x2) + EPS)
                              & (lat >= np.minimum(y1, y2) - EPS) & (lat <= np.maximum(y1, y2) + EPS))
                if np.any(on_line & on_segment):
                    return state
                crossing = (y1 > lat) != (y2 > lat)
                crossing_x = x1[crossing] + (lat - y1[crossing]) * dx[crossing] / dy[crossing]
                if np.count_nonzero(qx < crossing_x) % 2:
                    inside = not inside
            if inside:
                return state
        return None


@lru_cache(maxsize=1)
def get_boundary_index():
    with gzip.open(DEFAULT_PATH, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    if data["version"] != BOUNDARY_VERSION:
        raise ValueError("US boundary data version does not match the validator")
    return BoundaryIndex(data["states"])
