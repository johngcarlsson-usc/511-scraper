"""Fuse all sources into per-edge speeds and a predicted travel time.

This is the heart of the package. Given the edges of a route (each with a base
free-flow speed and length) and a :class:`~freetraffic.store.TrafficSnapshot`,
it computes an effective speed per edge using an explicit precedence and sums
the per-edge times into an ETA.

Precedence (highest first):

1. **Hard constraint** -- an edge with a full closure on it is impassable.
2. **Measured speed** -- if a probe / DOT travel-time / vendor / TomTom speed is
   matched to the edge, use it. Measurements already embody whatever incident or
   weather is happening, so we do *not* stack modeled penalties on top.
3. **Modeled fallback** -- otherwise start from the base speed and apply:
   incident/lane penalty, a posted reduced-speed-limit cap, and a weather
   multiplier for edges inside an active weather alert.

Everything is in km/h and metres. The functions here are pure (no network); a
thin adapter (``route_edges_from_trace``) turns a Valhalla ``/trace_attributes``
response into the ``RouteEdge`` list this consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..geometry import Geometry, decode_polyline
from ..models import EventType, LinkSpeed, Severity, TrafficEvent
from ..store import TrafficSnapshot


@dataclass
class RouteEdge:
    edge_id: int
    length_m: float
    base_speed_kph: float
    geometry: Optional[Geometry] = None

    def midpoint(self) -> Optional[Tuple[float, float]]:
        return self.geometry.representative_point() if self.geometry else None


@dataclass
class EdgePrediction:
    edge_id: int
    length_m: float
    base_speed_kph: float
    predicted_speed_kph: float
    source: str  # "measured:<src>" | "modeled" | "base" | "closed"
    impassable: bool = False
    factors: Dict[str, float] = field(default_factory=dict)

    @property
    def base_time_s(self) -> float:
        return _time_s(self.length_m, self.base_speed_kph)

    @property
    def predicted_time_s(self) -> float:
        return _time_s(self.length_m, self.predicted_speed_kph)


@dataclass
class EtaPrediction:
    edges: List[EdgePrediction]

    @property
    def base_time_s(self) -> float:
        return sum(e.base_time_s for e in self.edges)

    @property
    def predicted_time_s(self) -> float:
        return sum(e.predicted_time_s for e in self.edges)

    @property
    def delay_s(self) -> float:
        return self.predicted_time_s - self.base_time_s

    @property
    def length_m(self) -> float:
        return sum(e.length_m for e in self.edges)

    @property
    def has_impassable(self) -> bool:
        return any(e.impassable for e in self.edges)

    def to_dict(self) -> dict:
        return {
            "base_time_s": round(self.base_time_s, 1),
            "predicted_time_s": round(self.predicted_time_s, 1),
            "delay_s": round(self.delay_s, 1),
            "length_m": round(self.length_m, 1),
            "has_impassable": self.has_impassable,
            "measured_edge_count": sum(1 for e in self.edges if e.source.startswith("measured")),
            "edge_count": len(self.edges),
        }


@dataclass
class FusionConfig:
    # speed floor for an "impassable" edge so ETA stays finite but huge (km/h)
    impassable_floor_kph: float = 3.0
    # modeled incident penalty multipliers by severity (no lane info)
    severity_factor: Dict[Severity, float] = field(
        default_factory=lambda: {
            Severity.MAJOR: 0.40,
            Severity.MODERATE: 0.65,
            Severity.MINOR: 0.85,
            Severity.UNKNOWN: 0.90,
        }
    )
    # weather multipliers by keyword found in the alert name/subtypes
    weather_factor: Dict[str, float] = field(
        default_factory=lambda: {
            "blizzard": 0.5, "ice": 0.6, "freezing": 0.6, "winter": 0.7,
            "snow": 0.7, "flood": 0.5, "fog": 0.8, "dust": 0.7,
            "hurricane": 0.5, "tropical": 0.7, "tornado": 0.5,
            "storm": 0.8, "wind": 0.9,
        }
    )
    lane_min_factor: float = 0.30      # floor for lane-fraction based slowdown
    event_match_dist_m: float = 80.0   # how close a point/line event must be to an edge
    measured_max_age_s: Optional[float] = 900.0  # ignore measurements older than this
    min_speed_kph: float = 1.0


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def predict_eta(
    edges: Sequence[RouteEdge],
    snapshot: TrafficSnapshot,
    config: Optional[FusionConfig] = None,
    *,
    now: Optional[datetime] = None,
) -> EtaPrediction:
    return EtaPrediction(predict_edges(edges, snapshot, config, now=now))


def predict_edges(
    edges: Sequence[RouteEdge],
    snapshot: TrafficSnapshot,
    config: Optional[FusionConfig] = None,
    *,
    now: Optional[datetime] = None,
) -> List[EdgePrediction]:
    config = config or FusionConfig()
    now = now or datetime.now(timezone.utc)
    measured = _measured_by_edge(snapshot.speeds, config, now)

    preds: List[EdgePrediction] = []
    for edge in edges:
        preds.append(_predict_one(edge, snapshot.events, measured, config))
    return preds


# --------------------------------------------------------------------------- #
# Per-edge logic
# --------------------------------------------------------------------------- #

def _predict_one(
    edge: RouteEdge,
    events: Sequence[TrafficEvent],
    measured: Dict[int, LinkSpeed],
    config: FusionConfig,
) -> EdgePrediction:
    base = max(config.min_speed_kph, edge.base_speed_kph)
    affecting = _events_affecting(edge, events, config)

    # 1. hard constraint: full closure
    if any(_is_closure(e) for e in affecting):
        return EdgePrediction(
            edge_id=edge.edge_id, length_m=edge.length_m, base_speed_kph=base,
            predicted_speed_kph=config.impassable_floor_kph, source="closed",
            impassable=True, factors={"closure": 0.0},
        )

    reduced_cap = _reduced_limit_cap(affecting)

    # 2. measured speed wins
    m = measured.get(edge.edge_id)
    if m is not None:
        speed = m.speed_kph
        if reduced_cap is not None:
            speed = min(speed, reduced_cap)
        return EdgePrediction(
            edge_id=edge.edge_id, length_m=edge.length_m, base_speed_kph=base,
            predicted_speed_kph=max(config.min_speed_kph, speed),
            source=f"measured:{m.source_id}",
            factors={"measured_kph": round(m.speed_kph, 1)},
        )

    # 3. modeled fallback from base speed
    factors: Dict[str, float] = {}
    speed = base
    inc_factor = _incident_factor(affecting, config)
    if inc_factor < 1.0:
        factors["incident"] = round(inc_factor, 3)
        speed *= inc_factor
    wx_factor = _weather_factor(affecting, config)
    if wx_factor < 1.0:
        factors["weather"] = round(wx_factor, 3)
        speed *= wx_factor
    if reduced_cap is not None and reduced_cap < speed:
        factors["reduced_limit_cap_kph"] = round(reduced_cap, 1)
        speed = reduced_cap

    source = "modeled" if factors else "base"
    return EdgePrediction(
        edge_id=edge.edge_id, length_m=edge.length_m, base_speed_kph=base,
        predicted_speed_kph=max(config.min_speed_kph, speed),
        source=source, factors=factors,
    )


def _measured_by_edge(
    speeds: Sequence[LinkSpeed], config: FusionConfig, now: datetime
) -> Dict[int, LinkSpeed]:
    """Index measured speeds by edge id, keeping the best per edge.

    A LinkSpeed participates only if its ``link_id`` is an integer edge id (i.e.
    it has been map-matched to the routing graph). Best = highest confidence,
    then most recent; stale measurements are dropped.
    """
    best: Dict[int, LinkSpeed] = {}
    for s in speeds:
        edge_id = _as_edge_id(s.link_id)
        if edge_id is None:
            continue
        if config.measured_max_age_s is not None and s.observed_at is not None:
            age = (now - s.observed_at).total_seconds()
            if age > config.measured_max_age_s:
                continue
        cur = best.get(edge_id)
        if cur is None or _measure_rank(s) > _measure_rank(cur):
            best[edge_id] = s
    return best


def _measure_rank(s: LinkSpeed) -> tuple:
    conf = s.confidence if s.confidence is not None else 0.5
    ts = s.observed_at.timestamp() if s.observed_at else 0.0
    return (conf, ts)


def _events_affecting(
    edge: RouteEdge, events: Sequence[TrafficEvent], config: FusionConfig
) -> List[TrafficEvent]:
    mid = edge.midpoint()
    if mid is None:
        return []
    out: List[TrafficEvent] = []
    for e in events:
        if e.geometry is None:
            continue
        if e.geometry.type in ("Polygon", "MultiPolygon"):
            if _point_in_polygon(mid, e.geometry):
                out.append(e)
        elif _near_geometry(mid, e.geometry, config.event_match_dist_m):
            out.append(e)
    return out


def _incident_factor(events: Sequence[TrafficEvent], config: FusionConfig) -> float:
    factor = 1.0
    for e in events:
        if e.event_type is EventType.WEATHER_CONDITION:
            continue
        frac = e.impact.lane_fraction_open
        if frac is not None:
            f = max(config.lane_min_factor, frac)
        else:
            f = config.severity_factor.get(e.severity, 1.0)
        factor = min(factor, f)
    return factor


def _weather_factor(events: Sequence[TrafficEvent], config: FusionConfig) -> float:
    factor = 1.0
    for e in events:
        if e.event_type is not EventType.WEATHER_CONDITION:
            continue
        text = " ".join([e.headline or ""] + e.subtypes).lower()
        for keyword, f in config.weather_factor.items():
            if keyword in text:
                factor = min(factor, f)
    return factor


def _reduced_limit_cap(events: Sequence[TrafficEvent]) -> Optional[float]:
    caps = [e.impact.reduced_speed_kph for e in events if e.impact.reduced_speed_kph]
    return min(caps) if caps else None


def _is_closure(e: TrafficEvent) -> bool:
    return e.impact.closed or e.event_type is EventType.CLOSURE


# --------------------------------------------------------------------------- #
# Valhalla trace adapter + geometry helpers
# --------------------------------------------------------------------------- #

def route_edges_from_trace(trace: dict) -> List[RouteEdge]:
    """Convert a Valhalla /trace_attributes response into RouteEdges.

    Uses each edge's ``id``, ``length`` (km), ``speed`` (kph) and
    ``begin_shape_index``/``end_shape_index`` against the decoded ``shape``
    (polyline6) to attach per-edge geometry.
    """
    shape_pts = decode_polyline(trace.get("shape", ""), precision=6) if trace.get("shape") else []
    out: List[RouteEdge] = []
    for e in trace.get("edges") or []:
        if "id" not in e:
            continue
        geom = None
        bi, ei = e.get("begin_shape_index"), e.get("end_shape_index")
        if shape_pts and isinstance(bi, int) and isinstance(ei, int) and ei >= bi:
            seg = shape_pts[bi : ei + 1]
            if len(seg) >= 2:
                geom = Geometry("LineString", [list(p) for p in seg])
            elif seg:
                geom = Geometry.point(seg[0][0], seg[0][1])
        out.append(
            RouteEdge(
                edge_id=int(e["id"]),
                length_m=float(e.get("length", 0.0)) * 1000.0,
                base_speed_kph=float(e.get("speed", 0.0)),
                geometry=geom,
            )
        )
    return out


def _as_edge_id(link_id: Any) -> Optional[int]:
    if link_id is None:
        return None
    try:
        return int(link_id)
    except (TypeError, ValueError):
        return None


def _near_geometry(pt: Tuple[float, float], geom: Geometry, max_dist_m: float) -> bool:
    from ..geometry import haversine_m

    for pos in geom.iter_positions():
        if len(pos) >= 2 and haversine_m(pt, (float(pos[0]), float(pos[1]))) <= max_dist_m:
            return True
    return False


def _point_in_polygon(pt: Tuple[float, float], geom: Geometry) -> bool:
    rings_sets = []
    if geom.type == "Polygon":
        rings_sets = [geom.coordinates]
    elif geom.type == "MultiPolygon":
        rings_sets = geom.coordinates
    else:
        return False
    for polygon in rings_sets:
        if not polygon:
            continue
        if _ray_cast(pt, polygon[0]):  # outer ring; holes ignored (rare for alerts)
            return True
    return False


def _ray_cast(pt: Tuple[float, float], ring: Sequence[Sequence[float]]) -> bool:
    x, y = pt
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _time_s(length_m: float, speed_kph: float) -> float:
    speed = max(0.1, speed_kph)
    return length_m / (speed * (1000.0 / 3600.0))
