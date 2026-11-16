"""Starting-fuel valuation and cost accounting. (No network.)"""

from django.test import SimpleTestCase

from route_api.services.cost_calculator import choose_start_price, render_stop, summarize
from route_api.services.fuel_optimizer import FuelCandidate, optimize_fuel_plan
from route_api.services.route_matching import CorridorStation

from .helpers import station


def corridor(opis_id, mile, price, off=0.0, name=None):
    return CorridorStation(station(opis_id, 35.0, -100.0, price, name=name), mile, off, 35.0, -100.0)


def plan(corridor_stations, route_miles, **kw):
    cands = [FuelCandidate(c, c.mile_marker, c.off_route_miles, c.price) for c in corridor_stations]
    return optimize_fuel_plan(cands, route_miles, range_miles=500, mpg=10, **kw)


class StartPriceTests(SimpleTestCase):
    def test_nearest_by_mile_plus_detour_then_price(self):
        stations = [corridor(1, 5, 3.9, off=10), corridor(2, 8, 3.5, off=1), corridor(3, 9, 3.0, off=0)]
        start = choose_start_price(stations, 3.4)  # distances: 15, 9, 9 -> tie broken by price
        self.assertEqual((start.price, start.station.station.opis_id), (3.0, 3))
        self.assertEqual(start.method, "nearest_corridor_station")
        self.assertIsNone(start.warning)

    def test_dataset_average_when_no_corridor_station(self):
        start = choose_start_price([], 3.41741)
        self.assertEqual((start.price, start.method, start.station), (3.41741, "dataset_average", None))
        self.assertIn("average", start.warning)


class SummaryTests(SimpleTestCase):
    def test_short_trip_starting_fuel_only(self):
        stations = [corridor(1, 0, 4.0, off=2), corridor(2, 100, 3.0)]
        p = plan(stations, 300)
        s = summarize(p, choose_start_price(stations, 3.5))
        t = s["totals"]
        self.assertEqual(s["stops"], [])
        self.assertEqual(s["starting_fuel"]["gallons_used"], 30.0)
        self.assertEqual(s["starting_fuel"]["price_per_gallon"], 4.0)          # nearest: mile 0 + 2 off
        self.assertEqual(s["starting_fuel"]["estimated_value_of_fuel_used"], 120.0)
        self.assertEqual((t["refueling_cost"], t["gallons_purchased"]), (0, 0))
        self.assertEqual(t["total_fuel_cost"], 120.0)
        self.assertEqual(t["fuel_remaining_at_destination_gallons"], 20.0)   # reported, not credited

    def test_long_trip_separates_starting_fuel_from_refuelling(self):
        # A@400 $4, B@800 $3, trip 1200 (hand-calculated in test_fuel_optimizer): refuelling $240, 70 gal.
        stations = [corridor(1, 400, 4.0, name="A"), corridor(2, 800, 3.0, name="B")]
        p = plan(stations, 1200)
        s = summarize(p, choose_start_price(stations, 3.5))
        t = s["totals"]
        self.assertEqual(t["gallons_purchased"], 70.0)
        self.assertEqual(t["refueling_cost"], 240.0)
        self.assertEqual(s["starting_fuel"]["gallons_used"], 50.0)
        self.assertEqual(s["starting_fuel"]["estimated_value_of_fuel_used"], 200.0)  # 50 gal x $4 (A is nearest)
        self.assertEqual(t["total_fuel_cost"], 440.0)
        self.assertEqual(t["fuel_burned_gallons"], 120.0)                           # 1200 mi / 10 mpg
        self.assertEqual(t["fuel_remaining_at_destination_gallons"], 0.0)
        self.assertEqual([x["gallons_purchased"] for x in s["stops"]], [30.0, 40.0])
        self.assertIn("cheaper", s["stops"][0]["reason"])
        self.assertIn("destination", s["stops"][1]["reason"])

    def test_detours_counted_in_fuel_burned(self):
        stations = [corridor(1, 400, 3.2, off=5)]
        p = plan(stations, 800)
        t = summarize(p, choose_start_price(stations, 3.5))["totals"]
        self.assertEqual((t["estimated_detour_miles"], t["total_miles_driven"], t["fuel_burned_gallons"]), (10.0, 810.0, 81.0))
        self.assertEqual(t["gallons_purchased"], 31.0)  # 81 burned - 50 starting

    def test_rendered_stop_fields_and_rounding(self):
        stations = [corridor(1, 250, 3.00733333, name="WOODSHED")]
        p = plan(stations, 500.5)
        row = render_stop(1, p.stops[0])
        self.assertEqual(row["gallons_purchased"], 0.05)
        self.assertEqual(row["price_per_gallon"], 3.007)
        self.assertEqual(row["cost"], 0.15)
        self.assertEqual(row["location"]["precision"], "city-level (approximate)")
        for key in ("mile_marker", "distance_from_route_miles", "estimated_detour_miles", "fuel_on_arrival_gallons",
                    "fuel_on_departure_gallons", "decision", "reason"):
            self.assertIn(key, row)
