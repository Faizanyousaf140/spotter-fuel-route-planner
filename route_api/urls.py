from django.urls import path

from . import views

urlpatterns = [
    path("route/", views.RoutePlanView.as_view(), name="route-plan"),
    path("route/<uuid:plan_id>/", views.RoutePlanDetailView.as_view(), name="route-plan-detail"),
    path("route/<uuid:plan_id>/map/", views.route_map, name="route-plan-map"),
    path("health/", views.HealthView.as_view(), name="health"),
]
