"""Generic ArcGIS REST FeatureServer parser.

A huge number of city/county/regional DOT portals expose traffic events through
an ArcGIS REST ``.../FeatureServer/0/query`` endpoint. Responses come in two
shapes:

* **Esri JSON** (``f=json``): ``{"features": [{"attributes": {...},
  "geometry": {"x","y"} | {"paths"} | {"rings"}}], "spatialReference": {...}}``
* **GeoJSON** (``f=geojson``): a standard ``FeatureCollection``.

This parser handles both, converts Web-Mercator coordinates to lon/lat, and maps
attributes to canonical events with tolerant, case-insensitive field heuristics
(works out-of-the-box for the common schemas; a portal with exotic field names
can get a thin bespoke parser). Configure feeds with ``f=geojson`` where possible
for the cleanest geometry.
"""

from __future__ import annotations

import math
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

_HEADLINE_KEYS = ["description", "event_description", "headline", "incident",
                  "name", "title", "eventtype", "event_type", "type", "comments"]
_TYPE_KEYS = ["eventtype", "event_type", "type", "category", "incidenttype", "class"]
_ROAD_KEYS = ["road", "route", "roadway", "highway", "street", "rdwy_name", "road_name"]
_DIR_KEYS = ["direction", "dir", "travel_direction", "bound"]
_ID_KEYS = ["objectid", "id", "event_id", "incident_id", "globalid", "oid"]
_UPDATED_KEYS = ["last_updated", "lastupdated", "updated", "update_time", "edit_date", "moddate"]

_TYPE_KEYWORDS = [
    ("construction", EventType.CONSTRUCTION), ("roadwork", EventType.CONSTRUCTION),
    ("work", EventType.CONSTRUCTION), ("closure", EventType.CLOSURE),
    ("closed", EventType.CLOSURE), ("incident", EventType.INCIDENT),
    ("crash", EventType.INCIDENT), ("accident", EventType.INCIDENT),
    ("event", EventType.SPECIAL_EVENT), ("weather", EventType.WEATHER_CONDITION),
    ("flood", EventType.WEATHER_CONDITION), ("snow", EventType.WEATHER_CONDITION),
]

_MERCATOR_R = 20037508.342789244


def parse_arcgis(
    payload: Any, *, source_id: str, jurisdiction: Optional[str] = None
) -> List[TrafficEvent]:
    if not isinstance(payload, dict):
        return []
    features = payload.get("features") or []
    is_mercator = _is_mercator(payload.get("spatialReference"))
    out: List[TrafficEvent] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        event = _parse_feature(feat, source_id, jurisdiction, is_mercator)
        if event is not None:
            out.append(event)
    return out


def _parse_feature(feat, source_id, jurisdiction, is_mercator) -> Optional[TrafficEvent]:
    # GeoJSON feature?
    if "properties" in feat or feat.get("type") == "Feature":
        attrs = feat.get("properties") or {}
        geom = Geometry.from_geojson(feat.get("geometry"))
    else:  # Esri feature
        attrs = feat.get("attributes") or {}
        geom = _esri_geometry(feat.get("geometry"), is_mercator)

    lower = {str(k).lower(): v for k, v in attrs.items()}
    native_id = str(_first(lower, _ID_KEYS) or id(feat))
    type_text = str(_first(lower, _TYPE_KEYS) or _first(lower, _HEADLINE_KEYS) or "")

    return TrafficEvent(
        id=f"{source_id}:{native_id}",
        source_id=source_id,
        jurisdiction=jurisdiction,
        event_type=_map_type(type_text),
        status=EventStatus.ACTIVE,
        headline=_first(lower, _HEADLINE_KEYS),
        description=_first(lower, ["description", "comments", "details"]),
        roads=[RoadInfo(name=_first(lower, _ROAD_KEYS), direction=_first(lower, _DIR_KEYS))],
        impact=Impact(closed="clos" in type_text.lower()),
        geometry=geom,
        updated=parse_datetime(_first(lower, _UPDATED_KEYS)),
        raw=attrs,
    )


def _map_type(text: str) -> EventType:
    low = text.lower()
    for keyword, etype in _TYPE_KEYWORDS:
        if keyword in low:
            return etype
    return EventType.UNKNOWN


def _first(lower: Dict[str, Any], keys: List[str]) -> Any:
    for k in keys:
        v = lower.get(k)
        if v not in (None, ""):
            return v
    # substring fallback
    for k in keys:
        for actual, v in lower.items():
            if k in actual and v not in (None, ""):
                return v
    return None


def _is_mercator(spatial_ref: Any) -> bool:
    if not isinstance(spatial_ref, dict):
        return False
    wkid = spatial_ref.get("latestWkid") or spatial_ref.get("wkid")
    return wkid in (102100, 3857, 900913)


def _esri_geometry(geom: Any, is_mercator: bool) -> Optional[Geometry]:
    if not isinstance(geom, dict):
        return None
    if "x" in geom and "y" in geom:
        lon, lat = _xy(geom["x"], geom["y"], is_mercator)
        return Geometry.point(lon, lat) if lon is not None else None
    if "paths" in geom and geom["paths"]:
        line = [list(_xy(p[0], p[1], is_mercator)) for p in geom["paths"][0]]
        return Geometry("LineString", line) if len(line) >= 2 else None
    if "rings" in geom and geom["rings"]:
        rings = [[list(_xy(p[0], p[1], is_mercator)) for p in ring] for ring in geom["rings"]]
        return Geometry("Polygon", rings)
    return None


def _xy(x, y, is_mercator):
    try:
        x = float(x)
        y = float(y)
    except (TypeError, ValueError):
        return (None, None)
    if is_mercator or abs(x) > 180 or abs(y) > 90:
        lon = x / _MERCATOR_R * 180.0
        lat = math.degrees(2 * math.atan(math.exp((y / _MERCATOR_R * 180.0) * math.pi / 180.0)) - math.pi / 2)
        return (lon, lat)
    return (x, y)
