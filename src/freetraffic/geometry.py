"""Lightweight GeoJSON-style geometry helpers (no third-party deps).

All coordinates follow the GeoJSON convention: ``[longitude, latitude]`` and,
where present, ``[lon, lat, elevation]``. We intentionally avoid a heavy geo
stack here so the core library stays pure-standard-library and installable
anywhere; callers that need real spatial ops (map-matching, buffering) can hand
these structures to shapely/Valhalla/OSRM downstream.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Sequence, Tuple

Position = Sequence[float]  # [lon, lat] or [lon, lat, z]


@dataclass(frozen=True)
class Geometry:
    """A minimal GeoJSON geometry (Point / MultiPoint / LineString / etc.)."""

    type: str
    coordinates: Any  # shape depends on ``type``; kept as raw nested lists

    def to_geojson(self) -> dict:
        return {"type": self.type, "coordinates": self.coordinates}

    @classmethod
    def from_geojson(cls, obj: Optional[dict]) -> Optional["Geometry"]:
        if not obj or "type" not in obj or "coordinates" not in obj:
            return None
        return cls(type=obj["type"], coordinates=obj["coordinates"])

    @classmethod
    def point(cls, lon: float, lat: float) -> "Geometry":
        return cls("Point", [lon, lat])

    @classmethod
    def line(cls, positions: Iterable[Position]) -> "Geometry":
        return cls("LineString", [list(p) for p in positions])

    def iter_positions(self) -> Iterable[Position]:
        """Yield every coordinate position, regardless of geometry type."""
        yield from _iter_positions(self.coordinates)

    def representative_point(self) -> Optional[Tuple[float, float]]:
        """A single (lon, lat) summarising the geometry (centroid of vertices)."""
        xs: List[float] = []
        ys: List[float] = []
        for pos in self.iter_positions():
            if len(pos) >= 2:
                xs.append(float(pos[0]))
                ys.append(float(pos[1]))
        if not xs:
            return None
        return (sum(xs) / len(xs), sum(ys) / len(ys))


def _iter_positions(coords: Any) -> Iterable[Position]:
    """Recursively walk nested coordinate arrays down to [lon, lat(, z)]."""
    if not coords:
        return
    first = coords[0]
    if isinstance(first, (int, float)):
        # ``coords`` is itself a single position
        yield coords  # type: ignore[misc]
        return
    for item in coords:
        yield from _iter_positions(item)


def haversine_m(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance in metres between two (lon, lat) points."""
    lon1, lat1 = a
    lon2, lat2 = b
    r = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


@dataclass(frozen=True)
class BoundingBox:
    """Geographic bounding box in degrees."""

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    def contains(self, lon: float, lat: float) -> bool:
        return (
            self.min_lon <= lon <= self.max_lon
            and self.min_lat <= lat <= self.max_lat
        )

    def contains_geometry(self, geom: Optional[Geometry]) -> bool:
        if geom is None:
            return False
        for pos in geom.iter_positions():
            if len(pos) >= 2 and self.contains(float(pos[0]), float(pos[1])):
                return True
        return False

    def as_tuple(self) -> Tuple[float, float, float, float]:
        return (self.min_lon, self.min_lat, self.max_lon, self.max_lat)
