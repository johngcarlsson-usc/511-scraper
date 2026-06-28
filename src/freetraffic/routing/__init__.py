"""Routing layer: turn normalized traffic data into traffic-aware routes.

OSRM has been retired in favour of Valhalla, so this layer targets Valhalla's
HTTP API directly. :class:`~freetraffic.routing.valhalla.ValhallaClient` wraps
the server (HTTP Basic auth + optional ngrok header), and
:class:`~freetraffic.routing.service.TrafficAwareRouter` fuses a
:class:`~freetraffic.store.TrafficSnapshot` into each request -- without needing
to rebuild tiles -- by excluding closed roads and annotating the route with the
active events along it.
"""

from __future__ import annotations

from .service import RouteResult, TrafficAwareRouter
from .valhalla import ValhallaClient

__all__ = ["ValhallaClient", "TrafficAwareRouter", "RouteResult"]
