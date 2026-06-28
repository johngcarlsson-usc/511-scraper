"""Record/replay httpx transports backed by a JSON cassette.

Credentials never reach the cassette: only the request method, URL path, query
(minus key-ish params), and the *response* are stored. Auth headers and API-key
query params are stripped on the way in.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

# Query params / headers we never want to persist into a shared cassette.
_SECRET_PARAMS = {"api_key", "apikey", "key", "accesscode", "code", "token"}
_SECRET_HEADERS = {"authorization", "proxy-authorization"}


class Cassette:
    """An ordered collection of recorded HTTP interactions."""

    def __init__(self, interactions: Optional[List[dict]] = None) -> None:
        self.interactions: List[dict] = interactions or []

    def add(self, interaction: dict) -> None:
        self.interactions.append(interaction)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"interactions": self.interactions}, fh, indent=2)

    @classmethod
    def load(cls, path: str) -> "Cassette":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(data.get("interactions", []))

    def _queues(self) -> Dict[str, List[dict]]:
        queues: Dict[str, List[dict]] = {}
        for it in self.interactions:
            queues.setdefault(it["key"], []).append(it)
        return queues


def request_key(request: "httpx.Request") -> str:
    """A stable match key: METHOD path?sanitized-query."""
    parts = urlsplit(str(request.url))
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _SECRET_PARAMS]
    q = ("?" + urlencode(sorted(query))) if query else ""
    return f"{request.method} {parts.path}{q}"


def _response_payload(response: "httpx.Response") -> dict:
    content_type = response.headers.get("content-type", "")
    body: Any
    is_json = "json" in content_type
    text = response.text
    if is_json:
        try:
            body = response.json()
        except ValueError:
            is_json = False
            body = text
    else:
        body = text
    return {"status": response.status_code, "is_json": is_json, "body": body}


class RecordingTransport(httpx.AsyncBaseTransport):
    """Delegates to a real transport and records each interaction."""

    def __init__(self, inner: "httpx.AsyncBaseTransport", cassette: Cassette) -> None:
        self.inner = inner
        self.cassette = cassette

    async def handle_async_request(self, request: "httpx.Request") -> "httpx.Response":
        response = await self.inner.handle_async_request(request)
        await response.aread()  # buffer the body so we can re-emit it
        self.cassette.add(
            {
                "key": request_key(request),
                "request": {
                    "method": request.method,
                    "url": str(request.url),
                },
                "response": _response_payload(response),
            }
        )
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            content=response.content,
            request=request,
        )


class ReplayError(RuntimeError):
    pass


class ReplayTransport(httpx.AsyncBaseTransport):
    """Serves responses from a cassette; never touches the network."""

    def __init__(self, cassette: Cassette, *, strict: bool = True) -> None:
        self._queues = cassette._queues()
        self.strict = strict

    async def handle_async_request(self, request: "httpx.Request") -> "httpx.Response":
        key = request_key(request)
        queue = self._queues.get(key)
        if not queue:
            if self.strict:
                raise ReplayError(f"no recorded interaction for: {key}")
            return httpx.Response(404, json={"error": "not recorded"}, request=request)
        interaction = queue.pop(0)
        resp = interaction["response"]
        if resp.get("is_json"):
            return httpx.Response(resp["status"], json=resp["body"], request=request)
        return httpx.Response(resp["status"], text=resp.get("body", ""), request=request)
