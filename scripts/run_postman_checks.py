"""Run the original Postman assertions against normal and real fault servers.

Node/Newman are validation tools in npm's cache, not application dependencies.
All Django servers use var/hardening.sqlite3; none touches the user's main DB.
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
from tests.local_http import local_routing_server


@contextmanager
def server(port, **overrides):
    env = {**os.environ, "DJANGO_DEBUG": "True", "SQLITE_PATH": str(ROOT / "var/hardening.sqlite3"),
           "WARMUP_ON_START": "False", "LOG_LEVEL": "WARNING", "ROUTE_RATE_LIMIT": "60/min", **overrides}
    base = f"http://127.0.0.1:{port}"
    with (ROOT / f"var/postman-server-{port}.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, "-B", "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"],
                                   cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"Validation server {port} did not start")
                try:
                    if requests.get(base + "/api/v1/health/", timeout=.4).status_code == 200:
                        break
                except requests.RequestException:
                    time.sleep(.1)
            yield base
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def newman_cli():
    explicit = os.environ.get("SPOTTER_NEWMAN_CLI")
    if explicit:
        return explicit
    cache = Path(os.environ.get("LOCALAPPDATA", "")) / "npm-cache/_npx"
    return str(next(cache.glob("*/node_modules/newman/bin/newman.js")))


def run(base, name, collection, folders=()):
    report_path = ROOT / f"var/newman-{name}.json"
    command = ["node", newman_cli(), "run", str(collection), "--env-var", "base_url=" + base,
               "--reporters", "json", "--reporter-json-export", str(report_path),
               "--timeout-request", "45000", "--delay-request", "1100"]
    for folder in folders:
        command.extend(["--folder", folder])
    result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=200)
    if not report_path.exists():
        raise RuntimeError(result.stderr.decode(errors="replace"))
    report = json.loads(report_path.read_text(encoding="utf-8"))["run"]
    summary = {"scenario": name, "requests": report["stats"]["requests"],
               "assertions": report["stats"]["assertions"], "failures": report["failures"],
               "statuses": [e.get("response", {}).get("code") for e in report["executions"]]}
    print(json.dumps(summary, indent=2), flush=True)
    assert result.returncode == 0 and not summary["failures"], summary
    return summary


def main():
    collection = ROOT / "postman/Spotter_Fuel_Route_API.postman_collection.json"
    summaries = []
    with server(8012) as base:
        summaries.append(run(base, "normal-default-rate-limit", collection,
                             ["0. Health", "1. Successful routes", "2. Invalid input", "3. Impossible routes"]))
    original = json.loads(collection.read_text(encoding="utf-8"))
    faults = original["item"][-1]
    with tempfile.TemporaryDirectory(prefix="spotter-newman-") as folder:
        subset = Path(folder) / "provider-fault.json"
        for index, name in ((0, "connection-refused-503"), (1, "slow-stream-504")):
            data = copy.deepcopy(original)
            data["item"] = [{**faults, "item": [faults["item"][index]]}]
            subset.write_text(json.dumps(data), encoding="utf-8")
            if index == 0:
                with server(8013, OSRM_BASE_URL="http://127.0.0.1:9") as base:
                    summaries.append(run(base, name, subset))
            else:
                with local_routing_server() as (upstream, calls):
                    with server(8014, OSRM_BASE_URL=upstream + "/slow", ROUTING_TOTAL_TIMEOUT=".8", ROUTING_READ_TIMEOUT=".2") as base:
                        summaries.append(run(base, name, subset))
                    assert len(calls) == 1
    (ROOT / "validation/postman_checks.json").write_text(json.dumps(summaries, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
