"""WZDx (Work Zone Data Exchange) parser.

WZDx is the USDOT/FHWA standard for work-zone and (in later versions) road
restriction / detour / device data. A feed is a GeoJSON ``FeatureCollection``
where each feature's ``properties`` describe a road event.

The spec has shifted across versions:

* v2/v3: event fields live flat on ``properties`` (``road_names``, ``direction``,
  ``vehicle_impact``, ``beginning_accuracy`` ...).
* v4+: shared fields move under ``properties.core_details`` (``event_type``,
  ``road_names``, ``direction``, ``description``, ``update_date`` ...).

This parser handles both shapes and treats the feed version leniently.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..geometry import Geometry
from ..models import (
    EventStatus,
    EventType,
    Impact,
    MPH_TO_KPH,
    RoadInfo,
    Severity,
    TrafficEvent,
    parse_datetime,
)

# WZDx event_type -> canonical
_EVENT_TYPE_MAP = {
    "work-zone": EventType.CONSTRUCTION,
    "detour": EventType.RESTRICTION,
    "restriction": EventType.RESTRICTION,
}

# WZDx vehicle_impact -> (closed?, all-lanes-affected?)
_FULL_CLOSURE = {"all-lanes-closed"}


def parse_wzdx(
    payload: Dict[str, Any],
    *,
    source_id: str,
    jurisdiction: Optional[str] = None,
) -> List[TrafficEvent]:
    if not isinstance(payload, dict):
        return []
    features = payload.get("features") or []
    out: List[TrafficEvent] = []
    for feat in features:
        if isinstance(feat, dict) and feat.get("type") == "Feature":
            event = _parse_feature(feat, source_id, jurisdiction)
            if event is not None:
                out.append(event)
    return out


def _parse_feature(
    feat: Dict[str, Any], source_id: str, jurisdiction: Optional[str]
) -> Optional[TrafficEvent]:
    props = feat.get("properties") or {}
    if not isinstance(props, dict):
        return None
    core = props.get("core_details") if isinstance(props.get("core_details"), dict) else props

    native_id = str(feat.get("id") or core.get("road_event_id") or id(feat))
    road_names = core.get("road_names") or core.get("road_name") or []
    if isinstance(road_names, str):
        road_names = [road_names]
    direction = core.get("direction")

    reduced_speed = props.get("reduced_speed_limit_kph")
    if reduced_speed is None and props.get("reduced_speed_limit") is not None:
        # older feeds report mph
        reduced_speed = _as_float(props.get("reduced_speed_limit"))
        if reduced_speed is not None:
            reduced_speed *= MPH_TO_KPH

    lanes = props.get("lanes")
    lanes_total = len(lanes) if isinstance(lanes, list) else None
    lanes_closed = (
        sum(1 for ln in lanes if str(ln.get("status", "")).lower() == "closed")
        if isinstance(lanes, list)
        else None
    )

    vehicle_impact = str(props.get("vehicle_impact", "")).lower()
    closed = vehicle_impact in _FULL_CLOSURE or (
        lanes_total is not None and lanes_closed == lanes_total and lanes_total > 0
    )

    roads = [
        RoadInfo(
            name=name,
            direction=direction,
            lanes_total=lanes_total,
            lanes_closed=lanes_closed,
            reduced_speed_kph=reduced_speed,
        )
        for name in (road_names or [None])
    ]

    return TrafficEvent(
        id=f"{source_id}:{native_id}",
        source_id=source_id,
        jurisdiction=jurisdiction,
        event_type=_EVENT_TYPE_MAP.get(
            str(core.get("event_type", "")).lower(), EventType.CONSTRUCTION
        ),
        subtypes=_subtypes(props),
        severity=_severity(vehicle_impact, closed),
        status=_status(props),
        headline=core.get("name") or core.get("description"),
        description=core.get("description"),
        roads=roads,
        impact=Impact(
            closed=closed,
            lanes_closed=lanes_closed,
            lanes_total=lanes_total,
            reduced_speed_kph=reduced_speed,
        ),
        geometry=Geometry.from_geojson(feat.get("geometry")),
        created=parse_datetime(core.get("creation_date")),
        updated=parse_datetime(core.get("update_date") or core.get("update_time")),
        starts=parse_datetime(props.get("start_date")),
        ends=parse_datetime(props.get("end_date")),
        url=core.get("related_road_event_url"),
        raw=feat,
    )


def _subtypes(props: Dict[str, Any]) -> List[str]:
    subs = []
    wt = props.get("work_zone_type") or props.get("types_of_work")
    if isinstance(wt, str):
        subs.append(wt)
    elif isinstance(wt, list):
        for item in wt:
            if isinstance(item, dict) and item.get("type_name"):
                subs.append(str(item["type_name"]))
            elif item:
                subs.append(str(item))
    return subs


def _severity(vehicle_impact: str, closed: bool) -> Severity:
    if closed or vehicle_impact == "all-lanes-closed":
        return Severity.MAJOR
    if vehicle_impact in ("some-lanes-closed", "alternating-one-way"):
        return Severity.MODERATE
    if vehicle_impact == "all-lanes-open":
        return Severity.MINOR
    return Severity.UNKNOWN


def _status(props: Dict[str, Any]) -> EventStatus:
    raw = str(props.get("event_status", "")).lower()
    if raw == "active":
        return EventStatus.ACTIVE
    if raw in ("planned", "pending"):
        return EventStatus.PLANNED
    if raw in ("completed", "cancelled", "canceled"):
        return EventStatus.ARCHIVED
    return EventStatus.ACTIVE


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
