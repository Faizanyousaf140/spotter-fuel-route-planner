"""Consistent JSON error envelope: {"error": {"code", "message", "details"}}."""

import logging

from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler

from .services.errors import PlannerError

logger = logging.getLogger("route_api")

_DRF_CODES = {
    exceptions.ValidationError: ("invalid_request", "Request validation failed."),
    exceptions.ParseError: ("malformed_request", None),
    exceptions.UnsupportedMediaType: ("unsupported_media_type", None),
    exceptions.MethodNotAllowed: ("method_not_allowed", None),
    exceptions.NotFound: ("not_found", None),
    exceptions.Throttled: ("rate_limited", None),
}


def error_body(code: str, message: str, details=None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def api_exception_handler(exc, context):
    if isinstance(exc, PlannerError):
        log = logger.error if exc.status_code >= 500 else logger.info
        log("planner error %s (%s): %s", exc.code, exc.status_code, exc.message)
        return Response(error_body(exc.code, exc.message, exc.details), status=exc.status_code)

    response = exception_handler(exc, context)
    if response is not None:
        code, message = "error", None
        for cls, (c, m) in _DRF_CODES.items():
            if isinstance(exc, cls):
                code, message = c, m
                break
        if isinstance(exc, exceptions.ValidationError):
            details = response.data
        else:
            details = {}
        message = message or str(getattr(exc, "detail", exc))
        response.data = error_body(code, message, details)
        return response

    logger.exception("Unhandled error in %s", context.get("view").__class__.__name__)
    return Response(error_body("internal_error", "An unexpected error occurred."),
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR)
