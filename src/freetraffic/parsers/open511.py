"""Open511 traffic-event parser.

Open511 (https://open511.org/) is the closest thing to a cross-jurisdiction
standard for traffic *events*. A response is JSON shaped like::

    {
      "events": [ {event}, ... ],
      "pagination": {"next_url": ...},
      "meta": {...}
    }

Each event carries ``event_type``, ``severity``, ``status``, ``geography``
(GeoJSON), ``roads`` and schedule information. This parser is deliberately
tolerant: real feeds drift from the spec, so missing/extra fields never raise.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..geometry import Geometry
from ..models import (
    EventStatus,
    EventType,
    Impact,
    RoadInfo,
    Severity,
    TrafficEvent,
    parse_datetime,
)

_EVENT_TYPE_MAP = {
    "CONSTRUCTION": EventType.CONSTRUCTION,
    "INCIDENT": EventType.INCIDENT,
    "SPECIAL_EVENT": EventType.SPECIAL_EVENT,
    "ROAD_CONDITION": EventType.ROAD_CONDITION,
    "WEATHER_CONDITION": EventType.WEATHER_CONDITION,
}

_SEVERITY_MAP = {
    "MINOR": Severity.MINOR,
    "MODERATE": Severity.MODERATE,
    "MAJOR": Severity.MAJOR,
    "UNKNOWN": Severity.UNKNOWN,
}

_STATUS_MAP = {
    "ACTIVE": EventStatus.ACTIVE,
    "PLANNED": EventStatus.PLANNED,
    "ARCHIVED": EventStatus.ARCHIVED,
}


def parse_open511(
    payload: Dict[str, Any],
    *,
    source_id: str,
    jurisdiction: Optional[str] = None,
) -> List[TrafficEvent]:
    if not isinstance(payload, dict):
        return []
    events = payload.get("events") or []
    out: List[TrafficEvent] = []
    for raw in events:
        if isinstance(raw, dict):
            out.append(_parse_event(raw, source_id, jurisdiction))
    return out


def _parse_event(
    raw: Dict[str, Any], source_id: str, jurisdiction: Optional[str]
) -> TrafficEvent:
    native_id = str(raw.get("id") or raw.get("url") or id(raw))
    roads = [_parse_road(r) for r in (raw.get("roads") or []) if isinstance(r, dict)]
    schedule = raw.get("schedule") or {}

    return TrafficEvent(
        id=f"{source_id}:{native_id}",
        source_id=source_id,
        jurisdiction=jurisdiction,
        event_type=_EVENT_TYPE_MAP.get(
            str(raw.get("event_type", "")).upper(), EventType.UNKNOWN
        ),
        subtypes=[str(s) for s in (raw.get("event_subtypes") or [])],
        severity=_SEVERITY_MAP.get(
            str(raw.get("severity", "")).upper(), Severity.UNKNOWN
        ),
        status=_STATUS_MAP.get(str(raw.get("status", "")).upper(), EventStatus.ACTIVE),
        headline=raw.get("headline") or raw.get("description"),
        description=raw.get("description"),
        roads=roads,
        impact=_impact_from_roads(roads),
        geometry=Geometry.from_geojson(raw.get("geography")),
        created=parse_datetime(raw.get("created")),
        updated=parse_datetime(raw.get("updated")),
        starts=_schedule_bound(schedule, "start"),
        ends=_schedule_bound(schedule, "end"),
        url=raw.get("url"),
        raw=raw,
    )


def _parse_road(r: Dict[str, Any]) -> RoadInfo:
    lanes_total = _as_int(r.get("lanes"))
    lanes_open = _as_int(r.get("lanes_open"))
    lanes_closed = _as_int(r.get("lanes_closed"))
    if lanes_closed is None and lanes_total is not None and lanes_open is not None:
        lanes_closed = max(0, lanes_total - lanes_open)
    return RoadInfo(
        name=r.get("name"),
        direction=r.get("direction"),
        from_location=r.get("from"),
        to_location=r.get("to"),
        lanes_total=lanes_total,
        lanes_closed=lanes_closed,
    )


def _impact_from_roads(roads: List[RoadInfo]) -> Impact:
    impact = Impact()
    for road in roads:
        if road.lanes_total is not None:
            impact.lanes_total = max(impact.lanes_total or 0, road.lanes_total)
        if road.lanes_closed is not None:
            impact.lanes_closed = max(impact.lanes_closed or 0, road.lanes_closed)
    if (
        impact.lanes_total is not None
        and impact.lanes_closed is not None
        and impact.lanes_closed >= impact.lanes_total > 0
    ):
        impact.closed = True
    return impact


def _schedule_bound(schedule: Dict[str, Any], which: str) -> Optional[Any]:
    """Open511 schedules vary; pull a representative start/end datetime."""
    if not isinstance(schedule, dict):
        return None
    intervals = schedule.get("intervals")
    if isinstance(intervals, list) and intervals:
        # intervals are "<start>/<end>" ISO ranges
        first = intervals[0]
        parts = str(first).split("/")
        if which == "start":
            return parse_datetime(parts[0])
        if len(parts) > 1:
            return parse_datetime(parts[1])
    recurring = schedule.get("recurring_schedules")
    if isinstance(recurring, list) and recurring:
        key = "start_date" if which == "start" else "end_date"
        return parse_datetime(recurring[0].get(key))
    return None


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
