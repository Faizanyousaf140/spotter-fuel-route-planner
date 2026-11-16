from django.contrib import admin
from django.urls import include, path

from route_api.views import demo_page

urlpatterns = [
    path("", demo_page, name="demo"),
    path("admin/", admin.site.urls),
    path("api/v1/", include("route_api.urls")),
]
