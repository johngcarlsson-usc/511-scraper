"""GTFS-Realtime transit vehicles as floating traffic probes.

Hundreds of transit agencies publish live bus/train positions for free as
GTFS-Realtime ``VehiclePositions`` (protobuf). Buses share the road network, so
their movement is a live probe of road speed. The technique is well established
(transit AVL correlates strongly with traffic flow); the catches are dwell at
stops, bus-only lanes, and route-only coverage -- handled with filtering below.

Pipeline:

1. ``parse_vehicle_positions(pb_bytes)`` -> list of :class:`VehicleSample`
   (needs the optional ``[gtfs]`` extra: ``pip install 'freetraffic[gtfs]'``).
2. :class:`GtfsRtProbeTracker` -- call ``update(samples)`` each poll; it diffs
   each vehicle against its previous position to produce :class:`ProbeSpeed`
   segments, filtering implausible/dwell samples.
3. Map-match the segments to your Valhalla graph and aggregate per edge
   (:func:`aggregate_by_edge`) to get :class:`~freetraffic.models.LinkSpeed`.

The tracker logic is pure (no network/protobuf) and unit-tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from ..geometry import Geometry, haversine_m
from ..models import LinkSpeed

# GTFS-RT VehicleStopStatus
STOPPED_AT = 1


@dataclass
class VehicleSample:
    vehicle_id: str
    lon: float
    lat: float
    timestamp: int  # epoch seconds
    speed_mps: Optional[float] = None  # reported speed, if any
    bearing: Optional[float] = None
    route_id: Optional[str] = None
    trip_id: Optional[str] = None
    current_status: Optional[int] = None

    @property
    def point(self):
        return (self.lon, self.lat)


@dataclass
class ProbeSpeed:
    vehicle_id: str
    from_point: tuple
    to_point: tuple
    distance_m: float
    dt_s: float
    speed_kph: float
    observed_at: Optional[datetime] = None
    route_id: Optional[str] = None
    edge_id: Optional[int] = None  # filled after map-matching

    @property
    def mid_point(self) -> tuple:
        return (
            (self.from_point[0] + self.to_point[0]) / 2.0,
            (self.from_point[1] + self.to_point[1]) / 2.0,
        )

    def geometry(self) -> Geometry:
        return Geometry("LineString", [list(self.from_point), list(self.to_point)])

    def to_link_speed(self, source_id: str, jurisdiction: Optional[str] = None) -> LinkSpeed:
        return LinkSpeed(
            source_id=source_id,
            jurisdiction=jurisdiction,
            speed_kph=self.speed_kph,
            link_id=str(self.edge_id) if self.edge_id is not None else None,
            observed_at=self.observed_at,
            geometry=self.geometry(),
            raw={"vehicle_id": self.vehicle_id, "route_id": self.route_id,
                 "dt_s": self.dt_s, "distance_m": self.distance_m},
        )


class GtfsRtProbeTracker:
    """Stateful: diff consecutive vehicle positions into per-segment speeds."""

    def __init__(
        self,
        *,
        min_dt_s: float = 8.0,
        max_dt_s: float = 180.0,
        max_speed_kph: float = 130.0,
        min_distance_m: float = 5.0,
        drop_stopped: bool = True,
    ) -> None:
        self.min_dt_s = min_dt_s
        self.max_dt_s = max_dt_s
        self.max_speed_kph = max_speed_kph
        self.min_distance_m = min_distance_m
        self.drop_stopped = drop_stopped
        self._last: Dict[str, VehicleSample] = {}

    def update(self, samples: Sequence[VehicleSample]) -> List[ProbeSpeed]:
        out: List[ProbeSpeed] = []
        for s in samples:
            prev = self._last.get(s.vehicle_id)
            self._last[s.vehicle_id] = s
            if prev is None:
                continue
            probe = self._segment(prev, s)
            if probe is not None:
                out.append(probe)
        return out

    def _segment(self, a: VehicleSample, b: VehicleSample) -> Optional[ProbeSpeed]:
        dt = b.timestamp - a.timestamp
        if dt < self.min_dt_s or dt > self.max_dt_s:
            return None  # stale pairing or duplicate snapshot
        if self.drop_stopped and b.current_status == STOPPED_AT:
            return None  # dwell at a stop is not a road-speed sample
        dist = haversine_m(a.point, b.point)
        if dist < self.min_distance_m:
            # essentially not moving: a real, useful "very slow" observation
            speed_kph = 0.0
        else:
            speed_kph = (dist / dt) * 3.6
        if speed_kph > self.max_speed_kph:
            return None  # GPS jump / bad fix
        return ProbeSpeed(
            vehicle_id=b.vehicle_id,
            from_point=a.point,
            to_point=b.point,
            distance_m=dist,
            dt_s=float(dt),
            speed_kph=speed_kph,
            observed_at=datetime.fromtimestamp(b.timestamp, tz=timezone.utc)
            if b.timestamp
            else None,
            route_id=b.route_id,
        )

    def reset(self) -> None:
        self._last.clear()


def aggregate_by_edge(
    probes: Sequence[ProbeSpeed],
    *,
    source_id: str,
    jurisdiction: Optional[str] = None,
    min_samples: int = 1,
) -> List[LinkSpeed]:
    """Collapse map-matched probes into one median LinkSpeed per edge id.

    Probes must have ``edge_id`` set (i.e. already map-matched). Median is robust
    to the odd bad probe; require ``min_samples`` per edge to trust it.
    """
    buckets: Dict[int, List[ProbeSpeed]] = {}
    for p in probes:
        if p.edge_id is None:
            continue
        buckets.setdefault(p.edge_id, []).append(p)

    out: List[LinkSpeed] = []
    for edge_id, group in buckets.items():
        if len(group) < min_samples:
            continue
        speeds = sorted(g.speed_kph for g in group)
        median = _median(speeds)
        latest = max((g.observed_at for g in group if g.observed_at), default=None)
        out.append(
            LinkSpeed(
                source_id=source_id,
                jurisdiction=jurisdiction,
                speed_kph=median,
                link_id=str(edge_id),
                observed_at=latest,
                geometry=group[0].geometry(),
                confidence=min(1.0, len(group) / 5.0),
                raw={"sample_count": len(group)},
            )
        )
    return out


def _median(values: Sequence[float]) -> float:
    n = len(values)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2.0


def parse_vehicle_positions(pb_bytes: bytes) -> List[VehicleSample]:
    """Parse a GTFS-Realtime protobuf payload into VehicleSamples.

    Requires the optional ``[gtfs]`` extra (``gtfs-realtime-bindings``).
    """
    try:
        from google.transit import gtfs_realtime_pb2
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "GTFS-Realtime parsing needs the [gtfs] extra: "
            "pip install 'freetraffic[gtfs]'"
        ) from exc

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(pb_bytes)
    out: List[VehicleSample] = []
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        if not v.HasField("position"):
            continue
        pos = v.position
        vid = v.vehicle.id if v.HasField("vehicle") and v.vehicle.id else entity.id
        out.append(
            VehicleSample(
                vehicle_id=vid,
                lon=float(pos.longitude),
                lat=float(pos.latitude),
                timestamp=int(v.timestamp) if v.timestamp else 0,
                speed_mps=float(pos.speed) if pos.HasField("speed") else None,
                bearing=float(pos.bearing) if pos.HasField("bearing") else None,
                route_id=v.trip.route_id if v.HasField("trip") else None,
                trip_id=v.trip.trip_id if v.HasField("trip") else None,
                current_status=int(v.current_status) if v.HasField("current_status") else None,
            )
        )
    return out


async def map_match_probes(
    probes: Sequence[ProbeSpeed],
    valhalla_client: Any,
    *,
    costing: str = "auto",
    concurrency: int = 8,
) -> List[ProbeSpeed]:
    """Assign a Valhalla ``edge_id`` to each probe via /trace_attributes.

    Coarse (each probe is a short 2-point segment); good enough to bucket probes
    onto edges for aggregation. Returns the probes that matched at least one edge.
    """
    import asyncio

    sem = asyncio.Semaphore(concurrency)
    matched: List[ProbeSpeed] = []

    async def _one(p: ProbeSpeed) -> None:
        async with sem:
            try:
                data = await valhalla_client.trace_attributes(
                    [p.from_point, p.to_point], costing=costing, attributes=["edge.id"]
                )
            except Exception:  # noqa: BLE001 - skip unmatchable probes
                return
            edges = data.get("edges") or []
            if edges and "id" in edges[0]:
                p.edge_id = int(edges[0]["id"])
                matched.append(p)

    await asyncio.gather(*(_one(p) for p in probes))
    return matched


async def fetch_vehicle_positions(url: str, *, client: Any = None, headers: Optional[dict] = None) -> List[VehicleSample]:
    """Fetch + parse a GTFS-RT VehiclePositions feed (needs httpx + [gtfs])."""
    from ..client import _require_httpx

    httpx = _require_httpx()
    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    try:
        resp = await client.get(url, headers=headers or {})
        resp.raise_for_status()
        return parse_vehicle_positions(resp.content)
    finally:
        if owns:
            await client.aclose()
