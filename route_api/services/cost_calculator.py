"""
Fuel cost accounting (see README, "Fuel model and assumptions").

Kept deliberately separate:
  * starting fuel  - already in the tank; its use is an ESTIMATED cost, valued at
                     the price of the corridor station nearest the start;
  * refuelling     - money actually spent at the planned stops.
Total estimated fuel cost = starting fuel burned x start price + refuelling spend.

All arithmetic is unrounded; values are rounded only when rendered.
"""

from __future__ import annotations

from dataclasses import dataclass

from .fuel_optimizer import (
    DECISION_FILL_UP, DECISION_REACH_CHEAPER, DECISION_REACH_DESTINATION, DECISION_REACH_EQUAL,
    FuelPlan, PlannedStop,
)
from .route_matching import CorridorStation


@dataclass(frozen=True)
class StartFuelValuation:
    price: float
    method: str                      # "nearest_corridor_station" | "dataset_average"
    station: CorridorStation | None
    warning: str | None = None


def choose_start_price(corridor: list[CorridorStation], dataset_average_price: float) -> StartFuelValuation:
    """Price of the corridor station nearest the start (by mile_marker + off_route)."""
    if corridor:
        nearest = min(corridor, key=lambda c: (c.mile_marker + c.off_route_miles, c.price, c.station.opis_id))
        return StartFuelValuation(nearest.price, "nearest_corridor_station", nearest)
    return StartFuelValuation(
        dataset_average_price, "dataset_average", None,
        warning="No fuel station lies within the corridor of this route; the starting fuel is valued "
                "at the average price of all loaded stations.",
    )


def stop_reason(stop: PlannedStop) -> str:
    nxt = stop.next_candidate
    if stop.decision == DECISION_REACH_DESTINATION or nxt is None:
        return "Bought only enough fuel to reach the destination; no cheaper option is needed."
    nxt_station: CorridorStation = nxt.key
    where = f"{nxt_station.station.name} ({nxt_station.station.city}, {nxt_station.station.state}) at mile {nxt.mile:.0f}"
    if stop.decision == DECISION_REACH_CHEAPER:
        return (f"Bought only enough to reach {where}, which is cheaper "
                f"(${nxt.price:.3f} vs ${stop.candidate.price:.3f}/gal).")
    if stop.decision == DECISION_REACH_EQUAL:
        return f"Bought only enough to reach {where}, which has the same price (${nxt.price:.3f}/gal)."
    if stop.decision == DECISION_FILL_UP:
        return (f"Filled the tank: no cheaper station is reachable within range, and the next stop, {where}, "
                f"costs more (${nxt.price:.3f} vs ${stop.candidate.price:.3f}/gal).")
    return stop.decision  # pragma: no cover


def render_stop(index: int, stop: PlannedStop) -> dict:
    cs: CorridorStation = stop.candidate.key
    s = cs.station
    return {
        "sequence": index,
        "opis_id": s.opis_id,
        "name": s.name,
        "address": s.address,
        "city": s.city,
        "state": s.state,
        "location": {"lat": round(s.latitude, 6), "lon": round(s.longitude, 6),
                     "precision": "city-level (approximate)"},
        "price_per_gallon": round(s.price, 3),
        "mile_marker": round(cs.mile_marker, 1),
        "distance_from_route_miles": round(cs.off_route_miles, 1),
        "estimated_detour_miles": round(stop.detour_miles, 1),
        "fuel_on_arrival_gallons": round(stop.arrival_gallons, 2),
        "gallons_purchased": round(stop.purchase_gallons, 3),
        "fuel_on_departure_gallons": round(stop.departure_gallons, 2),
        "cost": round(stop.cost, 2),
        "decision": stop.decision,
        "reason": stop_reason(stop),
    }


def summarize(plan: FuelPlan, start: StartFuelValuation) -> dict:
    mpg = plan.mpg
    start_gallons = plan.start_fuel_miles / mpg
    burned_gallons = plan.burned_miles / mpg
    # Starting fuel is burned first; any fuel left at the destination is reported, not credited.
    start_used_gallons = min(plan.start_fuel_miles, plan.burned_miles) / mpg
    start_used_value = start_used_gallons * start.price
    refuel_gallons = plan.purchased_miles / mpg
    refuel_cost = plan.purchase_cost
    total = start_used_value + refuel_cost

    starting_fuel = {
        "gallons_in_tank_at_start": round(start_gallons, 3),
        "gallons_used": round(start_used_gallons, 3),
        "price_per_gallon": round(start.price, 3),
        "valuation_method": start.method,
        "estimated_value_of_fuel_used": round(start_used_value, 2),
    }
    if start.station is not None:
        s = start.station.station
        starting_fuel["valued_at_station"] = {
            "opis_id": s.opis_id, "name": s.name, "city": s.city, "state": s.state,
            "mile_marker": round(start.station.mile_marker, 1),
            "distance_from_route_miles": round(start.station.off_route_miles, 1),
        }
    return {
        "stops": [render_stop(i, stop) for i, stop in enumerate(plan.stops, start=1)],
        "starting_fuel": starting_fuel,
        "totals": {
            "route_distance_miles": round(plan.route_miles, 1),
            "estimated_detour_miles": round(plan.detour_miles, 1),
            "total_miles_driven": round(plan.burned_miles, 1),
            "fuel_burned_gallons": round(burned_gallons, 3),
            "number_of_stops": len(plan.stops),
            "gallons_purchased": round(refuel_gallons, 3),
            "refueling_cost": round(refuel_cost, 2),
            "starting_fuel_cost_estimate": round(start_used_value, 2),
            "total_fuel_cost": round(total, 2),
            "fuel_remaining_at_destination_gallons": round(plan.end_fuel_miles / mpg, 3),
        },
    }
