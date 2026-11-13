"""Tiny HTTP client on the standard library, so customers need no pip install."""
from __future__ import annotations

import json as _json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message


class RequestException(Exception):
    pass


class Response:
    def __init__(self, status: int, headers: Message, body: bytes, url: str):
        self.status_code = status
        self._headers = headers
        self.content = body
        self.url = url

    @property
    def headers(self):
        return {k.lower(): v for k, v in self._headers.items()} if self._headers else {}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self):
        return _json.loads(self.content or b"null")

    @property
    def links(self) -> dict:
        out = {}
        for part in (self.headers.get("link") or "").split(","):
            m = re.match(r'\s*<([^>]+)>\s*;\s*rel="([^"]+)"', part)
            if m:
                out[m.group(2)] = {"url": m.group(1)}
        return out


class Session:
    def __init__(self):
        self.headers: dict[str, str] = {}
        self._ctx = ssl.create_default_context()

    def request(self, method: str, url: str, params: dict | None = None, json=None,
                timeout: int = 60) -> Response:
        if params:
            q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{q}"
        data = None
        headers = dict(self.headers)
        if json is not None:
            data = _json.dumps(json).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as r:
                return Response(r.status, r.headers, r.read(), url)
        except urllib.error.HTTPError as e:
            return Response(e.code, e.headers, e.read() or b"", url)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise RequestException(str(e)) from e
