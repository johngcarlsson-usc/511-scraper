"""Travel-time prediction: the spine that turns sources into predicted ETAs.

Every source in this package normalizes to one intermediate -- a **predicted
speed per routing-graph edge** -- and travel time is then ``sum(length / speed)``
over a route's edges. :mod:`freetraffic.predict.fusion` implements that model
with an explicit, tunable precedence:

1. hard constraints  (full closures -> impassable)
2. measured speed    (probes / DOT travel-times / vendor speeds / TomTom)
3. modeled fallback  (incident & lane penalties, reduced-limit caps, weather)

See :class:`~freetraffic.predict.fusion.FusionConfig` for the knobs and
:func:`~freetraffic.predict.fusion.predict_eta` for the entry point.
"""

from __future__ import annotations

from .fusion import (
    EdgePrediction,
    EtaPrediction,
    FusionConfig,
    RouteEdge,
    predict_edges,
    predict_eta,
    route_edges_from_trace,
)

__all__ = [
    "RouteEdge",
    "EdgePrediction",
    "EtaPrediction",
    "FusionConfig",
    "predict_edges",
    "predict_eta",
    "route_edges_from_trace",
]
