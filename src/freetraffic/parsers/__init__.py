"""Parsers that turn raw source payloads into canonical records.

Each parser is a pure function ``parse(payload, *, source_id, jurisdiction)``
returning a list of :class:`~freetraffic.models.TrafficEvent` (or
:class:`~freetraffic.models.LinkSpeed`). They are registered by ``kind`` so the
catalog can dispatch generically.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from ..models import TrafficEvent
from .open511 import parse_open511
from .wzdx import parse_wzdx

# kind -> parser. Generic parsers cover any standards-compliant jurisdiction;
# bespoke per-state parsers can be registered here too.
EVENT_PARSERS: Dict[str, Callable[..., List[TrafficEvent]]] = {
    "open511": parse_open511,
    "wzdx": parse_wzdx,
}

__all__ = ["EVENT_PARSERS", "parse_open511", "parse_wzdx"]
