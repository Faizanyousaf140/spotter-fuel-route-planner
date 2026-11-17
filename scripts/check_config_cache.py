"""Verify persistent cache and GUI after restarting with a different MPG."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_postman_checks import server


def main():
    payload = {"start": "Chicago, IL", "finish": "Dallas, TX"}
    before = requests.post("http://127.0.0.1:8011/api/v1/route/", json=payload, timeout=45)
    before.raise_for_status()
    original = before.json()
    (ROOT / "var/readme-response.json").write_text(json.dumps(original, indent=2), encoding="utf-8")
    assert original["assumptions"]["vehicle_mpg"] == 10
    started = time.monotonic()
    with server(8016, VEHICLE_MPG="20", WARMUP_ON_START="True") as base:
        startup = (time.monotonic() - started) * 1000
        changed = requests.post(base + "/api/v1/route/", json=payload, timeout=45).json()
        repeated = requests.post(base + "/api/v1/route/", json=payload, timeout=45).json()
        assert changed["assumptions"]["vehicle_mpg"] == 20
        assert changed["assumptions"]["tank_capacity_gallons"] == 25
        assert not changed["meta"]["plan_cache_hit"]
        assert changed["meta"]["map_api_calls"]["routing"] == 0
        assert repeated["meta"]["plan_cache_hit"]
        assert repeated["meta"]["map_api_calls"]["routing"] == 0
        assert abs(changed["fuel_cost"]["total_fuel_cost"] * 2 - original["fuel_cost"]["total_fuel_cost"]) < .05
        # Check browser rendering with the non-default vehicle configuration.
        env = {**os.environ, "SPOTTER_BASE_URL": base, "SPOTTER_EXPECTED_MPG": "20",
               "SPOTTER_BROWSER_REPORT": "validation/browser_20mpg_checks.json"}
        subprocess.run(["node", "scripts/check_browser.cjs"], cwd=ROOT, env=env, check=True, timeout=90)
    report = {"passed": True, "startup_ms": round(startup, 1),
              "old_mpg": 10, "new_mpg": 20, "old_cost": original["fuel_cost"]["total_fuel_cost"],
              "new_cost": changed["fuel_cost"]["total_fuel_cost"],
              "changed_plan_cache_hit": changed["meta"]["plan_cache_hit"],
              "repeat_plan_cache_hit": repeated["meta"]["plan_cache_hit"],
              "routing_calls": [changed["meta"]["map_api_calls"]["routing"], repeated["meta"]["map_api_calls"]["routing"]]}
    (ROOT / "validation/config_cache_check.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
