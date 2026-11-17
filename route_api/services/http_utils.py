"""Bounded HTTP exchanges: each real request has an owned, reaped subprocess.

Socket read timeouts cannot bound DNS, headers, or a trickling response body.
The subprocess owns the whole exchange and is killed and reaped at the deadline.
No executor threads or abandoned network operations remain after cancellation.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
from urllib3.exceptions import ReadTimeoutError


class DeadlineExceeded(Exception):
    pass


class ResponseTooLarge(Exception):
    pass


_workers_lock = threading.Lock()
_workers: set[int] = set()


def active_http_workers() -> tuple[int, ...]:
    """Owned child PIDs, exposed for lifecycle regression checks."""
    with _workers_lock:
        return tuple(_workers)


def is_timeout_error(error: BaseException) -> bool:
    """Requests wraps urllib3 response-body timeouts in ConnectionError."""
    pending, seen = [error], set()
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, (requests.Timeout, ReadTimeoutError, TimeoutError)):
            return True
        pending.extend(x for x in (item.__cause__, item.__context__, *item.args)
                       if isinstance(x, BaseException))
    return False


def remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeadlineExceeded("HTTP exchange exceeded its total deadline")
    return remaining


def request_with_deadline(method: str, url: str, *, deadline: float,
                          max_bytes: int, timeout, headers=None, session=None, **kwargs):
    """Buffer a real HTTP response under a single cancellable exchange budget.

    Explicit non-Requests test sessions keep their deterministic test-double
    behavior. Production networking always runs in a cancellable child process.
    """
    remaining_seconds(deadline)
    if session is not None and not isinstance(session, requests.Session):
        response = session.request(method, url, timeout=timeout, headers=headers or {}, stream=True, **kwargs)
        return read_with_deadline(response, deadline, max_bytes)
    payload = json.dumps({"method": method, "url": url, "timeout": timeout,
                          "headers": headers or {}, "kwargs": kwargs, "max_bytes": max_bytes,
                          "cafile": requests.certs.where()}).encode()
    worker = Path(__file__).with_name("http_worker.py")
    process = subprocess.Popen([sys.executable, "-B", str(worker)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    with _workers_lock:
        _workers.add(process.pid)
    try:
        try:
            output, _ = process.communicate(payload, timeout=remaining_seconds(deadline))
        except subprocess.TimeoutExpired as exc:
            raise DeadlineExceeded("HTTP exchange exceeded its total deadline") from exc
        remaining_seconds(deadline)
        try:
            metadata, content = output.split(b"\n", 1)
            metadata = json.loads(metadata)
        except (ValueError, TypeError) as exc:
            raise requests.ConnectionError("HTTP worker did not return a valid result") from exc
        error = metadata.get("error")
        if error == "timeout":
            raise requests.Timeout("HTTP connect/read timed out")
        if error == "oversized":
            raise ResponseTooLarge("HTTP response exceeded its size limit")
        if error:
            raise requests.ConnectionError("HTTP connection failed")
        response = requests.Response()
        response.status_code = metadata["status"]
        response.headers.update(metadata["headers"])
        response._content, response._content_consumed = content, True
        response.url = url
        response.encoding = requests.utils.get_encoding_from_headers(response.headers)
        return response
    finally:
        if process.poll() is None:
            process.kill()
        # communicate closes pipes and joins its Windows pipe-reader threads.
        # The child owns no descendant processes, so termination is complete.
        process.communicate(timeout=2.0)
        process.wait(timeout=2.0)
        with _workers_lock:
            _workers.discard(process.pid)


def read_with_deadline(response, deadline: float, max_bytes: int):
    """Read ``response`` (opened with ``stream=True``) fully, or raise.

    ``deadline`` is a ``time.monotonic()`` value. Objects without
    ``iter_content`` (test doubles) are returned unchanged.
    """
    if not hasattr(response, "iter_content"):
        return response
    chunks, size = [], 0
    try:
        for chunk in response.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLarge(f"response larger than {max_bytes} bytes")
            remaining_seconds(deadline)
            chunks.append(chunk)
        response._content = b"".join(chunks)
        response._content_consumed = True
        return response
    finally:
        response.close()
