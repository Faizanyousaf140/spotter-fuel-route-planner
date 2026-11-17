import time

from django.conf import settings
from django.db import DatabaseError
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from .exceptions import error_body
from .models import FuelStation, RoutePlan
from .serializers import RouteRequestSerializer
from .services.planner import plan_route


class RoutePlanRateThrottle(SimpleRateThrottle):
    """Per-client-IP limit for planning requests; rate read from settings at request time."""

    scope = "route_plans"

    def get_rate(self):
        return settings.FUEL_PLANNER.get("ROUTE_RATE_LIMIT") or None

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class RoutePlanView(APIView):
    """
    POST /api/v1/route/

    Body: {"start": "New York, NY", "finish": "Los Angeles, CA", "corridor_miles": 15}
    Returns the route geometry, optimal fuel stops and fuel cost (201 Created).
    """

    serializer_class = RouteRequestSerializer
    throttle_classes = [RoutePlanRateThrottle]

    def post(self, request):
        t0 = time.perf_counter()
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = plan_route(data["start"], data["finish"], corridor_miles=data.get("corridor_miles"),
                            min_location_confidence=data["min_location_confidence"])

        detail_url = request.build_absolute_uri(reverse("route-plan-detail", args=[result.plan.id]))
        body = {
            "plan_id": str(result.plan.id),
            "map_url": request.build_absolute_uri(reverse("route-plan-map", args=[result.plan.id])),
            "plan_url": detail_url,
            **result.response,
        }
        body["meta"]["timings_ms"]["total_request"] = round((time.perf_counter() - t0) * 1000, 1)
        return Response(body, status=status.HTTP_201_CREATED, headers={"Location": detail_url})


class RoutePlanDetailView(APIView):
    """GET /api/v1/route/<plan_id>/ - a previously computed plan (persisted in the database)."""

    def get(self, request, plan_id):
        plan = RoutePlan.objects.filter(pk=plan_id).first()
        if plan is None:
            return Response(error_body("not_found", "Route plan not found."), status=status.HTTP_404_NOT_FOUND)
        return Response({
            "plan_id": str(plan.id),
            "created_at": plan.created_at.isoformat(),
            "map_url": request.build_absolute_uri(reverse("route-plan-map", args=[plan.id])),
            **plan.response,
        })


class HealthView(APIView):
    """GET /api/v1/health/ - database and station data status (503 if not ready)."""

    def get(self, request):
        try:
            stations = FuelStation.objects.count()
            plans = RoutePlan.objects.count()
        except DatabaseError:
            return Response({"status": "error", "database": "unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        ok = stations > 0
        return Response(
            {
                "status": "ok" if ok else "degraded",
                "database": "ok",
                "stations_loaded": stations,
                "saved_route_plans": plans,
                "routing_provider": settings.FUEL_PLANNER["ROUTING_PROVIDER"],
                "detail": None if ok else "No fuel stations loaded. Run `python manage.py load_stations`.",
            },
            status=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        )


def route_map(request, plan_id):
    """GET /api/v1/route/<plan_id>/map/ - the planner app showing a saved plan (HTML)."""
    plan = get_object_or_404(RoutePlan, pk=plan_id)
    data = {
        "plan_id": str(plan.id),
        "created_at": plan.created_at.isoformat(),
        "map_url": request.build_absolute_uri(reverse("route-plan-map", args=[plan.id])),
        "plan_url": request.build_absolute_uri(reverse("route-plan-detail", args=[plan.id])),
        **plan.response,
    }
    return render(request, "route_api/app.html", {"plan": plan, "data": data,
        "vehicle_range_miles": settings.FUEL_PLANNER["VEHICLE_RANGE_MILES"],
        "vehicle_mpg": settings.FUEL_PLANNER["VEHICLE_MPG"]})


def demo_page(request):
    """GET / - the planner app (form, results, map). ?start=..&finish=.. plans immediately."""
    return render(request, "route_api/app.html", {"data": None,
        "vehicle_range_miles": settings.FUEL_PLANNER["VEHICLE_RANGE_MILES"],
        "vehicle_mpg": settings.FUEL_PLANNER["VEHICLE_MPG"]})
