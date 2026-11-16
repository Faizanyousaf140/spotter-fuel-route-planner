"""
Fuel cost accounting (see README, "Fuel model and assumptions").

Kept deliberately separate:
  * starting fuel  - already in the tank; its use is an ESTIMATED cost, valued at
                     the price of the corridor station nearest the start;
  * refuelling     - money actually spent at the planned stops.
Total estimated fuel cost = starting fuel burned x start price + refuelling spend.

Rounding policy (so that every published number can be re-added by hand):
  * the optimiser works unrounded;
  * displayed fuel levels are the exact levels rounded to 0.001 gal at each
    node; gallons bought = departure - arrival and fuel used on a leg =
    previous departure - next arrival, so the displayed ledger balances exactly;
  * money is rounded per purchase, like a pump receipt:
    cost = gallons shown x price (stored 5-decimal precision), to the cent;
    totals are sums of the displayed parts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

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


GAL_DP = 3       # gallons shown to 0.001 (like a pump)
PRICE_DP = 5     # prices kept at the stored precision (averages of OPIS prices)


def _gal(miles: float, mpg: float) -> float:
    return round(max(miles, 0.0) / mpg, GAL_DP)


CENT = Decimal("0.01")


def _cents(gallons: float, price: float) -> Decimal:
    """gallons x price rounded half-up to the cent, in exact decimal arithmetic."""
    return (Decimal(repr(gallons)) * Decimal(repr(price))).quantize(CENT, rounding=ROUND_HALF_UP)


def summarize(plan: FuelPlan, start: StartFuelValuation, confidence: dict | None = None) -> dict:
    """Render the plan as consistent, hand-checkable numbers (see module docstring)."""
    mpg, cap = plan.mpg, plan.range_miles / plan.mpg
    confidence = confidence or {}
    stops_out: list[dict] = []
    trip_cost = Decimal("0")
    trip_gallons = 0.0
    prev_departure = round(plan.start_fuel_miles / mpg, GAL_DP)
    prev_mile, prev_off = 0.0, 0.0
    max_leg = 0.0
    for index, stop in enumerate(plan.stops, start=1):
        cs: CorridorStation = stop.candidate.key
        s = cs.station
        leg_miles = prev_off + (stop.candidate.mile - prev_mile) + stop.candidate.off_route
        max_leg = max(max_leg, leg_miles)
        arrival = _gal(stop.arrival_fuel_miles, mpg)
        departure = min(_gal(stop.departure_fuel_miles, mpg), round(cap, GAL_DP))
        bought = round(departure - arrival, GAL_DP)
        price = round(s.price, PRICE_DP)
        cost = _cents(bought, price)
        trip_cost += cost
        trip_gallons += bought
        conf = confidence.get(s.opis_id)
        stops_out.append({
            "sequence": index,
            "opis_id": s.opis_id,
            "name": s.name,
            "address": s.address,
            "city": s.city,
            "state": s.state,
            "location": {
                "lat": round(s.latitude, 6), "lon": round(s.longitude, 6),
                "precision": "city-level (approximate)",
                "confidence": conf.level if conf else None,
                "matched_route_highways": list(conf.matched_highways) if conf else [],
                "confidence_reason": conf.reason if conf else None,
            },
            "price_per_gallon": price,
            "mile_marker": round(cs.mile_marker, 2),
            "distance_from_route_miles": round(cs.off_route_miles, 2),
            "estimated_detour_miles": round(stop.detour_miles, 2),
            "leg_from_previous": {"miles": round(leg_miles, 2),
                                  "fuel_used_gallons": round(prev_departure - arrival, GAL_DP)},
            "fuel_on_arrival_gallons": arrival,
            "gallons_purchased": bought,
            "fuel_on_departure_gallons": departure,
            "cost": float(cost),
            "decision": stop.decision,
            "reason": stop_reason(stop),
        })
        prev_departure, prev_mile, prev_off = departure, stop.candidate.mile, stop.candidate.off_route

    final_leg_miles = prev_off + (plan.route_miles - prev_mile)
    max_leg = max(max_leg, final_leg_miles)
    remaining = _gal(plan.end_fuel_miles, mpg)
    trip_gallons = round(trip_gallons, GAL_DP)
    start_gallons = round(plan.start_fuel_miles / mpg, GAL_DP)
    # Displayed ledger: burned = start + bought - remaining (exact in the shown decimals).
    burned = round(start_gallons + trip_gallons - remaining, GAL_DP)
    start_used = round(min(start_gallons, burned), GAL_DP)  # starting fuel is burned first
    start_value = _cents(start_used, round(start.price, PRICE_DP))

    starting_fuel = {
        "gallons_in_tank_at_start": start_gallons,
        "gallons_used": start_used,
        "price_per_gallon": round(start.price, PRICE_DP),
        "valuation_method": start.method,
        "estimated_value_of_fuel_used": float(start_value),
    }
    if start.station is not None:
        st = start.station.station
        starting_fuel["valued_at_station"] = {
            "opis_id": st.opis_id, "name": st.name, "city": st.city, "state": st.state,
            "mile_marker": round(start.station.mile_marker, 2),
            "distance_from_route_miles": round(start.station.off_route_miles, 2),
        }
    return {
        "stops": stops_out,
        "starting_fuel": starting_fuel,
        "totals": {
            "route_distance_miles": round(plan.route_miles, 2),
            "estimated_detour_miles": round(plan.detour_miles, 2),
            "total_miles_driven": round(plan.burned_miles, 2),
            "fuel_burned_gallons": burned,
            "number_of_stops": len(plan.stops),
            "gallons_purchased": trip_gallons,
            "refueling_cost": float(trip_cost),
            "starting_fuel_cost_estimate": float(start_value),
            "total_fuel_cost": float(start_value + trip_cost),
            "fuel_remaining_at_destination_gallons": remaining,
            "final_leg": {"miles": round(final_leg_miles, 2),
                          "fuel_used_gallons": round(prev_departure - remaining, GAL_DP)},
            "longest_leg_miles": round(max_leg, 2),
        },
    }
