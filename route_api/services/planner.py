"""
Request orchestration: inputs -> route -> corridor stations -> fuel plan -> response.

External calls per request: at most one routing call (zero on a route-cache
hit) and, only for free-text inputs the offline gazetteer cannot resolve, one
cached geocoder call per location. Everything else runs in memory.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np
from django.conf import settings
from django.core.cache import cache

from route_api.models import RoutePlan

from .cost_calculator import choose_start_price, summarize
from .errors import NoFeasiblePlan
from .fuel_optimizer import FuelCandidate, optimize_fuel_plan
from .geo import simplify_polyline
from .geocoding_service import resolve_location
from .route_matching import build_route_profile, match_stations
from .routing_service import get_route, route_cache_key
from .station_service import get_station_index

logger = logging.getLogger(__name__)

COORDINATE_WARNING = (
    "Station coordinates are approximate: the price file has no coordinates, so each station is placed at "
    "its city's reference point (US Census Gazetteer, or a one-time geocoder lookup). "
    "distance_from_route_miles and estimated_detour_miles are straight-line estimates, not road distances; "
    "each used station is charged a detour of 2 x its distance from the route (a conservative estimate)."
)
PRICE_WARNING = (
    "Prices come from the supplied OPIS file (one record per station ID, averaging duplicate price rows); "
    "they are not live prices."
)


@dataclass
class PlanResult:
    response: dict
    plan: RoutePlan


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _response_geometry(route, start_point, finish_point, tolerance: float) -> dict:
    """Simplified GeoJSON LineString for clients, cached alongside the route."""
    key = f"{route_cache_key(route.provider, start_point, finish_point)}:geojson:{tolerance}"
    geometry = cache.get(key)
    if geometry is None:
        simplified = simplify_polyline(route.coordinates, tolerance)
        geometry = {"type": "LineString",
                    "coordinates": [[round(float(lon), 5), round(float(lat), 5)] for lat, lon in simplified]}
        cache.set(key, geometry)
    return geometry


def plan_route(start_value, finish_value, *, corridor_miles: float | None = None) -> PlanResult:
    cfg = settings.FUEL_PLANNER
    corridor = float(corridor_miles if corridor_miles is not None else cfg["MAX_OFF_ROUTE_MILES"])
    range_miles, mpg = cfg["VEHICLE_RANGE_MILES"], cfg["VEHICLE_MPG"]
    timings: dict[str, float] = {}

    t = time.perf_counter()
    start = resolve_location(start_value, "start")
    finish = resolve_location(finish_value, "finish")
    timings["resolve_locations"] = _ms(t)

    t = time.perf_counter()
    route = get_route(start.point, finish.point)
    timings["routing"] = _ms(t)

    t = time.perf_counter()
    index = get_station_index()
    timings["station_index"] = _ms(t)

    t = time.perf_counter()
    profile = build_route_profile(route.coordinates, route.distance_miles, cfg["ROUTE_SAMPLE_SPACING_MILES"])
    corridor_stations = match_stations(profile, index, corridor)
    timings["station_matching"] = _ms(t)

    t = time.perf_counter()
    candidates = [FuelCandidate(c, c.mile_marker, c.off_route_miles, c.price) for c in corridor_stations]
    try:
        fuel_plan = optimize_fuel_plan(candidates, route.distance_miles, range_miles=range_miles, mpg=mpg)
    except NoFeasiblePlan as exc:
        exc.details.update({
            "route_distance_miles": round(route.distance_miles, 1),
            "corridor_miles": corridor,
            "corridor_stations": len(corridor_stations),
            "hint": "Try a larger corridor_miles (max 50) or different endpoints.",
        })
        raise
    start_price = choose_start_price(corridor_stations, float(np.mean(index.prices)))
    summary = summarize(fuel_plan, start_price)
    timings["fuel_optimization"] = _ms(t)

    t = time.perf_counter()
    geometry = _response_geometry(route, start.point, finish.point, cfg["RESPONSE_GEOMETRY_TOLERANCE_DEG"])
    timings["response_geometry"] = _ms(t)

    totals = summary["totals"]
    warnings = [COORDINATE_WARNING, PRICE_WARNING]
    if start_price.warning:
        warnings.append(start_price.warning)
    if start.source == "nominatim" or finish.source == "nominatim":
        warnings.append("A location was resolved by the Nominatim (OpenStreetMap) geocoder; check the "
                        "resolved label/coordinates.")

    response = {
        "start": start.as_dict(),
        "finish": finish.as_dict(),
        "route": {
            "provider": route.provider,
            "distance_miles": round(route.distance_miles, 1),
            "duration_hours": round(route.duration_seconds / 3600, 2) if route.duration_seconds is not None else None,
            "geometry": geometry,
            "geometry_points_returned": len(geometry["coordinates"]),
            "geometry_points_from_provider": int(len(route.coordinates)),
        },
        "fuel_stops": summary["stops"],
        "fuel_cost": {
            "total_fuel_cost": totals["total_fuel_cost"],
            "purchased_during_trip": {
                "gallons": totals["gallons_purchased"],
                "cost": totals["refueling_cost"],
                "number_of_stops": totals["number_of_stops"],
            },
            "starting_fuel_estimate": summary["starting_fuel"],
            "how_total_is_calculated": "starting_fuel_estimate.estimated_value_of_fuel_used + purchased_during_trip.cost",
            "currency": "USD",
        },
        "fuel_usage": {
            "route_distance_miles": totals["route_distance_miles"],
            "estimated_detour_miles": totals["estimated_detour_miles"],
            "total_miles_driven": totals["total_miles_driven"],
            "fuel_burned_gallons": totals["fuel_burned_gallons"],
            "fuel_remaining_at_destination_gallons": totals["fuel_remaining_at_destination_gallons"],
        },
        "assumptions": {
            "vehicle_range_miles": range_miles,
            "vehicle_mpg": mpg,
            "tank_capacity_gallons": round(range_miles / mpg, 3),
            "starting_fuel": "Full tank, valued at the price of the corridor station nearest the start.",
            "fuel_remaining_at_destination": "Reported but not credited.",
            "station_corridor_miles": corridor,
            "detour_model": "2 x straight-line distance from the route to the station's city-level point.",
            "optimization": "Exact minimum refuelling cost over corridor stations (dynamic programme).",
        },
        "warnings": warnings,
    }

    plan = RoutePlan.objects.create(
        start_query=str(start.query)[:255],
        finish_query=str(finish.query)[:255],
        route_distance_miles=route.distance_miles,
        total_fuel_cost=totals["total_fuel_cost"],
        response=response,
    )
    response["meta"] = {
        "map_api_calls": {
            "routing": route.provider_calls,
            "geocoding": start.external_calls + finish.external_calls,
        },
        "route_cache_hit": route.from_cache,
        "stations_loaded": len(index),
        "corridor_stations": len(corridor_stations),
        "stations_considered_by_optimizer": fuel_plan.candidates_considered,
        "timings_ms": timings,
    }
    logger.info(
        "plan %s: %.0f mi, %d stops, $%.2f, routing_calls=%d cache_hit=%s timings=%s",
        plan.id, route.distance_miles, len(fuel_plan.stops), totals["total_fuel_cost"],
        route.provider_calls, route.from_cache, timings,
    )
    return PlanResult(response, plan)
