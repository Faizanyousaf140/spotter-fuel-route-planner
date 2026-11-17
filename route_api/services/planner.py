"""
Request orchestration: inputs -> route -> corridor stations -> fuel plan -> response.

External calls per request: at most one routing call (zero on a route-cache
hit) and, only for free-text inputs the offline gazetteer cannot resolve, one
cached geocoder call per location. Everything else runs in memory.
"""

from __future__ import annotations

import copy
import logging
import hashlib
import json
import time
from dataclasses import dataclass

import numpy as np
from django.conf import settings
from django.core.cache import cache

from route_api.models import RoutePlan

from .cost_calculator import choose_start_price, summarize
from .errors import NoFeasiblePlan, PlannerError
from .fuel_optimizer import FuelCandidate, optimize_fuel_plan
from .geo import simplify_polyline
from .geocoding_service import resolve_location
from .highways import CONFIDENCE_LOW, CONFIDENCE_RANK, assess_location
from .route_matching import build_route_profile, match_stations
from .routing_service import get_route, route_cache_key
from .station_service import get_station_index
from .us_boundaries import BOUNDARY_VERSION

logger = logging.getLogger(__name__)

# Bump when the plan computation or the response format changes (invalidates cached plans).
PLAN_CACHE_VERSION = "p3"

COORDINATE_WARNING = (
    "Station coordinates are approximate: the price file has no coordinates, so each station is placed at "
    "its city's reference point (US Census Gazetteer, or a one-time geocoder lookup). "
    "distance_from_route_miles and estimated_detour_miles are straight-line estimates, not road distances; "
    "each used station is charged a detour of 2 x its straight-line distance from the route. This is an "
    "approximation: the real road detour can be shorter (stop at the exit) or longer (indirect roads)."
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


def _confidence_counts(stops: list[dict]) -> dict:
    counts = {"high": 0, "medium": 0, "low": 0}
    for s in stops:
        counts[s["location"]["confidence"]] = counts.get(s["location"]["confidence"], 0) + 1
    return counts


def _check_plan_invariants(plan) -> None:
    """Defensive check of the optimiser's output before anything is returned or saved."""
    eps = 1e-6
    fuel, prev_mile, prev_off = plan.start_fuel_miles, 0.0, 0.0
    for stop in plan.stops:
        leg = prev_off + stop.candidate.mile - prev_mile + stop.candidate.off_route
        fuel -= leg
        ok = (fuel >= -eps and leg <= plan.range_miles + eps and stop.purchase_miles > 0
              and fuel + stop.purchase_miles <= plan.range_miles + eps)
        if not ok:
            raise PlannerError("Internal error: the fuel plan failed its consistency check.",
                               details={"stop_mile_marker": round(stop.candidate.mile, 2)})
        fuel += stop.purchase_miles
        prev_mile, prev_off = stop.candidate.mile, stop.candidate.off_route
    if fuel - (prev_off + plan.route_miles - prev_mile) < -eps:
        raise PlannerError("Internal error: the fuel plan failed its consistency check.", details={"leg": "final"})


def plan_route(start_value, finish_value, *, corridor_miles: float | None = None,
               min_location_confidence: str = CONFIDENCE_LOW) -> PlanResult:
    cfg = settings.FUEL_PLANNER
    corridor = float(corridor_miles if corridor_miles is not None else cfg["MAX_OFF_ROUTE_MILES"])
    range_miles, mpg = cfg["VEHICLE_RANGE_MILES"], cfg["VEHICLE_MPG"]
    timings: dict[str, float] = {}

    t = time.perf_counter()
    start = resolve_location(start_value, "start")
    finish = resolve_location(finish_value, "finish")
    timings["resolve_locations"] = _ms(t)

    t = time.perf_counter()
    index = get_station_index()
    timings["station_index"] = _ms(t)

    # Cache the complete computation contract, including vehicle configuration,
    # matching/output settings and versioned reference/station snapshots.
    route_key = route_cache_key(cfg["ROUTING_PROVIDER"], start.point, finish.point)
    computation = {
        "calculation_version": PLAN_CACHE_VERSION, "route_key": route_key,
        "vehicle_mpg": mpg, "vehicle_range_miles": range_miles,
        "corridor_miles": corridor, "default_corridor_miles": cfg["MAX_OFF_ROUTE_MILES"],
        "min_location_confidence": min_location_confidence,
        "sample_spacing_miles": cfg["ROUTE_SAMPLE_SPACING_MILES"],
        "response_geometry_tolerance_deg": cfg["RESPONSE_GEOMETRY_TOLERANCE_DEG"],
        "station_revision": index.revision, "station_fingerprint": index.fingerprint,
        "us_boundary_version": BOUNDARY_VERSION,
    }
    plan_key = "plan:" + hashlib.sha256(json.dumps(computation, sort_keys=True,
                                                  separators=(",", ":")).encode()).hexdigest()
    cached_response = cache.get(plan_key)
    if cached_response is not None:
        response = copy.deepcopy(cached_response)
        response["start"], response["finish"] = start.as_dict(), finish.as_dict()
        return _persist(response, start, finish, index, timings, meta_extra={
            "map_api_calls": {"routing": 0, "geocoding": start.external_calls + finish.external_calls},
            "route_cache_hit": True, "plan_cache_hit": True,
            "corridor_stations": response.pop("_corridor_found"),
            "corridor_stations_meeting_min_confidence": response.pop("_corridor_used"),
            "stations_considered_by_optimizer": response.pop("_considered"),
        })

    t = time.perf_counter()
    route = get_route(start.point, finish.point)
    timings["routing"] = _ms(t)

    t = time.perf_counter()
    profile = build_route_profile(route.coordinates, route.distance_miles, cfg["ROUTE_SAMPLE_SPACING_MILES"])
    corridor_stations = match_stations(profile, index, corridor)
    corridor_found = len(corridor_stations)
    # Starting fuel is valued against the full corridor: it must not depend on the confidence filter.
    start_price = choose_start_price(corridor_stations, float(np.mean(index.prices)))
    if CONFIDENCE_RANK[min_location_confidence] > 0:
        # Optional risk control: only use stations whose location confidence meets the minimum.
        need = CONFIDENCE_RANK[min_location_confidence]
        corridor_stations = [
            c for c in corridor_stations
            if CONFIDENCE_RANK[assess_location(c.station.highways, c.mile_marker, c.off_route_miles,
                                               route.road_segments).level] >= need
        ]
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
            "min_location_confidence": min_location_confidence,
            "hint": "Try a larger corridor_miles (max 50), a lower min_location_confidence, or different endpoints.",
        })
        raise
    _check_plan_invariants(fuel_plan)
    confidence = {
        stop.candidate.key.station.opis_id: assess_location(
            stop.candidate.key.station.highways, stop.candidate.mile, stop.candidate.off_route, route.road_segments)
        for stop in fuel_plan.stops
    }
    summary = summarize(fuel_plan, start_price, confidence)
    timings["fuel_optimization"] = _ms(t)

    t = time.perf_counter()
    geometry = _response_geometry(route, start.point, finish.point, cfg["RESPONSE_GEOMETRY_TOLERANCE_DEG"])
    timings["response_geometry"] = _ms(t)

    totals = summary["totals"]
    warnings = [COORDINATE_WARNING, PRICE_WARNING]
    if start_price.warning:
        warnings.append(start_price.warning)
    low = [s["name"] for s in summary["stops"] if s["location"]["confidence"] == CONFIDENCE_LOW]
    if low:
        warnings.append(f"{len(low)} stop(s) have low location confidence ({', '.join(low)}): their address "
                        "highway is not on the route nearby, so the real detour may be longer than estimated.")
    visited = [s["opis_id"] for s in summary["stops"]]
    if len(visited) != len(set(visited)):
        warnings.append("This plan visits the same station twice. That only happens among stations sharing one "
                        "route point (an exit), where going back to the first station is the cheapest option.")
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
            "final_leg": totals["final_leg"],
            "longest_leg_miles": totals["longest_leg_miles"],
            "max_range_miles": range_miles,
        },
        "data_quality": {
            "route_distance_and_geometry": f"from the routing provider ({route.provider})",
            "station_positions": "estimated: city-level reference point of the station's city",
            "location_confidence": _confidence_counts(summary["stops"]),
            "detours": "approximation: 2 x straight-line distance from the route to the city point; "
                       "real road detours can be shorter or longer",
            "prices": "static OPIS file; average of a station's price rows",
            "starting_fuel_value": "estimated: start price x starting gallons used",
            "money_rounding": "each purchase rounded to the cent; totals are sums of the shown parts",
        },
        "assumptions": {
            "vehicle_range_miles": range_miles,
            "vehicle_mpg": mpg,
            "tank_capacity_gallons": round(range_miles / mpg, 3),
            "starting_fuel": "Full tank, valued at the price of the corridor station nearest the start.",
            "fuel_remaining_at_destination": "Reported but not credited.",
            "station_corridor_miles": corridor,
            "min_location_confidence": min_location_confidence,
            "detour_model": "2 x straight-line distance from the route to the station's city-level point.",
            "optimization": "Exact minimum refuelling cost over corridor stations (dynamic programme).",
        },
        "warnings": warnings,
    }

    cache.set(plan_key, {**response, "_corridor_found": corridor_found, "_corridor_used": len(corridor_stations),
                         "_considered": fuel_plan.candidates_considered})
    return _persist(response, start, finish, index, timings, meta_extra={
        "map_api_calls": {"routing": route.provider_calls, "geocoding": start.external_calls + finish.external_calls},
        "route_cache_hit": route.from_cache, "plan_cache_hit": False,
        "corridor_stations": corridor_found,
        "corridor_stations_meeting_min_confidence": len(corridor_stations),
        "stations_considered_by_optimizer": fuel_plan.candidates_considered,
    })


def _persist(response: dict, start, finish, index, timings: dict, meta_extra: dict) -> PlanResult:
    plan = RoutePlan.objects.create(
        start_query=str(start.query)[:255],
        finish_query=str(finish.query)[:255],
        route_distance_miles=response["route"]["distance_miles"],
        total_fuel_cost=response["fuel_cost"]["total_fuel_cost"],
        response=response,
    )
    response["meta"] = {**meta_extra, "stations_loaded": len(index), "station_data_fingerprint": index.fingerprint,
                        "timings_ms": timings}
    logger.info("plan %s: %.0f mi, %d stops, $%.2f, routing_calls=%d plan_cache_hit=%s timings=%s",
                plan.id, response["route"]["distance_miles"], len(response["fuel_stops"]),
                response["fuel_cost"]["total_fuel_cost"], meta_extra["map_api_calls"]["routing"],
                meta_extra["plan_cache_hit"], timings)
    return PlanResult(response, plan)
