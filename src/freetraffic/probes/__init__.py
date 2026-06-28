"""Probe-based live-speed sources.

These derive *measured* road speeds (the signal that actually sharpens travel
times) rather than events:

* :mod:`freetraffic.probes.gtfs_rt` -- treats live transit vehicle positions
  (GTFS-Realtime) as floating probes and infers segment speeds. Free, national,
  ungated.
* :mod:`freetraffic.probes.tomtom` -- on-demand TomTom Traffic *Flow Segment*
  lookups using a freemium key. Live-lookup only, to respect the provider's ToS
  (no bulk storage / redistribution).
"""

from __future__ import annotations

from .gtfs_rt import (
    GtfsRtProbeTracker,
    ProbeSpeed,
    VehicleSample,
    aggregate_by_edge,
    parse_vehicle_positions,
)

__all__ = [
    "GtfsRtProbeTracker",
    "ProbeSpeed",
    "VehicleSample",
    "aggregate_by_edge",
    "parse_vehicle_positions",
]
