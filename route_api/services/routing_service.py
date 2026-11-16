"""
Routing: one provider call per uncached start/finish pair.

Default provider is the public OSRM demo server (no API key; max 1 req/s,
non-commercial, no SLA). OpenRouteService can be selected with
``ROUTING_PROVIDER=openrouteservice`` and ``ORS_API_KEY``.

Results are cached in Django's cache (database-backed by default) keyed by the
rounded coordinates, so repeated requests make zero routing calls. Concurrent
identical requests in one process share a single provider call ("single
flight"), and a corrupted cache entry is treated as a miss.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import zlib
from dataclasses import dataclass, field

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache

from .errors import RouteNotFound, RoutingProviderError, RoutingTimeout, RoutingUnavailable
from .geo import METERS_PER_MILE, decode_polyline_array
from .highways import RoadSegment, route_ref_tokens
from .http_utils import DeadlineExceeded, ResponseTooLarge, read_with_deadline

logger = logging.getLogger(__name__)

CACHE_VERSION = "v3"
CACHE_PRECISION = 5  # ~1 m; coordinates are rounded to this many decimals for the cache key
MAX_RESPONSE_BYTES = 20 * 1024 * 1024  # a coast-to-coast OSRM answer is ~0.6 MB


@dataclass
class RouteResult:
    distance_miles: float
    duration_seconds: float | None
    coordinates: np.ndarray  # shape (N, 2): [lat, lon]
    provider: str
    from_cache: bool = False
    provider_calls: int = 0  # external routing calls made for this result (0 if cached)
    provider_ms: float = 0.0
    # Highway refs along the route (from OSRM steps), used for location confidence.
    road_segments: list[RoadSegment] = field(default_factory=list)


class RoutingProvider:
    name = "base"

    # Process-wide spacing between provider calls (OSRM demo server: max 1 req/s).
    _spacing_lock = threading.Lock()
    _last_call_at = 0.0

    def __init__(self, cfg: dict, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.timeout = (cfg["ROUTING_CONNECT_TIMEOUT"], cfg["ROUTING_READ_TIMEOUT"])
        self.total_timeout = float(cfg.get("ROUTING_TOTAL_TIMEOUT") or 30.0)
        self.headers = {"User-Agent": cfg["HTTP_USER_AGENT"]}

    def route(self, start: tuple[float, float], finish: tuple[float, float]) -> RouteResult:
        raise NotImplementedError

    def _respect_min_interval(self) -> None:
        interval = float(self.cfg.get("ROUTING_MIN_INTERVAL_SECONDS") or 0)
        if interval <= 0:
            return
        with RoutingProvider._spacing_lock:
            wait = RoutingProvider._last_call_at + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            RoutingProvider._last_call_at = time.monotonic()

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        self._respect_min_interval()
        deadline = time.monotonic() + self.total_timeout
        try:
            headers = self.headers | kwargs.pop("headers", {})
            response = self.session.request(method, url, timeout=self.timeout, headers=headers, stream=True, **kwargs)
            return read_with_deadline(response, deadline, MAX_RESPONSE_BYTES)
        except DeadlineExceeded as exc:
            raise RoutingTimeout(
                f"Routing provider ({self.name}) did not finish responding within {self.total_timeout:.0f} s.") from exc
        except ResponseTooLarge as exc:
            raise RoutingProviderError(f"Routing provider ({self.name}) returned an oversized response.") from exc
        except requests.Timeout as exc:
            raise RoutingTimeout(f"Routing provider ({self.name}) timed out.") from exc
        except requests.RequestException as exc:
            raise RoutingUnavailable(f"Routing provider ({self.name}) is unreachable.") from exc

    @staticmethod
    def _json(response: requests.Response, provider: str) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise RoutingProviderError(
                f"Routing provider ({provider}) returned a non-JSON response (HTTP {response.status_code})."
            ) from exc
        if not isinstance(data, dict):
            raise RoutingProviderError(f"Routing provider ({provider}) returned an unexpected payload.")
        return data

    @staticmethod
    def _coords_from_geojson(coordinates, provider: str) -> np.ndarray:
        try:
            arr = np.asarray(coordinates, dtype=float)[:, [1, 0]]  # GeoJSON is [lon, lat]
        except (TypeError, ValueError, IndexError) as exc:
            raise RoutingProviderError(f"Routing provider ({provider}) returned invalid geometry.") from exc
        if arr.ndim != 2 or len(arr) < 2 or not np.isfinite(arr).all():
            raise RoutingProviderError(f"Routing provider ({provider}) returned an empty or invalid route geometry.")
        return arr


class OSRMProvider(RoutingProvider):
    name = "osrm"
    NOT_FOUND_CODES = {"NoRoute", "NoSegment", "NoMatch"}

    def route(self, start, finish):
        url = (
            f"{self.cfg['OSRM_BASE_URL']}/route/v1/driving/"
            f"{start[1]:.6f},{start[0]:.6f};{finish[1]:.6f},{finish[0]:.6f}"
        )
        # polyline6 + steps: smaller payload than GeoJSON (~615 KB vs ~820 KB coast to coast) and the
        # steps carry the highway refs ("I 80; US 6") used for location confidence. Still ONE request.
        params = {"overview": "full", "geometries": "polyline6", "alternatives": "false", "steps": "true"}
        response = self._send("GET", url, params=params)
        if response.status_code == 429:
            raise RoutingUnavailable("Routing provider (osrm) rate limit reached; retry shortly.")
        data = self._json(response, self.name)
        code = data.get("code")
        if code in self.NOT_FOUND_CODES:
            raise RouteNotFound(
                "No drivable route was found between the start and finish.",
                details={"provider_code": code, "provider_message": data.get("message", "")},
            )
        if response.status_code != 200 or code != "Ok":
            raise RoutingProviderError(
                f"Routing provider (osrm) error: HTTP {response.status_code}, code {code!r}.",
                details={"provider_message": data.get("message", "")},
            )
        try:
            route = data["routes"][0]
            distance_m = float(route["distance"])
            duration_s = float(route["duration"])
            coords = decode_polyline_array(route["geometry"], precision=6)
            steps = [step for leg in route.get("legs", []) for step in leg.get("steps", [])]
            segments = self._road_segments(steps)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise RoutingProviderError("Routing provider (osrm) returned an unexpected payload.") from exc
        if len(coords) < 2 or not np.isfinite(coords).all():
            raise RoutingProviderError("Routing provider (osrm) returned an empty or invalid route geometry.")
        return RouteResult(distance_m / METERS_PER_MILE, duration_s, coords, self.name, road_segments=segments)

    @staticmethod
    def _road_segments(steps: list[dict]) -> list[RoadSegment]:
        segments, mile = [], 0.0
        for step in steps:
            length = float(step.get("distance", 0.0)) / METERS_PER_MILE
            refs = route_ref_tokens(step.get("ref", ""))
            if refs and length > 0:
                segments.append(RoadSegment(mile, mile + length, refs))
            mile += length
        return segments


class OpenRouteServiceProvider(RoutingProvider):
    """OpenRouteService v2 directions (GeoJSON). Requires ORS_API_KEY."""

    name = "openrouteservice"
    NOT_FOUND_CODES = {2009, 2010, 2004}  # route not found / point not routable / limit exceeded

    def route(self, start, finish):
        key = self.cfg.get("ORS_API_KEY")
        if not key:
            raise RoutingProviderError("ROUTING_PROVIDER=openrouteservice but ORS_API_KEY is not set.")
        url = f"{self.cfg['ORS_BASE_URL']}/v2/directions/driving-car/geojson"
        body = {"coordinates": [[start[1], start[0]], [finish[1], finish[0]]]}
        response = self._send("POST", url, json=body, headers={"Authorization": key})
        if response.status_code == 429:
            raise RoutingUnavailable("Routing provider (openrouteservice) quota or rate limit reached.")
        data = self._json(response, self.name)
        error = data.get("error")
        if error:
            err_code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else str(error)
            if err_code in self.NOT_FOUND_CODES:
                raise RouteNotFound("No drivable route was found between the start and finish.",
                                    details={"provider_code": err_code, "provider_message": message})
            raise RoutingProviderError(f"Routing provider (openrouteservice) error: {message}",
                                       details={"provider_code": err_code, "http_status": response.status_code})
        if response.status_code != 200:
            raise RoutingProviderError(f"Routing provider (openrouteservice) error: HTTP {response.status_code}.")
        try:
            feature = data["features"][0]
            summary = feature["properties"]["summary"]
            coords = self._coords_from_geojson(feature["geometry"]["coordinates"], self.name)
            distance_m = float(summary["distance"])
            duration_s = float(summary.get("duration")) if summary.get("duration") is not None else None
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RoutingProviderError("Routing provider (openrouteservice) returned an unexpected payload.") from exc
        return RouteResult(distance_m / METERS_PER_MILE, duration_s, coords, self.name)


PROVIDERS = {"osrm": OSRMProvider, "openrouteservice": OpenRouteServiceProvider}


def get_provider(cfg: dict | None = None) -> RoutingProvider:
    cfg = cfg or settings.FUEL_PLANNER
    try:
        return PROVIDERS[cfg["ROUTING_PROVIDER"]](cfg)
    except KeyError as exc:
        raise RoutingProviderError(f"Unknown ROUTING_PROVIDER {cfg['ROUTING_PROVIDER']!r}.") from exc


def route_cache_key(provider: str, start, finish) -> str:
    raw = f"{provider}:{start[0]:.{CACHE_PRECISION}f},{start[1]:.{CACHE_PRECISION}f};" \
          f"{finish[0]:.{CACHE_PRECISION}f},{finish[1]:.{CACHE_PRECISION}f}"
    return f"route:{CACHE_VERSION}:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


_inflight_guard = threading.Lock()
_inflight: dict[str, threading.Lock] = {}


def _from_cache(key: str, provider_name: str) -> RouteResult | None:
    cached = cache.get(key)
    if not cached:
        return None
    try:
        coords = np.frombuffer(zlib.decompress(cached["coords"]), dtype="<f8").reshape(-1, 2)
        segments = [RoadSegment(a, b, frozenset(refs)) for a, b, refs in cached.get("roads", [])]
        if len(coords) < 1:
            raise ValueError("empty geometry")
        return RouteResult(float(cached["distance_miles"]), cached["duration_seconds"], coords,
                           provider_name, from_cache=True, provider_calls=0, road_segments=segments)
    except (KeyError, TypeError, ValueError, zlib.error) as exc:
        logger.warning("discarding corrupted route cache entry %s: %s", key, exc)
        cache.delete(key)
        return None


def get_route(start: tuple[float, float], finish: tuple[float, float], provider: RoutingProvider | None = None) -> RouteResult:
    """Return the driving route, from cache when possible (0 calls) or 1 provider call."""
    provider = provider or get_provider()
    key = route_cache_key(provider.name, start, finish)
    cached = _from_cache(key, provider.name)
    if cached:
        return cached

    # Single flight: concurrent identical requests wait for the first one's result.
    with _inflight_guard:
        key_lock = _inflight.setdefault(key, threading.Lock())
    # Waiters give up at the provider's total deadline instead of queueing indefinitely.
    if not key_lock.acquire(timeout=provider.total_timeout + 5):
        raise RoutingTimeout("Timed out waiting for an identical routing request in progress.")
    try:
        cached = _from_cache(key, provider.name)
        if cached:
            return cached
        return _fetch_and_cache(key, provider, start, finish)
    finally:
        key_lock.release()
        with _inflight_guard:
            if _inflight.get(key) is key_lock:
                _inflight.pop(key, None)


def _fetch_and_cache(key: str, provider: RoutingProvider, start, finish) -> RouteResult:
    t0 = time.perf_counter()
    try:
        result = provider.route(start, finish)
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info("routing call provider=%s elapsed_ms=%.0f", provider.name, elapsed_ms)
    result.provider_calls = 1
    result.provider_ms = elapsed_ms
    cache.set(key, {
        "distance_miles": result.distance_miles,
        "duration_seconds": result.duration_seconds,
        # Raw float64 [lat, lon] pairs, zlib-compressed: lossless and fast to load.
        "coords": zlib.compress(np.ascontiguousarray(result.coordinates, dtype="<f8").tobytes(), 1),
        "roads": [(seg.start_mile, seg.end_mile, sorted(seg.refs)) for seg in result.road_segments],
    })  # default timeout from CACHES (ROUTE_CACHE_SECONDS)
    return result
