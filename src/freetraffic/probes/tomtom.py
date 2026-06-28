"""TomTom Traffic *Flow Segment* lookups (freemium).

TomTom's Traffic API returns real, probe-derived current vs free-flow speed for
a road segment near a point. The freemium tier includes a free daily request
allowance, which is fine for **on-demand enrichment** -- e.g. checking the live
speed of a specific corridor while planning a route.

ToS guard rail: this is intentionally a *live-lookup* helper, not a bulk
scraper. TomTom's terms restrict caching and redistribution of their traffic
data, so:

* don't persist the returned speeds into a shared dataset,
* don't fan this out to crawl a whole network,
* use it to enrich the current request and then discard.

Set ``FT_TOMTOM_API_KEY`` (a freemium key from developer.tomtom.com).
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

from ..geometry import Geometry
from ..models import LinkSpeed

_FLOW_URL = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/{zoom}/json"


def _require_httpx():
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "TomTom lookups need httpx: pip install 'freetraffic[fetch]'"
        ) from exc
    return httpx


class TomTomFlowClient:
    def __init__(self, api_key: Optional[str] = None, *, client: Any = None) -> None:
        self.api_key = api_key or os.environ.get("FT_TOMTOM_API_KEY")
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def flow_segment(
        self, lon: float, lat: float, *, zoom: int = 10, source_id: str = "tomtom-flow"
    ) -> Optional[LinkSpeed]:
        """Live speed for the road segment nearest (lon, lat). None if no data.

        Per ToS: use this result for the current request only; do not store or
        redistribute it.
        """
        if not self.configured:
            raise RuntimeError("TomTom API key not set (FT_TOMTOM_API_KEY)")
        httpx = _require_httpx()
        owns = self._client is None
        client = self._client or httpx.AsyncClient(timeout=20.0)
        try:
            resp = await client.get(
                _FLOW_URL.format(zoom=zoom),
                params={"point": f"{lat},{lon}", "unit": "KMPH", "key": self.api_key},
            )
            resp.raise_for_status()
            data = resp.json()
        finally:
            if owns:
                await client.aclose()
        return _flow_to_link_speed(data, source_id)


def _flow_to_link_speed(data: dict, source_id: str) -> Optional[LinkSpeed]:
    seg = (data or {}).get("flowSegmentData")
    if not isinstance(seg, dict):
        return None
    current = seg.get("currentSpeed")
    if current is None:
        return None
    coords = (seg.get("coordinates") or {}).get("coordinate") or []
    geom = None
    line = [[c["longitude"], c["latitude"]] for c in coords if "longitude" in c]
    if len(line) >= 2:
        geom = Geometry("LineString", line)
    elif line:
        geom = Geometry.point(line[0][0], line[0][1])
    return LinkSpeed(
        source_id=source_id,
        speed_kph=float(current),
        freeflow_kph=_to_float(seg.get("freeFlowSpeed")),
        confidence=_to_float(seg.get("confidence")),
        geometry=geom,
        raw=seg,
    )


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
