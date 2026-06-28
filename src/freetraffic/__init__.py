"""freetraffic -- free traffic data for traffic-aware routing.

Scrapes free, public traffic data (state 511 / Open511 event feeds, WZDx
work-zone feeds, and live-speed feeds), normalizes everything into one canonical
model, and exports it into the live-traffic inputs that Valhalla and OSRM
consume.

Quick start (offline-friendly: parsing/exporting needs no network)::

    from freetraffic import load_builtin_catalog, parse_open511, TrafficSnapshot

    events = parse_open511(payload, source_id="sf-bay-open511", jurisdiction="CA")
    snap = TrafficSnapshot(events=events).dedupe()
    print(snap.dumps(indent=2))

Live fetching (needs the [fetch] extra)::

    import asyncio
    from freetraffic import load_builtin_catalog, collect_feeds, TrafficSnapshot

    cat = load_builtin_catalog()
    events, errors = asyncio.run(collect_feeds(cat.select(jurisdiction="CA")))
    snap = TrafficSnapshot(events=events, errors=errors).dedupe()
"""

from __future__ import annotations

from .catalog import Catalog, FeedSpec, load_builtin_catalog
from .geometry import BoundingBox, Geometry
from .models import (
    EventStatus,
    EventType,
    Impact,
    LinkSpeed,
    RoadInfo,
    Severity,
    TrafficEvent,
)
from .parsers import (
    parse_ibi511_events,
    parse_ibi511_speeds,
    parse_nws_alerts,
    parse_open511,
    parse_wsdot_travel_times,
    parse_wzdx,
)
from .probes import (
    GtfsRtProbeTracker,
    ProbeSpeed,
    VehicleSample,
    aggregate_by_edge,
)
from .probes.tomtom import TomTomFlowClient
from .predict import (
    EdgePrediction,
    EtaPrediction,
    FusionConfig,
    RouteEdge,
    predict_edges,
    predict_eta,
    route_edges_from_trace,
)
from .routing import RouteResult, TrafficAwareRouter, ValhallaClient
from .store import TrafficSnapshot

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # catalog
    "Catalog",
    "FeedSpec",
    "load_builtin_catalog",
    # model
    "TrafficEvent",
    "LinkSpeed",
    "RoadInfo",
    "Impact",
    "EventType",
    "Severity",
    "EventStatus",
    "Geometry",
    "BoundingBox",
    # parsers / store
    "parse_open511",
    "parse_wzdx",
    "parse_ibi511_events",
    "parse_ibi511_speeds",
    "parse_nws_alerts",
    "parse_wsdot_travel_times",
    "TrafficSnapshot",
    # probes (measured live speeds)
    "GtfsRtProbeTracker",
    "ProbeSpeed",
    "VehicleSample",
    "aggregate_by_edge",
    "TomTomFlowClient",
    # prediction (the spine)
    "predict_eta",
    "predict_edges",
    "EtaPrediction",
    "EdgePrediction",
    "RouteEdge",
    "FusionConfig",
    "route_edges_from_trace",
    # routing
    "ValhallaClient",
    "TrafficAwareRouter",
    "RouteResult",
]


# Network-dependent helpers are imported lazily so the package works without httpx.
def __getattr__(name: str):  # PEP 562
    if name in {"collect_feeds", "collect_feed", "fetch_raw", "parse_pages"}:
        from . import client

        return getattr(client, name)
    if name in {"discover_wzdx_feeds"}:
        from . import discovery

        return getattr(discovery, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
