"""National Weather Service alerts parser.

Weather doesn't measure traffic, but it explains and predicts it: winter storms,
ice, fog, flooding and high winds all depress road speeds and close roads. The
NWS API (https://api.weather.gov/alerts/active) is free, national and ungated
(it only asks for a descriptive User-Agent, which the client sends) and returns
GeoJSON, so it drops straight into our model.

We keep only driving-relevant alert types and emit them as
:class:`~freetraffic.models.TrafficEvent` of type ``WEATHER_CONDITION`` -- their
polygons can feed avoidance, and their presence/severity can drive speed
penalties downstream.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..geometry import Geometry
from ..models import (
    EventStatus,
    EventType,
    RoadInfo,
    Severity,
    TrafficEvent,
    parse_datetime,
)

# Substrings of NWS ``event`` names that meaningfully affect driving.
_DRIVING_KEYWORDS = (
    "winter", "snow", "ice", "blizzard", "freezing", "freeze",
    "fog", "flood", "wind", "dust", "storm", "hurricane",
    "tropical", "tornado",
)

_SEVERITY_MAP = {
    "extreme": Severity.MAJOR,
    "severe": Severity.MAJOR,
    "moderate": Severity.MODERATE,
    "minor": Severity.MINOR,
    "unknown": Severity.UNKNOWN,
}


def parse_nws_alerts(
    payload: Any,
    *,
    source_id: str,
    jurisdiction: Optional[str] = None,
) -> List[TrafficEvent]:
    if not isinstance(payload, dict):
        return []
    out: List[TrafficEvent] = []
    for feat in payload.get("features") or []:
        if not isinstance(feat, dict):
            continue
        event = _parse_alert(feat, source_id, jurisdiction)
        if event is not None:
            out.append(event)
    return out


def _parse_alert(
    feat: Dict[str, Any], source_id: str, jurisdiction: Optional[str]
) -> Optional[TrafficEvent]:
    props = feat.get("properties") or {}
    name = str(props.get("event") or "")
    if not _is_driving_relevant(name):
        return None

    native_id = str(props.get("id") or feat.get("id") or id(feat))
    area = props.get("areaDesc")

    return TrafficEvent(
        id=f"{source_id}:{native_id}",
        source_id=source_id,
        jurisdiction=jurisdiction,
        event_type=EventType.WEATHER_CONDITION,
        subtypes=[name] if name else [],
        severity=_SEVERITY_MAP.get(str(props.get("severity", "")).lower(), Severity.UNKNOWN),
        status=EventStatus.ACTIVE,
        headline=props.get("headline") or name,
        description=props.get("description"),
        roads=[RoadInfo(from_location=area)] if area else [],
        geometry=Geometry.from_geojson(feat.get("geometry")),
        created=parse_datetime(props.get("sent") or props.get("onset")),
        updated=parse_datetime(props.get("sent")),
        starts=parse_datetime(props.get("onset") or props.get("effective")),
        ends=parse_datetime(props.get("ends") or props.get("expires")),
        url=props.get("@id") or props.get("id"),
        raw=feat,
    )


def _is_driving_relevant(event_name: str) -> bool:
    low = event_name.lower()
    return any(k in low for k in _DRIVING_KEYWORDS)
