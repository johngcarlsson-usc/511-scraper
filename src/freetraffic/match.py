"""Map-matching bridge: canonical geometry -> routing-engine graph ids.

Live traffic only helps a router if each observation is attached to the
router's *own* graph identifiers. This module bridges that gap for Valhalla
using its ``/trace_attributes`` endpoint, which snaps a polyline to the graph
and returns the traversed edges (including their ``way_id`` and Valhalla edge
id). The resulting edge ids feed :mod:`freetraffic.export.valhalla`.

OSRM has an analogous ``/match`` service that returns ``nodes`` per matched
leg; wiring that into OSRM node-pair segment rows is left as a documented
follow-up (the shape mirrors the Valhalla function here).
"""

from __future__ import annotations

from typing import Any, List, Optional

from .geometry import Geometry


def geometry_to_shape(geom: Geometry) -> List[dict]:
    """Convert a LineString/Point geometry to Valhalla trace ``shape`` points."""
    points: List[dict] = []
    for pos in geom.iter_positions():
        if len(pos) >= 2:
            points.append({"lon": float(pos[0]), "lat": float(pos[1])})
    return points


async def match_geometry_to_valhalla_edges(
    geom: Geometry,
    *,
    base_url: str,
    costing: str = "auto",
    client: Any = None,
) -> List[int]:
    """Return Valhalla edge ids covered by ``geom`` via /trace_attributes.

    ``base_url`` is your Valhalla server, e.g. ``http://localhost:8002``.
    Requires at least two shape points; returns an empty list otherwise.
    """
    shape = geometry_to_shape(geom)
    if len(shape) < 2:
        return []

    from .client import _require_httpx

    httpx = _require_httpx()
    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        body = {
            "shape": shape,
            "costing": costing,
            "shape_match": "map_snap",
            "filters": {"attributes": ["edge.id", "edge.way_id"], "action": "include"},
        }
        resp = await client.post(f"{base_url.rstrip('/')}/trace_attributes", json=body)
        resp.raise_for_status()
        data = resp.json()
        edges = data.get("edges") or []
        return [int(e["id"]) for e in edges if "id" in e]
    finally:
        if owns:
            await client.aclose()
