"""Preload in-memory indexes when a server process starts (first request stays fast)."""

import logging
import time

from django.db import DatabaseError

from .services.errors import StationDataUnavailable
from .services.gazetteer import get_place_index
from .services.station_service import get_station_index
from .services.us_boundaries import get_boundary_index

logger = logging.getLogger(__name__)


def warm_up() -> None:
    t0 = time.perf_counter()
    try:
        get_place_index()
        get_boundary_index()
    except OSError as exc:
        logger.warning("Place reference not loaded at startup: %s", exc)
    try:
        get_station_index()
    except (StationDataUnavailable, DatabaseError) as exc:
        # Not fatal: the API answers 503 until `migrate` + `load_stations` have been run.
        logger.warning("Station index not loaded at startup: %s", exc)
    logger.info("warm-up finished in %.0f ms", (time.perf_counter() - t0) * 1000)
