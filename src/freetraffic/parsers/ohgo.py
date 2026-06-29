"""OHGO (Ohio DOT) parser.

OHGO's public API (https://publicapi.ohgo.com/api/v1/<construction|incidents|...>)
returns JSON ``{"results": [ ... ]}`` with one object per event. Auth is an
``Authorization: APIKEY <key>`` header. Tolerant to field drift.
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

_TYPE_MAP = {
    "construction": EventType.CONSTRUCTION,
    "incident": EventType.INCIDENT,
    "incidents": EventType.INCIDENT,
    "crash": EventType.INCIDENT,
    "closure": EventType.CLOSURE,
    "weather": EventType.WEATHER_CONDITION,
}


def parse_ohgo(
    payload: Any, *, source_id: str, jurisdiction: Optional[str] = "OH"
) -> List[TrafficEvent]:
    if isinstance(payload, dict):
        records = payload.get("results") or payload.get("data") or []
    elif isinstance(payload, list):
        records = payload
    else:
        return []
    out: List[TrafficEvent] = []
    for rec in records:
        if isinstance(rec, dict):
            out.append(_parse(rec, source_id, jurisdiction))
    return out


def _parse(rec: Dict[str, Any], source_id: str, jurisdiction: Optional[str]) -> TrafficEvent:
    native_id = str(rec.get("id") or rec.get("Id") or id(rec))
    category = str(rec.get("category") or rec.get("type") or "").lower()
    lat = _to_float(rec.get("latitude") or rec.get("lat"))
    lon = _to_float(rec.get("longitude") or rec.get("lon") or rec.get("lng"))
    closed = "closed" in str(rec.get("roadStatus", "")).lower()

    return TrafficEvent(
        id=f"{source_id}:{native_id}",
        source_id=source_id,
        jurisdiction=jurisdiction,
        event_type=_TYPE_MAP.get(category, EventType.UNKNOWN),
        severity=Severity.MAJOR if closed else Severity.UNKNOWN,
        status=EventStatus.ACTIVE,
        headline=rec.get("description") or rec.get("title"),
        description=rec.get("description"),
        roads=[RoadInfo(
            name=rec.get("routeName") or rec.get("route"),
            direction=rec.get("direction"),
            from_location=rec.get("location"),
        )],
        impact=Impact(closed=closed),
        geometry=Geometry.point(lon, lat) if lat is not None and lon is not None else None,
        updated=parse_datetime(rec.get("lastUpdated") or rec.get("updateTime")),
        url=rec.get("detailUrl") or rec.get("url"),
        raw=rec,
    )


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
