import logging

from django.conf import settings

# Keep test output readable; services log at INFO in normal operation.
logging.getLogger("route_api").setLevel(logging.WARNING)

# Mocked providers need no rate-limit spacing (live tests sleep explicitly).
settings.FUEL_PLANNER["ROUTING_MIN_INTERVAL_SECONDS"] = 0
