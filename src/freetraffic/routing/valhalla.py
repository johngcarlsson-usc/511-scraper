"""Async client for a Valhalla routing server.

Handles the practical bits of talking to a real deployment: HTTP Basic auth, the
``ngrok-skip-browser-warning`` header (for ngrok-tunnelled servers), retries,
and the common endpoints (``/route``, ``/locate``, ``/trace_attributes``,
``/status``). Credentials are read from the environment via
:meth:`ValhallaClient.from_env` so they never live in code.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 3

# (lon, lat) or {"lat":..,"lon":..}
LonLat = Tuple[float, float]


def _require_httpx():
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "Routing requires httpx. Install with: pip install 'freetraffic[fetch]'"
        ) from exc
    return httpx


class ValhallaClient:
    def __init__(
        self,
        base_url: str,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        ngrok_skip: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
        client: Any = None,
    ) -> None:
        # Permissive: an empty base_url is allowed at construction (e.g. when
        # env vars are unset) and validated when a request is actually made, so
        # callers can give a friendly message instead of catching a constructor error.
        self.base_url = (base_url or "").rstrip("/")
        self.username = username
        self.password = password
        self.ngrok_skip = ngrok_skip
        self.timeout = timeout
        self._client = client  # injectable for testing (e.g. httpx.MockTransport)

    @classmethod
    def from_env(cls, **overrides: Any) -> "ValhallaClient":
        """Build from FT_VALHALLA_URL / _USER / _PASS / _NGROK env vars."""
        return cls(
            base_url=overrides.pop("base_url", os.environ.get("FT_VALHALLA_URL", "")),
            username=overrides.pop("username", os.environ.get("FT_VALHALLA_USER")),
            password=overrides.pop("password", os.environ.get("FT_VALHALLA_PASS")),
            ngrok_skip=overrides.pop(
                "ngrok_skip", os.environ.get("FT_VALHALLA_NGROK", "") not in ("", "0")
            ),
            **overrides,
        )

    # -- internals --------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.ngrok_skip:
            headers["ngrok-skip-browser-warning"] = "1"
        return headers

    def _auth(self):
        httpx = _require_httpx()
        if self.username is not None and self.password is not None:
            return httpx.BasicAuth(self.username, self.password)
        return None

    def _check_base(self) -> None:
        if not self.base_url:
            raise RuntimeError(
                "Valhalla base_url is not set (FT_VALHALLA_URL)."
            )

    async def _post(self, path: str, body: dict) -> dict:
        import asyncio

        self._check_base()
        httpx = _require_httpx()
        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        try:
            last: Optional[Exception] = None
            for attempt in range(DEFAULT_RETRIES):
                try:
                    resp = await client.post(
                        f"{self.base_url}{path}",
                        json=body,
                        headers=self._headers(),
                        auth=self._auth(),
                    )
                    resp.raise_for_status()
                    return resp.json()
                except (httpx.HTTPError, ValueError) as exc:
                    last = exc
                    if attempt < DEFAULT_RETRIES - 1:
                        await asyncio.sleep(2 ** attempt)
            raise RuntimeError(f"Valhalla {path} failed after retries: {last}")
        finally:
            if owns:
                await client.aclose()

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        self._check_base()
        httpx = _require_httpx()
        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        try:
            resp = await client.get(
                f"{self.base_url}{path}",
                params=params,
                headers=self._headers(),
                auth=self._auth(),
            )
            resp.raise_for_status()
            return resp.json()
        finally:
            if owns:
                await client.aclose()

    # -- endpoints --------------------------------------------------------
    async def status(self, verbose: bool = False) -> dict:
        return await self._get("/status", {"verbose": str(verbose).lower()})

    async def route(
        self,
        locations: Sequence[LonLat],
        *,
        costing: str = "auto",
        exclude_polygons: Optional[List[List[List[float]]]] = None,
        exclude_locations: Optional[Sequence[LonLat]] = None,
        units: str = "kilometers",
        options: Optional[dict] = None,
    ) -> dict:
        body: Dict[str, Any] = {
            "locations": [{"lon": lon, "lat": lat} for lon, lat in locations],
            "costing": costing,
            "directions_options": {"units": units},
        }
        if exclude_polygons:
            body["exclude_polygons"] = exclude_polygons
        if exclude_locations:
            body["exclude_locations"] = [
                {"lon": lon, "lat": lat} for lon, lat in exclude_locations
            ]
        if options:
            body.update(options)
        return await self._post("/route", body)

    async def locate(
        self, locations: Sequence[LonLat], *, costing: str = "auto"
    ) -> dict:
        body = {
            "locations": [{"lon": lon, "lat": lat} for lon, lat in locations],
            "costing": costing,
        }
        return await self._post("/locate", body)

    async def trace_attributes(
        self,
        shape: Sequence[LonLat],
        *,
        costing: str = "auto",
        attributes: Optional[Sequence[str]] = None,
    ) -> dict:
        body = {
            "shape": [{"lon": lon, "lat": lat} for lon, lat in shape],
            "costing": costing,
            "shape_match": "map_snap",
            "filters": {
                "attributes": list(attributes or ["edge.id", "edge.way_id"]),
                "action": "include",
            },
        }
        return await self._post("/trace_attributes", body)
