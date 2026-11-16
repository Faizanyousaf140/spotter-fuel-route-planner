"""Small, dependency-light geometry helpers (all distances in statute miles)."""

from __future__ import annotations

import math

import numpy as np

EARTH_RADIUS_MILES = 3958.7613
METERS_PER_MILE = 1609.344


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance. Works on scalars or numpy arrays."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def is_valid_coordinate(lat, lon) -> bool:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180


# --------------------------------------------------------------------------
# Google encoded polyline (precision 5) -- used by OSRM and for responses.
# --------------------------------------------------------------------------
def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode an encoded polyline into ``[(lat, lon), ...]``."""
    coords: list[tuple[float, float]] = []
    index = lat = lon = 0
    factor = 10**precision
    length = len(encoded)
    while index < length:
        for is_lon in (False, True):
            shift = result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lon:
                lon += delta
            else:
                lat += delta
        coords.append((lat / factor, lon / factor))
    return coords


def decode_polyline_array(encoded: str, precision: int = 5) -> np.ndarray:
    """
    Vectorised polyline decoder returning an (N, 2) array of [lat, lon].

    Same output as ``decode_polyline`` but ~50x faster on long routes (a
    coast-to-coast OSRM geometry has ~35,000 points). Raises ValueError on
    malformed input.
    """
    if not encoded:
        return np.zeros((0, 2))
    try:
        raw = np.frombuffer(encoded.encode("ascii"), dtype=np.uint8).astype(np.int64) - 63
    except UnicodeEncodeError as exc:
        raise ValueError("polyline contains non-ASCII characters") from exc
    if raw.min() < 0 or raw.max() > 63:
        raise ValueError("polyline contains characters outside the encoding alphabet")
    ends = np.flatnonzero(raw < 0x20)          # last chunk of every varint
    if ends.size == 0 or ends[-1] != raw.size - 1 or ends.size % 2:
        raise ValueError("truncated polyline")
    starts = np.concatenate(([0], ends[:-1] + 1))
    lengths = ends - starts + 1
    if lengths.max() > 12:
        raise ValueError("polyline value too long")
    chunk_index = np.arange(raw.size) - np.repeat(starts, lengths)
    values = np.add.reduceat((raw & 0x1F) << (5 * chunk_index), starts)
    deltas = np.where(values & 1, ~(values >> 1), values >> 1)
    return np.cumsum(deltas.reshape(-1, 2), axis=0) / float(10**precision)


def encode_polyline(coords, precision: int = 5) -> str:
    """Encode ``[(lat, lon), ...]`` as a Google encoded polyline."""
    factor = 10**precision
    out: list[str] = []
    prev_lat = prev_lon = 0
    for lat, lon in coords:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for delta in (ilat - prev_lat, ilon - prev_lon):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


def simplify_polyline(coords: np.ndarray, tolerance_deg: float) -> np.ndarray:
    """
    Ramer-Douglas-Peucker simplification (iterative, planar in degrees).

    Only used to shrink the geometry returned to clients; all distance and
    station-matching work uses the full-resolution route.
    """
    n = len(coords)
    if n <= 2:
        return coords
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        segment = coords[start + 1 : end]
        a, b = coords[start], coords[end]
        ab = b - a
        norm = math.hypot(ab[0], ab[1])
        if norm == 0:
            dists = np.hypot(segment[:, 0] - a[0], segment[:, 1] - a[1])
        else:
            dists = np.abs(ab[0] * (segment[:, 1] - a[1]) - ab[1] * (segment[:, 0] - a[0])) / norm
        idx = int(np.argmax(dists))
        if dists[idx] > tolerance_deg:
            split = start + 1 + idx
            keep[split] = True
            stack.append((start, split))
            stack.append((split, end))
    return coords[keep]
