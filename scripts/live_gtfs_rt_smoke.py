"""Live smoke test: MBTA VehiclePositions -> Valhalla /trace_attributes ->
aggregate_by_edge. Confirms the end-to-end probe pipeline works against the
real running Valhalla (Mode A path). Doesn't write traffic.tar (that needs
shell access on the tile host)."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# Load .env manually (freetraffic.cli does this on entry, but this is a script).
env = Path(__file__).resolve().parent.parent / ".env"
if env.exists():
    for line in env.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import httpx
from google.transit import gtfs_realtime_pb2

from freetraffic.probes import GtfsRtProbeTracker, VehicleSample, aggregate_by_edge
from freetraffic.probes.gtfs_rt import map_match_probes
from freetraffic.routing import ValhallaClient


async def main() -> int:
    # Read a snapshot; fetch a second one 25s later to build 2-point probe samples.
    ngrok = os.environ.get("FT_VALHALLA_NGROK") == "1"
    valhalla = ValhallaClient(
        os.environ["FT_VALHALLA_URL"],
        username=os.environ.get("FT_VALHALLA_USER"),
        password=os.environ.get("FT_VALHALLA_PASS"),
        ngrok_skip=ngrok,
    )

    url = "https://cdn.mbta.com/realtime/VehiclePositions.pb"
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as ac:
        def _snapshot(pb: bytes) -> list[VehicleSample]:
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(pb)
            out: list[VehicleSample] = []
            for e in feed.entity:
                if not e.HasField("vehicle"):
                    continue
                v = e.vehicle
                if not v.HasField("position"):
                    continue
                vid = v.vehicle.id or e.id
                ts = int(v.timestamp) if v.timestamp else int(time.time())
                out.append(
                    VehicleSample(
                        vehicle_id=vid,
                        lat=v.position.latitude,
                        lon=v.position.longitude,
                        timestamp=ts,
                    )
                )
            return out

        r1 = await ac.get(url); r1.raise_for_status()
        s1 = _snapshot(r1.content)
        print(f"snapshot 1: {len(s1)} vehicles; t0={s1[0].timestamp if s1 else None}")
        await asyncio.sleep(60)
        r2 = await ac.get(url); r2.raise_for_status()
        s2 = _snapshot(r2.content)
        print(f"snapshot 2: {len(s2)} vehicles; t1={s2[0].timestamp if s2 else None}")

    tracker = GtfsRtProbeTracker()
    tracker.update(s1)
    probes = tracker.update(s2)
    print(f"probes derived: {len(probes)}")
    if not probes:
        print("no probes; agency likely idle -- rerun")
        return 1

    # Only pass ones with meaningful motion (skip parked buses)
    moving = [p for p in probes if p.speed_kph and p.speed_kph > 5]
    print(f"moving probes (>5 kph): {len(moving)}")
    sample = moving[:40]  # cap for speed

    matched = await map_match_probes(sample, valhalla, costing="auto", concurrency=6)
    print(f"map-matched: {len(matched)}/{len(sample)}")
    edge_speeds = aggregate_by_edge(matched, source_id="mbta-vp")
    print(f"unique edges: {len(edge_speeds)}")
    for sp in edge_speeds[:5]:
        print(f"  edge {sp.link_id}: {sp.speed_kph:.1f} kph "
              f"(freeflow={sp.freeflow_kph and round(sp.freeflow_kph,1)}, "
              f"raw={sp.raw})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
