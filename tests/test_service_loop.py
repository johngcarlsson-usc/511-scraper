"""Offline tests for the GTFS-RT service loop (roadmap #1).

Everything runs against an in-process httpx.MockTransport: a synthetic
VehiclePositions protobuf for the feed and a canned /trace_attributes response
for Valhalla map-matching. No live network, no API keys.
"""

import asyncio
import time

import httpx
import pytest

from freetraffic.models import LinkSpeed
from freetraffic.routing import ValhallaClient
from freetraffic.service import AgencyFeed, EdgeSpeedStore, run_service_loop

gtfs_pb = pytest.importorskip("google.transit.gtfs_realtime_pb2")


def _build_vehicle_positions(vehicles, *, timestamp):
    """vehicles: list of (id, lat, lon, ts) -> serialized FeedMessage bytes."""
    feed = gtfs_pb.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.incrementality = gtfs_pb.FeedHeader.FULL_DATASET
    feed.header.timestamp = timestamp
    for vid, lat, lon, ts in vehicles:
        ent = feed.entity.add()
        ent.id = vid
        v = ent.vehicle
        v.vehicle.id = vid
        v.timestamp = ts
        v.position.latitude = lat
        v.position.longitude = lon
    return feed.SerializeToString()


# Two buses, each moving ~333 m (~0.003 deg lat) over 30 s => ~40 km/h.
# Timestamps are anchored near "now" so the loop's TTL prune keeps them fresh
# (real GTFS-RT feeds always carry current timestamps).
def _ticks(base_ts):
    tick1 = [("v1", 42.3601, -71.0589, base_ts), ("v2", 42.3501, -71.0689, base_ts)]
    tick2 = [("v1", 42.3631, -71.0589, base_ts + 30),
             ("v2", 42.3531, -71.0689, base_ts + 30)]
    return tick1, tick2


def _make_transport():
    """One MockTransport serving both the feed (GET .pb) and Valhalla (POST)."""
    base_ts = int(time.time())
    tick1, tick2 = _ticks(base_ts)
    state = {"feed_gets": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".pb"):
            state["feed_gets"] += 1
            payload = tick1 if state["feed_gets"] == 1 else tick2
            ts = base_ts if state["feed_gets"] == 1 else base_ts + 30
            return httpx.Response(200, content=_build_vehicle_positions(payload, timestamp=ts))
        if request.url.path.endswith("/trace_attributes"):
            import json

            body = json.loads(request.content)
            first_lat = body["shape"][0]["lat"]
            # Deterministic edge id from the probe's start latitude so the two
            # buses land on two distinct edges.
            edge_id = int(round(first_lat * 1000))
            return httpx.Response(200, json={"edges": [{"id": edge_id}]})
        return httpx.Response(404)

    return httpx.MockTransport(handler), state


def test_service_loop_two_ticks_produces_edge_speeds():
    transport, state = _make_transport()
    ticks_seen = []

    async def run():
        async with httpx.AsyncClient(transport=transport) as feed_client, \
                httpx.AsyncClient(transport=transport) as val_inner:
            val = ValhallaClient("https://valhalla.test", client=val_inner)
            store = EdgeSpeedStore(ttl_s=10_000)
            await run_service_loop(
                [AgencyFeed(source_id="test-rt", url="https://feed.test/vp.pb",
                            jurisdiction="MA")],
                val,
                interval_s=0,            # no real waiting in tests
                store=store,
                max_ticks=2,
                http_client=feed_client,
                on_tick=lambda s: ticks_seen.append(s),
            )
            return store

    store = asyncio.run(run())

    # First tick only seeds the tracker (no prior position => no probes);
    # speeds appear on the second tick.
    assert len(ticks_seen) == 2
    assert ticks_seen[0][0].probes == 0
    assert ticks_seen[0][0].edges == 0
    assert ticks_seen[1][0].probes == 2
    assert ticks_seen[1][0].matched == 2
    assert ticks_seen[1][0].edges == 2

    # Two distinct edges (int edge-id keyed) with plausible (~40 km/h) speeds.
    speeds = store.speeds()
    assert len(speeds) == 2
    assert all(isinstance(k, int) for k in speeds)
    for kph in speeds.values():
        assert 20 < kph < 60


def test_service_loop_mode_b_writes_to_tar_updater():
    transport, _ = _make_transport()
    written = []

    class FakeTarUpdater:
        def apply_link_speeds(self, links):
            written.extend(links)
            return len(links)

    async def run():
        async with httpx.AsyncClient(transport=transport) as feed_client, \
                httpx.AsyncClient(transport=transport) as val_inner:
            val = ValhallaClient("https://valhalla.test", client=val_inner)
            await run_service_loop(
                [AgencyFeed(source_id="test-rt", url="https://feed.test/vp.pb")],
                val,
                interval_s=0,
                tar_updater=FakeTarUpdater(),
                max_ticks=2,
                http_client=feed_client,
            )

    asyncio.run(run())
    assert len(written) == 2
    assert all(isinstance(ls, LinkSpeed) for ls in written)


def test_edge_speed_store_newest_wins_and_prune():
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    store = EdgeSpeedStore(ttl_s=300)
    old = LinkSpeed(source_id="s", speed_kph=10.0, link_id="42",
                    observed_at=now - timedelta(seconds=20))
    new = LinkSpeed(source_id="s", speed_kph=55.0, link_id="42",
                    observed_at=now - timedelta(seconds=5))
    store.update([old])
    store.update([new])
    assert store.speeds()[42] == 55.0  # newer observation wins (int edge-id key)

    stale = LinkSpeed(source_id="s", speed_kph=30.0, link_id="99",
                      observed_at=now - timedelta(seconds=600))
    store.update([stale])
    dropped = store.prune(now=now)
    assert dropped == 1
    assert 99 not in store.speeds()
    assert 42 in store.speeds()


def test_run_service_loop_requires_an_agency():
    with pytest.raises(ValueError):
        asyncio.run(run_service_loop([], ValhallaClient("https://v.test"), max_ticks=1))
