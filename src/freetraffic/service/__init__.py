"""Long-running services that turn feeds into live routing inputs.

:mod:`freetraffic.service.loop` is the continuous GTFS-Realtime poller: on a
timer it fetches transit ``VehiclePositions`` for one or more agencies, diffs
them into probe speeds, map-matches to the Valhalla graph, aggregates per edge,
and applies the result either to an in-memory snapshot (Mode A) or straight into
a Valhalla ``traffic.tar`` (Mode B). This is what makes freetraffic a *running
service* rather than a one-shot tool.
"""

from __future__ import annotations

from .loop import (
    AgencyFeed,
    EdgeSpeedStore,
    TickStats,
    run_service_loop,
)

__all__ = [
    "AgencyFeed",
    "EdgeSpeedStore",
    "TickStats",
    "run_service_loop",
]
