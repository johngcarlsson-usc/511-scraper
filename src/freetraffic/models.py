"""Canonical data model for free traffic data.

Every source (Open511 event feeds, WZDx work-zone feeds, sensor/probe speed
feeds, ...) is normalized into the two record types below:

* :class:`TrafficEvent` -- a discrete, located event that affects the road
  network: an incident, closure, construction/work zone, special event, weather
  or road condition. This is what the overwhelming majority of state 511 / WZDx
  feeds actually publish. Events become *restrictions and penalties* on routing
  graph edges (avoid this edge, drop its speed, block it entirely).

* :class:`LinkSpeed` -- an observed/estimated speed on a stretch of road at a
  point in time. This is the scarcer, more valuable signal (sensor loops, probe
  data) that lets a router compute genuinely *traffic-aware travel times*.

Units are normalized: speeds are stored in **km/h** internally. Timestamps are
timezone-aware :class:`datetime` objects (UTC where the source is ambiguous).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .geometry import Geometry

MPH_TO_KPH = 1.609_344


def parse_datetime(value: Any) -> Optional[datetime]:
    """Best-effort parse of the assorted timestamp formats feeds emit."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Heuristic: treat large values as epoch milliseconds.
        ts = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    # Normalize a trailing Z to an explicit UTC offset for fromisoformat.
    candidate = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class EventType(str, enum.Enum):
    """Canonical event categories (superset of Open511 + WZDx vocabularies)."""

    CONSTRUCTION = "construction"
    INCIDENT = "incident"
    SPECIAL_EVENT = "special_event"
    ROAD_CONDITION = "road_condition"
    WEATHER_CONDITION = "weather_condition"
    CLOSURE = "closure"
    RESTRICTION = "restriction"
    UNKNOWN = "unknown"


class Severity(str, enum.Enum):
    UNKNOWN = "unknown"
    MINOR = "minor"
    MODERATE = "moderate"
    MAJOR = "major"


class EventStatus(str, enum.Enum):
    ACTIVE = "active"
    PLANNED = "planned"
    ARCHIVED = "archived"


@dataclass
class RoadInfo:
    """A road segment referenced by an event."""

    name: Optional[str] = None
    direction: Optional[str] = None
    from_location: Optional[str] = None
    to_location: Optional[str] = None
    lanes_total: Optional[int] = None
    lanes_closed: Optional[int] = None
    reduced_speed_kph: Optional[float] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Impact:
    """The routing-relevant effect of an event."""

    closed: bool = False
    lanes_closed: Optional[int] = None
    lanes_total: Optional[int] = None
    reduced_speed_kph: Optional[float] = None

    @property
    def lane_fraction_open(self) -> Optional[float]:
        if self.closed:
            return 0.0
        if self.lanes_total and self.lanes_closed is not None:
            if self.lanes_total <= 0:
                return None
            return max(0.0, (self.lanes_total - self.lanes_closed) / self.lanes_total)
        return None

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if v is not None}
        frac = self.lane_fraction_open
        if frac is not None:
            d["lane_fraction_open"] = round(frac, 3)
        return d


@dataclass
class TrafficEvent:
    """A normalized traffic event from any source."""

    id: str  # globally unique, namespaced as "<source_id>:<native_id>"
    source_id: str
    jurisdiction: Optional[str] = None  # e.g. a US state code "CA"
    event_type: EventType = EventType.UNKNOWN
    subtypes: List[str] = field(default_factory=list)
    severity: Severity = Severity.UNKNOWN
    status: EventStatus = EventStatus.ACTIVE
    headline: Optional[str] = None
    description: Optional[str] = None
    roads: List[RoadInfo] = field(default_factory=list)
    impact: Impact = field(default_factory=Impact)
    geometry: Optional[Geometry] = None
    created: Optional[datetime] = None
    updated: Optional[datetime] = None
    starts: Optional[datetime] = None
    ends: Optional[datetime] = None
    url: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def to_geojson_feature(self, include_raw: bool = False) -> dict:
        props: Dict[str, Any] = {
            "id": self.id,
            "source_id": self.source_id,
            "jurisdiction": self.jurisdiction,
            "event_type": self.event_type.value,
            "subtypes": self.subtypes,
            "severity": self.severity.value,
            "status": self.status.value,
            "headline": self.headline,
            "description": self.description,
            "roads": [r.to_dict() for r in self.roads],
            "impact": self.impact.to_dict(),
            "created": _iso(self.created),
            "updated": _iso(self.updated),
            "starts": _iso(self.starts),
            "ends": _iso(self.ends),
            "url": self.url,
        }
        if include_raw:
            props["raw"] = self.raw
        props = {k: v for k, v in props.items() if v not in (None, [], {})}
        return {
            "type": "Feature",
            "geometry": self.geometry.to_geojson() if self.geometry else None,
            "properties": props,
        }


@dataclass
class LinkSpeed:
    """An observed speed on a road link at a point in time."""

    source_id: str
    speed_kph: float
    observed_at: Optional[datetime] = None
    link_id: Optional[str] = None  # source-native link/segment identifier
    jurisdiction: Optional[str] = None
    roadway: Optional[str] = None
    direction: Optional[str] = None
    freeflow_kph: Optional[float] = None
    geometry: Optional[Geometry] = None
    confidence: Optional[float] = None  # 0..1 if the source reports it
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def congestion_ratio(self) -> Optional[float]:
        """speed / freeflow; <1 means slower than free-flow (congested)."""
        if self.freeflow_kph and self.freeflow_kph > 0:
            return max(0.0, min(1.0, self.speed_kph / self.freeflow_kph))
        return None

    def to_geojson_feature(self) -> dict:
        props = {
            "source_id": self.source_id,
            "link_id": self.link_id,
            "jurisdiction": self.jurisdiction,
            "roadway": self.roadway,
            "direction": self.direction,
            "speed_kph": round(self.speed_kph, 2),
            "freeflow_kph": self.freeflow_kph,
            "congestion_ratio": self.congestion_ratio,
            "observed_at": _iso(self.observed_at),
            "confidence": self.confidence,
        }
        props = {k: v for k, v in props.items() if v is not None}
        return {
            "type": "Feature",
            "geometry": self.geometry.to_geojson() if self.geometry else None,
            "properties": props,
        }


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None
