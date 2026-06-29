"""NCDOT (DriveNC) parser.

North Carolina DOT publishes incidents/road conditions via a public REST API
(e.g. ``https://eapps.ncdot.gov/services/traffic-prod/v1/incidents``) returning a
JSON array of incident objects. Tolerant to field drift.
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
    "incident": EventType.INCIDENT,
    "crash": EventType.INCIDENT,
    "road construction": EventType.CONSTRUCTION,
    "construction": EventType.CONSTRUCTION,
    "roadwork": EventType.CONSTRUCTION,
    "interstate construction": EventType.CONSTRUCTION,
    "road closure": EventType.CLOSURE,
    "closure": EventType.CLOSURE,
    "weather event": EventType.WEATHER_CONDITION,
    "special event": EventType.SPECIAL_EVENT,
}


def parse_ncdot(
    payload: Any, *, source_id: str, jurisdiction: Optional[str] = "NC"
) -> List[TrafficEvent]:
    if isinstance(payload, dict):
        records = payload.get("incidents") or payload.get("data") or payload.get("results") or []
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
    native_id = str(rec.get("id") or rec.get("incidentId") or id(rec))
    itype = str(rec.get("incidentType") or rec.get("type") or "").lower()
    lat = _to_float(rec.get("latitude") or rec.get("lat"))
    lon = _to_float(rec.get("longitude") or rec.get("lon"))
    condition = str(rec.get("condition") or "")
    closed = "closed" in condition.lower()

    return TrafficEvent(
        id=f"{source_id}:{native_id}",
        source_id=source_id,
        jurisdiction=jurisdiction,
        event_type=_TYPE_MAP.get(itype, EventType.UNKNOWN),
        severity=Severity.MAJOR if closed else Severity.UNKNOWN,
        status=EventStatus.ACTIVE,
        headline=rec.get("road") and f"{rec.get('road')}: {rec.get('reason') or condition or itype}"
        or (rec.get("reason") or condition or itype),
        description=rec.get("reason") or condition,
        roads=[RoadInfo(
            name=rec.get("road"),
            direction=rec.get("direction"),
            from_location=rec.get("location") or rec.get("city"),
        )],
        impact=Impact(closed=closed),
        geometry=Geometry.point(lon, lat) if lat is not None and lon is not None else None,
        created=parse_datetime(rec.get("start") or rec.get("constructionDateStart")),
        updated=parse_datetime(rec.get("lastUpdate") or rec.get("modified")),
        starts=parse_datetime(rec.get("start")),
        ends=parse_datetime(rec.get("end")),
        raw=rec,
    )


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
