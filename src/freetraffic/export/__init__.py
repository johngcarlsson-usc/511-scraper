"""Exporters: canonical records -> routing-engine traffic inputs.

The canonical model is engine-agnostic. These exporters turn it into the
formats Valhalla and OSRM actually consume. Both engines key live traffic by
their *own* internal graph identifiers (Valhalla edge ids; OSRM node-pair
segments), so the missing link is map-matching a record's geometry onto those
ids -- see :mod:`freetraffic.match` for the Valhalla bridge. The exporters here
take *already-matched* rows and emit byte-perfect files.
"""

from __future__ import annotations

from . import osrm, valhalla

__all__ = ["osrm", "valhalla"]
