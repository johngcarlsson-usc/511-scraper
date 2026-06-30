"""MassDOT roadway events parser (Everbridge ERS XML, public/keyless).

MassDOT publishes incidents/events at ``http://events.massdot.evbg.net/`` as
``<ERSEvents><Events><Event>`` XML. Times are local Eastern; lat/lon are often
blank (especially for non-point/weather events). No auth, full list per call.

(MassDOT also has a WZDx v3.1 GeoJSON feed -- use the existing ``wzdx`` parser
for that -- and a key-gated GoTime travel-times API where speed is masked for
external users, so it's not wired here.)
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import List, Optional

from ..geometry import Geometry
from ..models import (
    EventStatus,
    EventType,
    RoadInfo,
    TrafficEvent,
    parse_datetime,
)

_TYPE_MAP = {
    "traffic incidents": EventType.INCIDENT,
    "incident": EventType.INCIDENT,
    "crash": EventType.INCIDENT,
    "construction": EventType.CONSTRUCTION,
    "roadwork": EventType.CONSTRUCTION,
    "planned construction": EventType.CONSTRUCTION,
    "closure": EventType.CLOSURE,
    "weather": EventType.WEATHER_CONDITION,
    "special event": EventType.SPECIAL_EVENT,
}


def parse_massdot_events(
    payload, *, source_id: str, jurisdiction: Optional[str] = "MA"
) -> List[TrafficEvent]:
    text = payload if isinstance(payload, str) else ""
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out: List[TrafficEvent] = []
    for ev in root.iter("Event"):
        out.append(_parse_event(ev, source_id, jurisdiction))
    return out


def _ft(elem, tag: str) -> Optional[str]:
    val = elem.findtext(tag)
    return val.strip() if val and val.strip() else None


def _flt(elem, tag: str) -> Optional[float]:
    raw = _ft(elem, tag)
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _parse_event(ev, source_id: str, jurisdiction: Optional[str]) -> TrafficEvent:
    native_id = _ft(ev, "EventId") or str(id(ev))
    etype = (_ft(ev, "EventType") or _ft(ev, "EventCategory") or "").lower()
    lat = _flt(ev, "PrimaryLatitude")
    lon = _flt(ev, "PrimaryLongitude")
    status = (_ft(ev, "EventStatus") or "").lower()

    return TrafficEvent(
        id=f"{source_id}:{native_id}",
        source_id=source_id,
        jurisdiction=jurisdiction,
        event_type=_map_type(etype),
        subtypes=[s for s in [_ft(ev, "EventSubType")] if s],
        status=EventStatus.ARCHIVED if status in ("closed", "completed") else EventStatus.ACTIVE,
        headline=_ft(ev, "LocationDescription") or _ft(ev, "EventType"),
        description=_ft(ev, "LaneBlockageDescription"),
        roads=[RoadInfo(name=_ft(ev, "RoadwayName"), direction=_ft(ev, "Direction"))],
        geometry=Geometry.point(lon, lat) if lat is not None and lon is not None else None,
        created=parse_datetime(_ft(ev, "EventCreatedDate")),
        updated=parse_datetime(_ft(ev, "LastUpdate")),
        starts=parse_datetime(_ft(ev, "EventStartDate")),
        ends=parse_datetime(_ft(ev, "EventEndDate")),
        raw={"EventId": native_id, "EventType": _ft(ev, "EventType"),
             "EventSubType": _ft(ev, "EventSubType")},
    )


def _map_type(text: str) -> EventType:
    for key, etype in _TYPE_MAP.items():
        if key in text:
            return etype
    return EventType.UNKNOWN
