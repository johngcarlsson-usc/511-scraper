"""Parsers that turn raw source payloads into canonical records.

Each parser is a pure function ``parse(payload, *, source_id, jurisdiction)``
returning a list of :class:`~freetraffic.models.TrafficEvent` or
:class:`~freetraffic.models.LinkSpeed`. They are registered by ``kind`` so the
catalog/client can dispatch generically. ``EVENT_PARSERS`` and ``SPEED_PARSERS``
are keyed by the feed's ``kind``.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from ..models import LinkSpeed, TrafficEvent
from .ibi511 import parse_ibi511_events, parse_ibi511_speeds
from .nws import parse_nws_alerts
from .open511 import parse_open511
from .wsdot import parse_wsdot_travel_times
from .wzdx import parse_wzdx

# kind -> event parser. Generic parsers cover any standards-compliant or shared
# vendor-platform jurisdiction; bespoke per-state parsers register here too.
EVENT_PARSERS: Dict[str, Callable[..., List[TrafficEvent]]] = {
    "open511": parse_open511,
    "wzdx": parse_wzdx,
    "ibi511": parse_ibi511_events,
    "nws_alerts": parse_nws_alerts,
}

# kind -> speed parser (the scarcer live-speed signal).
SPEED_PARSERS: Dict[str, Callable[..., List[LinkSpeed]]] = {
    "ibi511_speeds": parse_ibi511_speeds,
    "wsdot_traveltimes": parse_wsdot_travel_times,
}

__all__ = [
    "EVENT_PARSERS",
    "SPEED_PARSERS",
    "parse_open511",
    "parse_wzdx",
    "parse_ibi511_events",
    "parse_ibi511_speeds",
    "parse_nws_alerts",
    "parse_wsdot_travel_times",
]
