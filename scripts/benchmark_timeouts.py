"""Measure real local HTTP deadlines and verify every worker is reaped."""
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
from django.conf import settings
settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "deadline-bench"}}
django.setup()

from django.core.cache import cache
from route_api.services.http_utils import active_http_workers
from route_api.services.routing_service import OSRMProvider, get_route
from tests.local_http import local_routing_server

START, FINISH = (40.7128, -74), (34.0522, -118.2437)


def main():
    report = []
    for mode in ("slow", "headers", "stall"):
        with local_routing_server() as (url, calls):
            budget = .8 if mode != "stall" else 2
            cfg = {**settings.FUEL_PLANNER, "OSRM_BASE_URL": url + "/" + mode,
                   "ROUTING_TOTAL_TIMEOUT": budget, "ROUTING_READ_TIMEOUT": .1 if mode == "stall" else .2,
                   "ROUTING_MIN_INTERVAL_SECONDS": 0}
            start = time.perf_counter()
            try:
                OSRMProvider(cfg).route(START, FINISH)
                raise AssertionError("Expected a timeout")
            except Exception as error:
                assert getattr(error, "status_code", None) == 504, repr(error)
                elapsed = time.perf_counter() - start
                assert elapsed < budget + .4
                assert len(calls) == 1
                assert not active_http_workers()
                report.append({"scenario": mode, "budget_seconds": budget, "elapsed_ms": round(elapsed * 1000, 1),
                               "http_status": 504, "external_requests": len(calls), "workers_remaining": 0})
    cache.clear()
    with local_routing_server() as (url, calls):
        cfg = {**settings.FUEL_PLANNER, "OSRM_BASE_URL": url + "/slow", "ROUTING_TOTAL_TIMEOUT": .8,
               "ROUTING_READ_TIMEOUT": .2, "ROUTING_MIN_INTERVAL_SECONDS": 0}
        barrier, errors = threading.Barrier(5), []
        def run():
            barrier.wait()
            try:
                get_route(START, FINISH, OSRMProvider(cfg))
            except Exception as error:
                errors.append(getattr(error, "status_code", None))
        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        start = time.perf_counter()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
        elapsed = time.perf_counter() - start
        assert not any(t.is_alive() for t in threads)
        assert errors == [504] * 4
        assert len(calls) == 1 and not active_http_workers()
        report.append({"scenario": "4 concurrent identical trickling requests", "budget_seconds": .8,
                       "elapsed_ms": round(elapsed * 1000, 1), "http_statuses": errors,
                       "external_requests": len(calls), "workers_remaining": 0})
    path = ROOT / "validation/timeout_benchmarks.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
