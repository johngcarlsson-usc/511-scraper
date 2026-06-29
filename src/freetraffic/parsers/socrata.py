"""Generic Socrata (SODA) open-data parser.

Many city/county open-data portals run Socrata and expose traffic
incidents/closures at ``https://<host>/resource/<id>.json`` as a flat JSON array.
Field names vary by dataset, so this parser uses tolerant, case-insensitive
heuristics and handles Socrata's geo conventions (``latitude``/``longitude``
columns, or a GeoJSON ``point`` / ``location`` column). Datasets with exotic
schemas can get a thin bespoke parser.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..geometry import Geometry
from ..models import (
    EventStatus,
    EventType,
    Impact,
    RoadInfo,
    TrafficEvent,
    parse_datetime,
)

_HEADLINE_KEYS = ["description", "incident", "name", "title", "event_type",
                  "type", "details", "comments", "status_description"]
_TYPE_KEYS = ["event_type", "type", "category", "incident_type", "class", "eventtype"]
_ROAD_KEYS = ["road", "street", "route", "roadway", "highway", "on_street", "street_name"]
_DIR_KEYS = ["direction", "dir", "bound", "travel_direction"]
_ID_KEYS = ["id", "event_id", "incident_id", "case_number", "objectid", "sid"]
_UPDATED_KEYS = ["updated", "last_updated", "update_date", "modified", "datetime", "date"]
_GEO_KEYS = ["location", "point", "geocoded_column", "the_geom", "geom", "shape"]

_TYPE_KEYWORDS = [
    ("construction", EventType.CONSTRUCTION), ("work", EventType.CONSTRUCTION),
    ("closure", EventType.CLOSURE), ("closed", EventType.CLOSURE),
    ("incident", EventType.INCIDENT), ("crash", EventType.INCIDENT),
    ("collision", EventType.INCIDENT), ("event", EventType.SPECIAL_EVENT),
    ("weather", EventType.WEATHER_CONDITION), ("flood", EventType.WEATHER_CONDITION),
]


def parse_socrata(
    payload: Any, *, source_id: str, jurisdiction: Optional[str] = None
) -> List[TrafficEvent]:
    records = payload if isinstance(payload, list) else \
        (payload.get("data") if isinstance(payload, dict) else None)
    if not isinstance(records, list):
        return []
    out: List[TrafficEvent] = []
    for rec in records:
        if isinstance(rec, dict):
            out.append(_parse(rec, source_id, jurisdiction))
    return out


def _parse(rec: Dict[str, Any], source_id: str, jurisdiction: Optional[str]) -> TrafficEvent:
    lower = {str(k).lower(): v for k, v in rec.items()}
    native_id = str(_first(lower, _ID_KEYS) or id(rec))
    type_text = str(_first(lower, _TYPE_KEYS) or _first(lower, _HEADLINE_KEYS) or "")

    return TrafficEvent(
        id=f"{source_id}:{native_id}",
        source_id=source_id,
        jurisdiction=jurisdiction,
        event_type=_map_type(type_text),
        status=EventStatus.ACTIVE,
        headline=_first(lower, _HEADLINE_KEYS),
        description=_first(lower, ["description", "details", "comments"]),
        roads=[RoadInfo(name=_first(lower, _ROAD_KEYS), direction=_first(lower, _DIR_KEYS))],
        impact=Impact(closed="clos" in type_text.lower()),
        geometry=_geometry(lower),
        updated=parse_datetime(_first(lower, _UPDATED_KEYS)),
        raw=rec,
    )


def _geometry(lower: Dict[str, Any]) -> Optional[Geometry]:
    lat = _to_float(_first(lower, ["latitude", "lat", "y"]))
    lon = _to_float(_first(lower, ["longitude", "lon", "lng", "x"]))
    if lat is not None and lon is not None:
        return Geometry.point(lon, lat)
    geo = _first(lower, _GEO_KEYS)
    if isinstance(geo, dict):
        # Socrata "point" / GeoJSON-ish: {"type":"Point","coordinates":[lon,lat]}
        if geo.get("coordinates"):
            g = Geometry.from_geojson(geo)
            if g:
                return g
        glat = _to_float(geo.get("latitude"))
        glon = _to_float(geo.get("longitude"))
        if glat is not None and glon is not None:
            return Geometry.point(glon, glat)
    return None


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
    for k in keys:
        for actual, v in lower.items():
            if k in actual and v not in (None, "", {}):
                return v
    return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
