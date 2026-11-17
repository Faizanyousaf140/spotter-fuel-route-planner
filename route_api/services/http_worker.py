"""Private HTTP subprocess: JSON stdin, then one JSON header and raw body stdout.

No Django imports, threads, or descendant processes. Credentials remain on the
private stdin pipe, never in process command arguments or error messages.
"""
from __future__ import annotations

import json
import http.client
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib


def main():
    request = json.load(sys.stdin.buffer)
    url, kwargs, headers = request["url"], request["kwargs"], request["headers"]
    if kwargs.get("params"):
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(kwargs["params"], doseq=True)
    body = None
    if "json" in kwargs:
        body = json.dumps(kwargs["json"]).encode()
        headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept-Encoding", "gzip")
    message = urllib.request.Request(url, data=body, headers=headers, method=request["method"])
    context = ssl.create_default_context(cafile=request["cafile"]) if url.startswith("https:") else None
    try:
        try:
            response = urllib.request.urlopen(message, timeout=request["timeout"][0], context=context)
        except urllib.error.HTTPError as error:
            response = error  # HTTP 4xx/5xx bodies still belong to the provider parser.
        with response:
            buffered = response.fp
            if isinstance(buffered, http.client.HTTPResponse):
                buffered = buffered.fp  # urllib HTTPError adds a wrapper.
            sock = getattr(getattr(buffered, "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(request["timeout"][1])
            metadata = {"status": response.getcode(), "headers": dict(response.headers)}
            encoding = response.headers.get("Content-Encoding", "").lower()
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
            chunks, size = [], 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                if decoder is not None:
                    chunk = decoder.decompress(chunk, request["max_bytes"] - size + 1)
                size += len(chunk)
                if size > request["max_bytes"]:
                    metadata, chunks = {"error": "oversized"}, []
                    break
                chunks.append(chunk)
            content = b"".join(chunks)
    except (TimeoutError, socket.timeout):
        metadata, content = {"error": "timeout"}, b""
    except urllib.error.URLError as error:
        metadata, content = {"error": "timeout" if isinstance(error.reason, TimeoutError) else "connection"}, b""
    except (OSError, ValueError, zlib.error):
        metadata, content = {"error": "connection"}, b""
    sys.stdout.buffer.write(json.dumps(metadata).encode() + b"\n" + content)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
