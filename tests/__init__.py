import logging

# Keep test output readable; services log at INFO in normal operation.
logging.getLogger("route_api").setLevel(logging.WARNING)
