"""Continuous GTFS-Realtime -> Valhalla traffic service loop (roadmap #1).

A long-running poller that, on a timer and for one or more transit agencies:

1. fetches ``VehiclePositions`` (:func:`probes.gtfs_rt.fetch_vehicle_positions`),
2. diffs them into per-segment probe speeds
   (:class:`probes.gtfs_rt.GtfsRtProbeTracker`, one stateful tracker per agency),
3. map-matches each probe to a Valhalla edge
   (:func:`probes.gtfs_rt.map_match_probes`),
4. aggregates probes into one median :class:`~freetraffic.models.LinkSpeed` per
   edge (:func:`probes.gtfs_rt.aggregate_by_edge`), and
5. applies the result -- to an in-memory :class:`EdgeSpeedStore` (**Mode A**,
   read post-hoc by the router) and/or straight into a Valhalla ``traffic.tar``
   (**Mode B**, :class:`~freetraffic.export.valhalla_traffic.TrafficTarUpdater`).

The first tick for each agency only seeds the tracker (a vehicle needs two
positions before it yields a speed), so probes start flowing from the second
tick on. The loop runs forever by default; pass ``max_ticks`` to bound it (used
by the offline tests). It owns and cleans up any HTTP client / tar updater it
creates.

Network + protobuf live behind lazy imports so importing this module stays
cheap and stdlib-only; the running loop needs the ``[fetch]`` and ``[gtfs]``
extras.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..models import LinkSpeed
from ..probes.gtfs_rt import (
    GtfsRtProbeTracker,
    aggregate_by_edge,
    fetch_vehicle_positions,
    map_match_probes,
)

logger = logging.getLogger("freetraffic.service")

# MBTA (Boston) publishes a keyless, high-volume VehiclePositions feed -- a good
# default for proving the loop and well inside a US-coverage Valhalla tileset.
MBTA_VEHICLE_POSITIONS = "https://cdn.mbta.com/realtime/VehiclePositions.pb"


@dataclass
class AgencyFeed:
    """One transit agency's GTFS-Realtime VehiclePositions feed."""

    source_id: str
    url: str
    jurisdiction: Optional[str] = None
    headers: Optional[Dict[str, str]] = None  # e.g. an API key/token header
    costing: str = "auto"  # Valhalla costing used for map-matching


@dataclass
class TickStats:
    """Per-agency outcome of a single poll, for logging / callers / tests."""

    tick: int
    agency: str
    samples: int = 0
    probes: int = 0
    matched: int = 0
    edges: int = 0
    applied: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EdgeSpeedStore:
    """In-memory latest measured speed per Valhalla edge (Mode A).

    Keeps the most recent :class:`LinkSpeed` per edge id and drops entries older
    than ``ttl_s`` so the snapshot reflects *current* conditions. A
    ``TrafficAwareRouter`` (or the ``traffic-update`` exporter) can read
    :meth:`speeds` to re-time routes without rebuilding Valhalla tiles.
    """

    def __init__(self, ttl_s: float = 600.0) -> None:
        self.ttl_s = ttl_s
        self._by_edge: Dict[int, LinkSpeed] = {}

    def update(self, links: Sequence[LinkSpeed]) -> int:
        """Merge fresh per-edge speeds in, newest-observation-wins. Returns count."""
        n = 0
        for ls in links:
            try:
                edge_id = int(ls.link_id)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            prev = self._by_edge.get(edge_id)
            if prev is None or _observed_key(ls) >= _observed_key(prev):
                self._by_edge[edge_id] = ls
                n += 1
        return n

    def prune(self, *, now: Optional[datetime] = None) -> int:
        """Drop edges whose newest observation is older than ``ttl_s``. Returns dropped."""
        now = now or _utcnow()
        stale = [
            edge_id
            for edge_id, ls in self._by_edge.items()
            if ls.observed_at is not None
            and (now - ls.observed_at).total_seconds() > self.ttl_s
        ]
        for edge_id in stale:
            del self._by_edge[edge_id]
        return len(stale)

    def speeds(self) -> Dict[int, float]:
        """Current ``{edge_id: speed_kph}`` snapshot."""
        return {eid: ls.speed_kph for eid, ls in self._by_edge.items()}

    def link_speeds(self) -> List[LinkSpeed]:
        return list(self._by_edge.values())

    def __len__(self) -> int:
        return len(self._by_edge)


def _observed_key(ls: LinkSpeed) -> float:
    """Sort key for recency; missing timestamps sort oldest."""
    return ls.observed_at.timestamp() if ls.observed_at else 0.0


async def _poll_agency(
    feed: AgencyFeed,
    tracker: GtfsRtProbeTracker,
    valhalla_client: Any,
    *,
    tick: int,
    http_client: Any,
    concurrency: int,
    min_samples: int,
) -> tuple[List[LinkSpeed], TickStats]:
    """Run the full fetch->diff->match->aggregate chain once for one agency."""
    stats = TickStats(tick=tick, agency=feed.source_id)
    try:
        samples = await fetch_vehicle_positions(
            feed.url, client=http_client, headers=feed.headers
        )
        stats.samples = len(samples)
        probes = tracker.update(samples)
        stats.probes = len(probes)
        if not probes:
            return [], stats  # first tick (seeding) or nothing moved
        matched = await map_match_probes(
            probes, valhalla_client, costing=feed.costing, concurrency=concurrency
        )
        stats.matched = len(matched)
        links = aggregate_by_edge(
            matched,
            source_id=feed.source_id,
            jurisdiction=feed.jurisdiction,
            min_samples=min_samples,
        )
        stats.edges = len(links)
        return links, stats
    except Exception as exc:  # noqa: BLE001 - one bad agency must not kill the loop
        stats.error = f"{type(exc).__name__}: {exc}"
        logger.warning("agency %s tick %d failed: %s", feed.source_id, tick, stats.error)
        return [], stats


async def run_service_loop(
    agencies: Sequence[AgencyFeed],
    valhalla_client: Any,
    *,
    interval_s: float = 60.0,
    store: Optional[EdgeSpeedStore] = None,
    tar_updater: Any = None,
    max_ticks: Optional[int] = None,
    concurrency: int = 8,
    min_samples: int = 1,
    tracker_factory: Optional[Callable[[], GtfsRtProbeTracker]] = None,
    http_client: Any = None,
    on_tick: Optional[Callable[[List[TickStats]], None]] = None,
) -> EdgeSpeedStore:
    """Poll ``agencies`` forever (or ``max_ticks`` times), applying edge speeds.

    Applies each tick's per-edge speeds to ``store`` (Mode A, created if not
    given) and, when ``tar_updater`` is supplied, to a Valhalla ``traffic.tar``
    (Mode B). Returns the store. Resources this function creates (the HTTP
    client) are closed on exit; a ``tar_updater`` passed in is left to its owner.
    """
    if not agencies:
        raise ValueError("run_service_loop needs at least one AgencyFeed")
    store = store if store is not None else EdgeSpeedStore()
    make_tracker = tracker_factory or (lambda: GtfsRtProbeTracker())
    trackers: Dict[str, GtfsRtProbeTracker] = {a.source_id: make_tracker() for a in agencies}

    owns_http = http_client is None
    if owns_http:
        from ..client import _require_httpx

        httpx = _require_httpx()
        http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    logger.info(
        "service loop start: %d agenc%s, interval=%.0fs, mode=%s",
        len(agencies),
        "y" if len(agencies) == 1 else "ies",
        interval_s,
        "B (traffic.tar)" if tar_updater is not None else "A (in-memory)",
    )
    tick = 0
    try:
        while max_ticks is None or tick < max_ticks:
            tick += 1
            tick_stats: List[TickStats] = []
            for feed in agencies:
                links, stats = await _poll_agency(
                    feed,
                    trackers[feed.source_id],
                    valhalla_client,
                    tick=tick,
                    http_client=http_client,
                    concurrency=concurrency,
                    min_samples=min_samples,
                )
                if links:
                    store.update(links)
                    if tar_updater is not None:
                        stats.applied = tar_updater.apply_link_speeds(links)
                    else:
                        stats.applied = stats.edges
                logger.info(
                    "tick %d %-14s samples=%d probes=%d matched=%d edges=%d applied=%d%s",
                    stats.tick,
                    stats.agency,
                    stats.samples,
                    stats.probes,
                    stats.matched,
                    stats.edges,
                    stats.applied,
                    f" ERROR {stats.error}" if stats.error else "",
                )
                tick_stats.append(stats)

            dropped = store.prune()
            if dropped:
                logger.debug("pruned %d stale edge(s); store now %d", dropped, len(store))
            if on_tick is not None:
                on_tick(tick_stats)

            if max_ticks is not None and tick >= max_ticks:
                break
            await asyncio.sleep(interval_s)
    except asyncio.CancelledError:  # graceful shutdown (Ctrl-C cancels the task)
        logger.info("service loop cancelled; shutting down after %d tick(s)", tick)
        raise
    finally:
        if owns_http:
            await http_client.aclose()
    logger.info("service loop done: %d tick(s), %d edge(s) in store", tick, len(store))
    return store
