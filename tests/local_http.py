"""Real bounded HTTP fixtures for deadline tests and reproducible benchmarks."""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .helpers import osrm_ok


@contextmanager
def local_routing_server():
    stop = threading.Event()
    calls, active = [], set()
    lock = threading.Lock()
    payload = json.dumps(osrm_ok([(40.7128, -74), (34.0522, -118.2437)], 4_500_000).json()).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                calls.append(self.path)
                active.add(threading.current_thread())
            try:
                if self.path.startswith("/headers/"):
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                    self.wfile.flush()
                    for _ in range(100):
                        if stop.wait(.025):
                            return
                        self.wfile.write(b"x")
                        self.wfile.flush()
                    return
                body = json.dumps({"code": "NoRoute", "message": "No route"}).encode() if self.path.startswith("/missing/") else payload
                self.send_response(400 if self.path.startswith("/missing/") else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.path.startswith("/stall/"):
                    stop.wait(2)
                    return
                if self.path.startswith("/slow/"):
                    for byte in payload:
                        if stop.wait(.025):
                            return
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                    return
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                with lock:
                    active.discard(threading.current_thread())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .025}, name="local-http-fixture")
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        with lock:
            handlers = list(active)
        for handler in handlers:
            handler.join(timeout=2)
        if thread.is_alive() or any(t.is_alive() for t in handlers):
            raise AssertionError("Local HTTP fixture leaked a server thread")
