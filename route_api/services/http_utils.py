"""
Bounded HTTP responses.

``requests`` timeouts limit the connect time and the gap between received bytes,
not the total duration: a server that trickles its response can hold a request
open indefinitely (observed: one OSRM call took 30 minutes). ``read_with_deadline``
streams the body and enforces an overall deadline and a maximum size.
"""

from __future__ import annotations

import time


class DeadlineExceeded(Exception):
    pass


class ResponseTooLarge(Exception):
    pass


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
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLarge(f"response larger than {max_bytes} bytes")
            if time.monotonic() > deadline:
                raise DeadlineExceeded("response not received before the deadline")
    except (DeadlineExceeded, ResponseTooLarge):
        response.close()
        raise
    response._content = b"".join(chunks)  # lets response.json() work as usual
    response._content_consumed = True
    return response
