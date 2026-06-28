"""Traffic-aware routing on top of Valhalla.

The pragmatic, no-tile-rebuild approach: take a live
:class:`~freetraffic.store.TrafficSnapshot`, and for each routing request

1. build small ``exclude_polygons`` around currently *closed* roads in the
   origin/destination corridor so Valhalla routes around them, and
2. after routing, annotate the result with the active events that lie along the
   chosen route (so a caller/UI can surface "2 incidents, 1 work zone ahead").

This works against any Valhalla HTTP server with no changes to its tiles. The
deeper integration (live edge *speeds* via ``traffic.tar``, map-matched from
``LinkSpeed`` records) is the roadmap item that needs server-side build access.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..geometry import BoundingBox, decode_polyline, haversine_m
from ..models import EventType, LinkSpeed, TrafficEvent
from ..predict import EtaPrediction, FusionConfig, predict_eta, route_edges_from_trace
from ..store import TrafficSnapshot
from .valhalla import LonLat, ValhallaClient

_METERS_PER_DEG_LAT = 111_320.0


@dataclass
class RouteResult:
    """A routed trip plus the traffic context that shaped/affects it."""

    raw: dict  # the full Valhalla /route response
    events_on_route: List[TrafficEvent] = field(default_factory=list)
    exclusions_applied: int = 0
    summary: Optional[dict] = None
    eta: Optional[EtaPrediction] = None  # fused, traffic-aware ETA (route_with_eta)

    @property
    def length_km(self) -> Optional[float]:
        return (self.summary or {}).get("length")

    @property
    def time_s(self) -> Optional[float]:
        return (self.summary or {}).get("time")

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "exclusions_applied": self.exclusions_applied,
            "events_on_route": [e.to_geojson_feature() for e in self.events_on_route],
            "trip": self.raw.get("trip"),
        }


class TrafficAwareRouter:
    def __init__(self, client: ValhallaClient) -> None:
        self.client = client

    async def route(
        self,
        origin: LonLat,
        destination: LonLat,
        snapshot: Optional[TrafficSnapshot] = None,
        *,
        costing: str = "auto",
        avoid_closures: bool = True,
        closure_buffer_m: float = 35.0,
        on_route_dist_m: float = 120.0,
        corridor_pad_deg: float = 0.25,
        max_exclusions: int = 50,
        units: str = "kilometers",
        options: Optional[dict] = None,
    ) -> RouteResult:
        snapshot = snapshot or TrafficSnapshot()
        bbox = BoundingBox.from_points([origin, destination], pad_deg=corridor_pad_deg)

        exclude_polygons: List[List[List[float]]] = []
        if avoid_closures and bbox is not None:
            closures = closures_in_bbox(snapshot.events, bbox)
            exclude_polygons = exclude_polygons_for(
                closures, buffer_m=closure_buffer_m
            )[:max_exclusions]

        resp = await self.client.route(
            [origin, destination],
            costing=costing,
            exclude_polygons=exclude_polygons or None,
            units=units,
            options=options,
        )

        route_pts = decode_route_points(resp)
        candidates = relevant_events(snapshot.events, bbox)
        on_route = events_near_points(candidates, route_pts, max_dist_m=on_route_dist_m)

        summary = (resp.get("trip") or {}).get("summary")
        return RouteResult(
            raw=resp,
            events_on_route=on_route,
            exclusions_applied=len(exclude_polygons),
            summary=summary,
        )

    async def route_with_eta(
        self,
        origin: LonLat,
        destination: LonLat,
        snapshot: Optional[TrafficSnapshot] = None,
        *,
        costing: str = "auto",
        config: Optional[FusionConfig] = None,
        **route_kwargs: Any,
    ) -> RouteResult:
        """Route (avoiding closures), then re-time it with fused live speeds.

        This is Mode A: works against any Valhalla server with no tile rebuild.
        ``snapshot.speeds`` should already be map-matched to Valhalla edge ids
        (``link_id`` = edge id); use :func:`map_match_link_speeds` first for feeds
        whose speeds carry their own native ids.
        """
        result = await self.route(origin, destination, snapshot, costing=costing, **route_kwargs)
        route_pts = decode_route_points(result.raw)
        if len(route_pts) >= 2:
            trace = await self.client.trace_attributes(
                route_pts, costing=costing,
                attributes=["edge.id", "edge.length", "edge.speed",
                            "edge.begin_shape_index", "edge.end_shape_index"],
            )
            edges = route_edges_from_trace(trace)
            result.eta = predict_eta(edges, snapshot or TrafficSnapshot(), config)
        return result


# --------------------------------------------------------------------------- #
# Pure helpers (no network) -- independently testable.
# --------------------------------------------------------------------------- #

async def map_match_link_speeds(
    speeds: Sequence[LinkSpeed],
    client: ValhallaClient,
    *,
    costing: str = "auto",
    concurrency: int = 8,
) -> List[LinkSpeed]:
    """Set each LinkSpeed's ``link_id`` to a Valhalla edge id via /trace_attributes.

    For feeds whose speeds carry their own native segment ids (IBI511, WSDOT,
    TomTom). Returns the speeds that matched at least one edge. GTFS-RT probe
    speeds are already edge-keyed and don't need this.
    """
    import asyncio

    sem = asyncio.Semaphore(concurrency)
    matched: List[LinkSpeed] = []

    async def _one(s: LinkSpeed) -> None:
        if s.geometry is None:
            return
        pts = [(float(p[0]), float(p[1])) for p in s.geometry.iter_positions() if len(p) >= 2]
        if len(pts) < 2:
            return
        async with sem:
            try:
                trace = await client.trace_attributes(pts, costing=costing, attributes=["edge.id"])
            except Exception:  # noqa: BLE001
                return
        edges = trace.get("edges") or []
        if edges and "id" in edges[0]:
            s.link_id = str(int(edges[0]["id"]))
            matched.append(s)

    await asyncio.gather(*(_one(s) for s in speeds))
    return matched


def relevant_events(
    events: Sequence[TrafficEvent], bbox: Optional[BoundingBox]
) -> List[TrafficEvent]:
    if bbox is None:
        return list(events)
    return [e for e in events if e.geometry and bbox.contains_geometry(e.geometry)]


def closures_in_bbox(
    events: Sequence[TrafficEvent], bbox: BoundingBox
) -> List[TrafficEvent]:
    out = []
    for e in events:
        if not e.geometry:
            continue
        is_closure = e.impact.closed or e.event_type is EventType.CLOSURE
        if is_closure and bbox.contains_geometry(e.geometry):
            out.append(e)
    return out


def exclude_polygons_for(
    events: Sequence[TrafficEvent], *, buffer_m: float
) -> List[List[List[float]]]:
    """A small square ring (in [lon,lat]) around each event's location.

    Valhalla ``exclude_polygons`` is a list of rings; any edge intersecting a
    ring is avoided. A tight box around a closure is enough to push the route
    onto an alternative without globally distorting costs.
    """
    rings: List[List[List[float]]] = []
    for e in events:
        if not e.geometry:
            continue
        pt = e.geometry.representative_point()
        if pt is None:
            continue
        rings.append(_box_ring(pt[0], pt[1], buffer_m))
    return rings


def _box_ring(lon: float, lat: float, buffer_m: float) -> List[List[float]]:
    dlat = buffer_m / _METERS_PER_DEG_LAT
    dlon = buffer_m / (_METERS_PER_DEG_LAT * max(0.1, math.cos(math.radians(lat))))
    return [
        [lon - dlon, lat - dlat],
        [lon + dlon, lat - dlat],
        [lon + dlon, lat + dlat],
        [lon - dlon, lat + dlat],
        [lon - dlon, lat - dlat],
    ]


def decode_route_points(valhalla_response: dict) -> List[Tuple[float, float]]:
    """Decode the (precision-6) shape of every leg into [(lon, lat), ...]."""
    pts: List[Tuple[float, float]] = []
    trip = valhalla_response.get("trip") or {}
    for leg in trip.get("legs") or []:
        shape = leg.get("shape")
        if isinstance(shape, str):
            pts.extend((lon, lat) for lon, lat in decode_polyline(shape, precision=6))
    return pts


def events_near_points(
    events: Sequence[TrafficEvent],
    points: Sequence[Tuple[float, float]],
    *,
    max_dist_m: float,
) -> List[TrafficEvent]:
    """Events whose location is within ``max_dist_m`` of any route point.

    Coarse point-to-vertex proximity -- cheap and good enough for "what's on my
    route"; swap in a proper point-to-polyline distance if you need precision.
    """
    if not points:
        return []
    out = []
    for e in events:
        if not e.geometry:
            continue
        pt = e.geometry.representative_point()
        if pt is None:
            continue
        if any(haversine_m(pt, rp) <= max_dist_m for rp in points):
            out.append(e)
    return out
