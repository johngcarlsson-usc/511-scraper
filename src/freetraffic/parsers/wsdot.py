"""WSDOT Traveler Information API parsers.

WSDOT (Washington State DOT) publishes a free Traveler Information API. The most
valuable endpoint for traffic-aware routing is **GetTravelTimes**: pre-defined
corridors with a *measured current travel time* vs a typical average -- i.e. the
exact quantity a router is trying to estimate. We convert each into a
:class:`~freetraffic.models.LinkSpeed` (current speed from distance/time, with
the typical time giving a freeflow baseline).

This adapter is also the template for the many other state DOTs that expose a
travel-times feed; point a new ``FeedSpec`` at theirs and adjust field names.

Free key: https://wsdot.wa.gov/traffic/api/
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..geometry import Geometry
from ..models import MPH_TO_KPH, LinkSpeed, parse_datetime

_MILES_TO_KM = 1.609_344


def parse_wsdot_travel_times(
    payload: Any,
    *,
    source_id: str,
    jurisdiction: Optional[str] = "WA",
) -> List[LinkSpeed]:
    records = payload if isinstance(payload, list) else (payload or {}).get("d") or []
    out: List[LinkSpeed] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        link = _travel_time_to_link_speed(rec, source_id, jurisdiction)
        if link is not None:
            out.append(link)
    return out


def _travel_time_to_link_speed(
    rec: Dict[str, Any], source_id: str, jurisdiction: Optional[str]
) -> Optional[LinkSpeed]:
    distance_mi = _to_float(rec.get("Distance"))
    current_min = _to_float(rec.get("CurrentTime"))
    average_min = _to_float(rec.get("AverageTime"))
    if not distance_mi or distance_mi <= 0:
        return None
    if not current_min or current_min <= 0:
        return None

    distance_km = distance_mi * _MILES_TO_KM
    speed_kph = distance_km / (current_min / 60.0)
    freeflow_kph = (
        distance_km / (average_min / 60.0)
        if average_min and average_min > 0
        else None
    )

    start = rec.get("StartPoint") or {}
    end = rec.get("EndPoint") or {}
    geom = _line_from_points(start, end)

    return LinkSpeed(
        source_id=source_id,
        jurisdiction=jurisdiction,
        speed_kph=speed_kph,
        freeflow_kph=freeflow_kph,
        link_id=str(rec.get("TravelTimeID") or rec.get("Name") or "") or None,
        roadway=start.get("RoadName") or rec.get("Name"),
        direction=start.get("Direction"),
        observed_at=parse_datetime(rec.get("TimeUpdated")),
        geometry=geom,
        raw=rec,
    )


def _line_from_points(start: Dict[str, Any], end: Dict[str, Any]) -> Optional[Geometry]:
    s = (_to_float(start.get("Longitude")), _to_float(start.get("Latitude")))
    e = (_to_float(end.get("Longitude")), _to_float(end.get("Latitude")))
    if None not in s and None not in e:
        return Geometry("LineString", [[s[0], s[1]], [e[0], e[1]]])
    if None not in s:
        return Geometry.point(s[0], s[1])
    return None


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
