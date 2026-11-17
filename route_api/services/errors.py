"""Domain errors. Each carries the HTTP status and machine-readable code the API returns."""


class PlannerError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


# --- input / location -------------------------------------------------------
class LocationNotFound(PlannerError):
    status_code = 422
    code = "location_not_found"


class LocationOutsideUSA(PlannerError):
    status_code = 422
    code = "location_outside_usa"


class GeocoderUnavailable(PlannerError):
    status_code = 503
    code = "geocoder_unavailable"


class LocationValidationUnavailable(PlannerError):
    status_code = 503
    code = "location_validation_unavailable"


# --- routing ----------------------------------------------------------------
class RouteNotFound(PlannerError):
    status_code = 422
    code = "route_not_found"


class RoutingUnavailable(PlannerError):
    status_code = 503
    code = "routing_unavailable"


class RoutingTimeout(PlannerError):
    status_code = 504
    code = "routing_timeout"


class RoutingProviderError(PlannerError):
    """Provider answered, but with something we cannot use (bad JSON, 5xx, bad key...)."""

    status_code = 502
    code = "routing_provider_error"


# --- data / planning --------------------------------------------------------
class StationDataUnavailable(PlannerError):
    status_code = 503
    code = "station_data_unavailable"


class NoFeasiblePlan(PlannerError):
    status_code = 422
    code = "no_feasible_fuel_plan"
