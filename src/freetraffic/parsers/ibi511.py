"""Parser for the Arcadis / IBI Group "Travel-IQ" 511 platform.

A large set of state and provincial 511 systems run on one vendor platform
(hosted under ``*.ibi511.com`` and on state-branded domains like 511ny.org,
511wi.gov, ...). They expose an identical REST API::

    GET /api/GetEvents?key={key}&format=json
    GET /api/GetTrafficSpeeds?key={key}&format=json
    GET /api/GetConstruction | GetIncidents | GetCameras | GetMessageSigns ...

This is the single highest-leverage adapter for breadth: one parser normalizes
many jurisdictions at once. Both endpoints return a flat JSON array of objects
with PascalCase keys. Field names drift slightly between portals and platform
versions, so every lookup is tolerant (tries several aliases) and missing
fields never raise.

Two record types come out of this platform:

* ``GetEvents`` -> :class:`TrafficEvent`  (incidents/closures/construction)
* ``GetTrafficSpeeds`` -> :class:`LinkSpeed`  (the scarce, valuable speed signal)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..geometry import Geometry, decode_polyline
from ..models import (
    MPH_TO_KPH,
    EventStatus,
    EventType,
    Impact,
    LinkSpeed,
    RoadInfo,
    Severity,
    TrafficEvent,
    parse_datetime,
)

_EVENT_TYPE_MAP = {
    "accidentsandincidents": EventType.INCIDENT,
    "incident": EventType.INCIDENT,
    "incidents": EventType.INCIDENT,
    "crash": EventType.INCIDENT,
    "construction": EventType.CONSTRUCTION,
    "roadwork": EventType.CONSTRUCTION,
    "roadworks": EventType.CONSTRUCTION,
    "closures": EventType.CLOSURE,
    "closure": EventType.CLOSURE,
    "specialevents": EventType.SPECIAL_EVENT,
    "specialevent": EventType.SPECIAL_EVENT,
    "roadconditions": EventType.ROAD_CONDITION,
    "weatherconditions": EventType.WEATHER_CONDITION,
    "winterdriving": EventType.WEATHER_CONDITION,
}

_SEVERITY_MAP = {
    "severe": Severity.MAJOR,
    "major": Severity.MAJOR,
    "high": Severity.MAJOR,
    "moderate": Severity.MODERATE,
    "medium": Severity.MODERATE,
    "minor": Severity.MINOR,
    "low": Severity.MINOR,
    "unknown": Severity.UNKNOWN,
}


def _first(d: Dict[str, Any], *keys: str) -> Any:
    """Return the first present, non-empty value among ``keys`` (case-tolerant)."""
    lower = None
    for key in keys:
        if key in d and d[key] not in (None, ""):
            return d[key]
    # fall back to a case-insensitive scan once
    lower = {k.lower(): v for k, v in d.items()}
    for key in keys:
        v = lower.get(key.lower())
        if v not in (None, ""):
            return v
    return None


def _records(payload: Any) -> List[Dict[str, Any]]:
    """The platform usually returns a bare array; tolerate a wrapped object."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("events", "Events", "data", "Data", "results", "items"):
            v = payload.get(key)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def _geometry(rec: Dict[str, Any]) -> Optional[Geometry]:
    poly = _first(rec, "EncodedPolyline", "MapEncodedPolyline", "Polyline", "encodedPolyline")
    if isinstance(poly, str) and poly:
        coords = decode_polyline(poly, precision=5)
        if len(coords) >= 2:
            return Geometry("LineString", coords)
    lat = _to_float(_first(rec, "Latitude", "lat", "StartLatitude"))
    lon = _to_float(_first(rec, "Longitude", "lon", "lng", "StartLongitude"))
    if lat is not None and lon is not None:
        return Geometry.point(lon, lat)
    return None


def parse_ibi511_events(
    payload: Any,
    *,
    source_id: str,
    jurisdiction: Optional[str] = None,
) -> List[TrafficEvent]:
    out: List[TrafficEvent] = []
    for rec in _records(payload):
        out.append(_parse_event(rec, source_id, jurisdiction))
    return out


# Alias so this slots into the generic EVENT_PARSERS registry by ``kind``.
parse_ibi511 = parse_ibi511_events


def _parse_event(
    rec: Dict[str, Any], source_id: str, jurisdiction: Optional[str]
) -> TrafficEvent:
    native_id = str(_first(rec, "ID", "Id", "id", "EventId") or id(rec))
    roadway = _first(rec, "RoadwayName", "RoadName", "Roadway")
    direction = _first(rec, "DirectionOfTravel", "Direction")
    full_closure = _to_bool(_first(rec, "IsFullClosure", "FullClosure"))
    lanes_affected = _first(rec, "LanesAffected", "LanesStatus")

    impact = Impact(closed=bool(full_closure))
    road = RoadInfo(
        name=roadway,
        direction=direction,
        from_location=_first(rec, "FromCity", "PrimaryLocation", "Location"),
    )

    return TrafficEvent(
        id=f"{source_id}:{native_id}",
        source_id=source_id,
        jurisdiction=jurisdiction,
        event_type=_map_event_type(_first(rec, "EventType", "Type")),
        subtypes=_subtypes(_first(rec, "EventSubType", "SubType")),
        severity=_SEVERITY_MAP.get(
            str(_first(rec, "Severity", "Priority") or "").lower(), Severity.UNKNOWN
        ),
        status=_status(rec),
        headline=_first(rec, "Description", "Headline", "Title"),
        description=_compose_description(rec, lanes_affected),
        roads=[road],
        impact=impact,
        geometry=_geometry(rec),
        created=parse_datetime(_first(rec, "Reported", "CreateTime", "StartDate")),
        updated=parse_datetime(_first(rec, "LastUpdated", "Updated", "UpdateTime")),
        starts=parse_datetime(_first(rec, "StartDate", "StartTime")),
        ends=parse_datetime(_first(rec, "PlannedEndDate", "EndDate", "EndTime")),
        url=_first(rec, "Url", "DetailUrl"),
        raw=rec,
    )


def parse_ibi511_traveltimes(
    payload: Any,
    *,
    source_id: str,
    jurisdiction: Optional[str] = None,
) -> List[LinkSpeed]:
    """Parse the IBI/Travel-IQ NEW-gen ``traveltimes`` endpoint into LinkSpeeds.

    (There is NO ``GetTrafficSpeeds`` endpoint on this platform; a handful of
    installs -- e.g. 511WI ``/api/v2/get/traveltimes`` -- ship measured travel
    times instead.) Fields: Distance (mi), CurrentTime/NormalTime (min),
    Start/End lat-lon. Speed = distance / current time.
    """
    out: List[LinkSpeed] = []
    for rec in _records(payload):
        # Prefer an explicit speed if present; otherwise derive from distance/time.
        speed_mph = _to_float(_first(rec, "Speed", "AverageSpeed", "CurrentSpeed"))
        distance_mi = _to_float(_first(rec, "Distance"))
        current_min = _to_float(_first(rec, "CurrentTime", "TravelTime"))
        normal_min = _to_float(_first(rec, "NormalTime", "AverageTime"))
        if speed_mph is None and distance_mi and current_min and current_min > 0:
            speed_mph = distance_mi / (current_min / 60.0)
        if speed_mph is None:
            continue
        freeflow_mph = _to_float(_first(rec, "FreeFlowSpeed", "SpeedLimit"))
        if freeflow_mph is None and distance_mi and normal_min and normal_min > 0:
            freeflow_mph = distance_mi / (normal_min / 60.0)
        out.append(
            LinkSpeed(
                source_id=source_id,
                jurisdiction=jurisdiction,
                speed_kph=speed_mph * MPH_TO_KPH,
                freeflow_kph=freeflow_mph * MPH_TO_KPH if freeflow_mph else None,
                link_id=str(_first(rec, "ID", "Id", "LinkId", "id") or "") or None,
                roadway=_first(rec, "RoadwayName", "RoadName", "Description"),
                direction=_first(rec, "DirectionOfTravel", "Direction"),
                observed_at=parse_datetime(_first(rec, "LastUpdated", "Updated")),
                geometry=_traveltime_geometry(rec),
                raw=rec,
            )
        )
    return out


# Back-compat alias (the platform has no GetTrafficSpeeds; this is travel-times).
parse_ibi511_speeds = parse_ibi511_traveltimes


def _traveltime_geometry(rec: Dict[str, Any]) -> Optional[Geometry]:
    s_lat = _to_float(_first(rec, "StartLatitude"))
    s_lon = _to_float(_first(rec, "StartLongitude"))
    e_lat = _to_float(_first(rec, "EndLatitude"))
    e_lon = _to_float(_first(rec, "EndLongitude"))
    if None not in (s_lat, s_lon, e_lat, e_lon):
        return Geometry("LineString", [[s_lon, s_lat], [e_lon, e_lat]])
    return _geometry(rec)


def _map_event_type(value: Any) -> EventType:
    return _EVENT_TYPE_MAP.get(str(value or "").strip().lower(), EventType.UNKNOWN)


def _subtypes(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)]


def _status(rec: Dict[str, Any]) -> EventStatus:
    raw = str(_first(rec, "EventStatus", "Status") or "").lower()
    if raw in ("planned", "pending", "scheduled"):
        return EventStatus.PLANNED
    if raw in ("closed", "completed", "cleared", "archived"):
        return EventStatus.ARCHIVED
    return EventStatus.ACTIVE


def _compose_description(rec: Dict[str, Any], lanes_affected: Any) -> Optional[str]:
    parts = []
    desc = _first(rec, "Description", "Details")
    if desc:
        parts.append(str(desc))
    if lanes_affected:
        parts.append(f"Lanes affected: {lanes_affected}")
    return " | ".join(parts) if parts else None


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")
