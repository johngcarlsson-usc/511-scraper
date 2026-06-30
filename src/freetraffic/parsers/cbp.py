"""CBP border wait times parser (XML).

US Customs & Border Protection publishes estimated wait times at land ports of
entry (https://bwt.cbp.gov/api/waittimes, XML). For cross-border routing the
relevant signal is the *delay in minutes* per port/lane type. The feed has no
coordinates, so events come through without geometry (a port-name → lon/lat
lookup is a roadmap item); they still carry the delay in the headline/raw.

Tolerant: the feed's element names drift; missing fields never raise.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from importlib import resources
from typing import Dict, List, Optional

from ..geometry import Geometry
from ..models import EventStatus, EventType, RoadInfo, Severity, TrafficEvent, parse_datetime


def _load_port_coords() -> Dict[str, list]:
    try:
        text = resources.files("freetraffic.registry").joinpath("cbp_ports.json").read_text(
            encoding="utf-8"
        )
        return json.loads(text).get("ports", {})
    except (FileNotFoundError, ValueError):  # pragma: no cover
        return {}


_PORT_COORDS = _load_port_coords()


def parse_cbp_border_wait(
    payload, *, source_id: str, jurisdiction: Optional[str] = None
) -> List[TrafficEvent]:
    text = payload if isinstance(payload, str) else (payload or {}).get("_raw", "")
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    out: List[TrafficEvent] = []
    for port in root.iter("port"):
        event = _parse_port(port, source_id, jurisdiction)
        if event is not None:
            out.append(event)
    return out


def _text(elem, tag: str) -> Optional[str]:
    child = elem.find(tag)
    return child.text.strip() if child is not None and child.text else None


def _lane_delay(port, group: str, lane: str):
    grp = port.find(group)
    if grp is None:
        return None, None, None
    ln = grp.find(lane)
    if ln is None:
        return None, None, None
    return _text(ln, "delay_minutes"), _text(ln, "operational_status"), _text(ln, "update_time")


def _parse_port(port, source_id: str, jurisdiction: Optional[str]) -> Optional[TrafficEvent]:
    port_number = _text(port, "port_number") or _text(port, "port_name") or "port"
    port_name = _text(port, "port_name") or "Border crossing"
    border = _text(port, "border")

    delay, status, updated = _lane_delay(port, "passenger_vehicle_lanes", "standard_lanes")
    comm_delay, _, _ = _lane_delay(port, "commercial_vehicle_lanes", "standard_lanes")

    delay_min = _to_int(delay)
    headline = f"{port_name}: {delay_min} min wait (passenger, standard)" if delay_min is not None \
        else f"{port_name}: border wait time"

    severity = Severity.UNKNOWN
    if delay_min is not None:
        severity = Severity.MAJOR if delay_min >= 45 else (
            Severity.MODERATE if delay_min >= 20 else Severity.MINOR
        )

    return TrafficEvent(
        id=f"{source_id}:{port_number}",
        source_id=source_id,
        jurisdiction=jurisdiction,
        event_type=EventType.RESTRICTION,
        subtypes=["border_wait"],
        severity=severity,
        status=EventStatus.ACTIVE,
        headline=headline,
        description=(
            f"Border: {border}. Passenger standard delay: {delay} min. "
            f"Commercial standard delay: {comm_delay} min."
        ),
        roads=[RoadInfo(name=_text(port, "crossing_name") or port_name)],
        geometry=_port_geometry(port_number),  # joined from bundled BTS lookup
        updated=parse_datetime(updated),
        raw={
            "port_number": port_number, "port_name": port_name, "border": border,
            "passenger_standard_delay_min": delay_min,
            "commercial_standard_delay_min": _to_int(comm_delay),
            "operational_status": status,
        },
    )


def _port_geometry(port_number: str) -> Optional[Geometry]:
    """CBP port_number (6-8 digits) -> coords via first-4-digit BTS port_code."""
    code = str(port_number).strip()[:4].zfill(4)
    lonlat = _PORT_COORDS.get(code)
    if lonlat and len(lonlat) == 2:
        return Geometry.point(float(lonlat[0]), float(lonlat[1]))
    return None


def _to_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
