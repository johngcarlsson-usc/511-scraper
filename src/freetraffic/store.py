"""Collect, de-duplicate and serialise normalized records.

A :class:`TrafficSnapshot` is the unit you hand downstream: a point-in-time set
of events (and, where available, link speeds) ready to serialise as GeoJSON or
feed into the routing exporters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from .geometry import Geometry
from .models import (
    EventStatus,
    EventType,
    Impact,
    LinkSpeed,
    RoadInfo,
    Severity,
    TrafficEvent,
    parse_datetime,
)


@dataclass
class TrafficSnapshot:
    events: List[TrafficEvent] = field(default_factory=list)
    speeds: List[LinkSpeed] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)
    generated_at: Optional[datetime] = None

    def dedupe(self) -> "TrafficSnapshot":
        """Drop duplicate events, keeping the most recently updated copy.

        Different feeds (e.g. a state Open511 feed and that state's WZDx feed)
        routinely report the same closure. We key on the namespaced id first,
        then fall back to (event_type, representative point, headline).
        """
        best: Dict[object, TrafficEvent] = {}
        for ev in self.events:
            key = _event_key(ev)
            cur = best.get(key)
            if cur is None or _newer(ev, cur):
                best[key] = ev
        self.events = list(best.values())
        return self

    def add_events(self, events: Iterable[TrafficEvent]) -> None:
        self.events.extend(events)

    def add_speeds(self, speeds: Iterable[LinkSpeed]) -> None:
        self.speeds.extend(speeds)

    def to_geojson(self, include_raw: bool = False) -> dict:
        features = [e.to_geojson_feature(include_raw=include_raw) for e in self.events]
        features += [s.to_geojson_feature() for s in self.speeds]
        return {
            "type": "FeatureCollection",
            "metadata": {
                "generated_at": (self.generated_at or _now()).isoformat(),
                "event_count": len(self.events),
                "speed_count": len(self.speeds),
                "errors": self.errors,
            },
            "features": features,
        }

    def dumps(self, *, indent: Optional[int] = None, include_raw: bool = False) -> str:
        return json.dumps(self.to_geojson(include_raw=include_raw), indent=indent)

    @classmethod
    def from_geojson(cls, obj: dict) -> "TrafficSnapshot":
        """Reconstruct a snapshot from GeoJSON this package emitted.

        Round-trips the routing-relevant fields (geometry, type, impact,
        timing); ``raw`` and exotic fields are not restored.
        """
        snap = cls()
        for feat in obj.get("features", []):
            props = feat.get("properties") or {}
            geom = Geometry.from_geojson(feat.get("geometry"))
            if "speed_kph" in props:
                snap.speeds.append(
                    LinkSpeed(
                        source_id=props.get("source_id", "?"),
                        speed_kph=float(props["speed_kph"]),
                        freeflow_kph=props.get("freeflow_kph"),
                        link_id=props.get("link_id"),
                        jurisdiction=props.get("jurisdiction"),
                        roadway=props.get("roadway"),
                        direction=props.get("direction"),
                        observed_at=parse_datetime(props.get("observed_at")),
                        geometry=geom,
                    )
                )
                continue
            impact_d = props.get("impact") or {}
            snap.events.append(
                TrafficEvent(
                    id=props.get("id", ""),
                    source_id=props.get("source_id", "?"),
                    jurisdiction=props.get("jurisdiction"),
                    event_type=_enum(EventType, props.get("event_type"), EventType.UNKNOWN),
                    subtypes=props.get("subtypes", []),
                    severity=_enum(Severity, props.get("severity"), Severity.UNKNOWN),
                    status=_enum(EventStatus, props.get("status"), EventStatus.ACTIVE),
                    headline=props.get("headline"),
                    description=props.get("description"),
                    impact=Impact(
                        closed=bool(impact_d.get("closed", False)),
                        lanes_closed=impact_d.get("lanes_closed"),
                        lanes_total=impact_d.get("lanes_total"),
                        reduced_speed_kph=impact_d.get("reduced_speed_kph"),
                    ),
                    geometry=geom,
                    created=parse_datetime(props.get("created")),
                    updated=parse_datetime(props.get("updated")),
                    starts=parse_datetime(props.get("starts")),
                    ends=parse_datetime(props.get("ends")),
                    url=props.get("url"),
                )
            )
        return snap


def _enum(enum_cls, value, default):
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


def _event_key(ev: TrafficEvent) -> object:
    if ev.id:
        # Strip the source prefix so the same native id from two mirror feeds
        # of one jurisdiction collapses, but keep jurisdiction to avoid cross
        # collisions.
        native = ev.id.split(":", 1)[-1]
        return ("id", ev.jurisdiction, native)
    point = ev.geometry.representative_point() if ev.geometry else None
    rounded = (round(point[0], 4), round(point[1], 4)) if point else None
    return ("fuzzy", ev.event_type, rounded, ev.headline)


def _newer(a: TrafficEvent, b: TrafficEvent) -> bool:
    ta = a.updated or a.created
    tb = b.updated or b.created
    if ta and tb:
        return ta > tb
    return bool(ta) and not tb


def _now() -> datetime:
    return datetime.now(timezone.utc)
