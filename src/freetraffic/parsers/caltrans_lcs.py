"""Caltrans Lane Closure System (LCS) parser (XML).

Caltrans publishes per-district lane-closure XML at
``https://cwwp2.dot.ca.gov/data/d{N}/lcs/lcsStatusD{NN}.xml`` (directory
unpadded, filename zero-padded; no statewide file -- fetch all 12 districts).
Root ``<data>`` -> repeated ``<lcs>`` records with begin/end lat-lon, route,
direction, and a ``<closure>`` block (type, work, lanes, epochs). Updated every
5 min, covers active + 7-day planned. No auth.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import List, Optional

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


def _epoch(text: Optional[str]):
    if text and text.strip().isdigit():
        return parse_datetime(int(text))
    return None


def parse_caltrans_lcs(
    payload, *, source_id: str, jurisdiction: Optional[str] = "CA"
) -> List[TrafficEvent]:
    text = payload if isinstance(payload, str) else ""
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    out: List[TrafficEvent] = []
    for lcs in root.iter("lcs"):
        event = _parse_lcs(lcs, source_id, jurisdiction)
        if event is not None:
            out.append(event)
    return out


def _ft(elem, path: str) -> Optional[str]:
    val = elem.findtext(path)
    return val.strip() if val else None


def _flt(elem, path: str) -> Optional[float]:
    raw = _ft(elem, path)
    try:
        return float(raw) if raw not in (None, "") else None
    except ValueError:
        return None


def _parse_lcs(lcs, source_id: str, jurisdiction: Optional[str]) -> Optional[TrafficEvent]:
    native_id = _ft(lcs, "index") or str(id(lcs))
    direction = _ft(lcs, "location/travelFlowDirection")
    route = _ft(lcs, "location/begin/beginRoute") or _ft(lcs, "location/end/endRoute")

    b_lat = _flt(lcs, "location/begin/beginLatitude")
    b_lon = _flt(lcs, "location/begin/beginLongitude")
    e_lat = _flt(lcs, "location/end/endLatitude")
    e_lon = _flt(lcs, "location/end/endLongitude")
    geom = None
    if None not in (b_lat, b_lon, e_lat, e_lon):
        geom = Geometry("LineString", [[b_lon, b_lat], [e_lon, e_lat]])
    elif None not in (b_lat, b_lon):
        geom = Geometry.point(b_lon, b_lat)

    type_of_closure = (_ft(lcs, "closure/typeOfClosure") or "").lower()
    type_of_work = _ft(lcs, "closure/typeOfWork")
    delay_min = _flt(lcs, "closure/estimatedDelay")
    lanes_total = _to_int(_ft(lcs, "closure/totalExistingLanes"))
    lanes_closed_raw = _ft(lcs, "closure/lanesClosed")  # e.g. "2, RShoulder"
    lanes_closed = _leading_int(lanes_closed_raw)

    full = "full" in type_of_closure
    impact = Impact(closed=full, lanes_total=lanes_total, lanes_closed=lanes_closed)

    severity = Severity.MAJOR if full else (
        Severity.MAJOR if (delay_min and delay_min >= 45) else
        Severity.MODERATE if (delay_min and delay_min >= 15) else Severity.MINOR
    )

    headline = f"{route or 'Caltrans'} {direction or ''}: {type_of_closure or 'closure'}".strip()
    if type_of_work:
        headline += f" ({type_of_work})"

    return TrafficEvent(
        id=f"{source_id}:{native_id}",
        source_id=source_id,
        jurisdiction=jurisdiction,
        event_type=EventType.CLOSURE if full else EventType.CONSTRUCTION,
        subtypes=[type_of_work] if type_of_work else [],
        severity=severity,
        status=EventStatus.ACTIVE,
        headline=headline,
        description=f"Estimated delay: {delay_min} min" if delay_min else None,
        roads=[RoadInfo(name=route, direction=direction,
                        lanes_total=lanes_total, lanes_closed=lanes_closed)],
        impact=impact,
        geometry=geom,
        updated=_epoch(_ft(lcs, "recordTimestamp/recordEpoch")),
        starts=_epoch(_ft(lcs, "closure/closureTimestamp/closureStartEpoch")),
        ends=_epoch(_ft(lcs, "closure/closureTimestamp/closureEndEpoch")),
        raw={"index": native_id, "typeOfClosure": type_of_closure,
             "estimatedDelay": delay_min, "lanesClosed": lanes_closed_raw},
    )


def _to_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _leading_int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    m = re.match(r"\s*(\d+)", value)
    return int(m.group(1)) if m else None
