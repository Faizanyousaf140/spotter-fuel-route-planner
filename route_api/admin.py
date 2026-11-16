from django.contrib import admin

from .models import FuelStation, RoutePlan


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ("opis_id", "name", "city", "state", "price", "latitude", "longitude", "geocode_source")
    list_filter = ("state", "geocode_source")
    search_fields = ("name", "city", "opis_id")


@admin.register(RoutePlan)
class RoutePlanAdmin(admin.ModelAdmin):
    list_display = ("id", "created_at", "start_query", "finish_query", "route_distance_miles", "total_fuel_cost")
    readonly_fields = [f.name for f in RoutePlan._meta.fields]
