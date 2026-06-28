import asyncio

import httpx
import pytest

from freetraffic.models import LinkSpeed
from freetraffic.routing import TrafficAwareRouter, ValhallaClient
from freetraffic.store import TrafficSnapshot
from freetraffic.testkit import Cassette, RecordingTransport, ReplayTransport
from freetraffic.testkit.cassette import request_key


def _enc(v):
    v = ~(v << 1) if v < 0 else (v << 1)
    out = ""
    while v >= 0x20:
        out += chr((0x20 | (v & 0x1F)) + 63)
        v >>= 5
    return out + chr(v + 63)


def encode_polyline(coords, precision=6):
    factor = 10 ** precision
    out, plat, plon = "", 0, 0
    for lon, lat in coords:
        ilat, ilon = round(lat * factor), round(lon * factor)
        out += _enc(ilat - plat) + _enc(ilon - plon)
        plat, plon = ilat, ilon
    return out


P0, P1, P2 = (-122.30, 47.60), (-122.31, 47.61), (-122.33, 47.62)


def test_request_key_strips_secrets():
    req = httpx.Request("GET", "https://api.example/x?key=SECRET&state=NY")
    key = request_key(req)
    assert "SECRET" not in key and "key=" not in key
    assert key == "GET /x?state=NY"


def test_record_then_replay_roundtrip(tmp_path):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"trip": {"summary": {"length": 5.0}}})

    cas = Cassette()

    async def record():
        rec = RecordingTransport(httpx.MockTransport(handler), cas)
        async with httpx.AsyncClient(transport=rec) as ac:
            client = ValhallaClient("https://v.example", username="u", password="p", client=ac)
            return await client.route([P0, P1])

    resp = asyncio.run(record())
    assert resp["trip"]["summary"]["length"] == 5.0
    assert len(cas.interactions) == 1
    assert cas.interactions[0]["key"] == "POST /route"

    path = tmp_path / "cas.json"
    cas.save(str(path))
    loaded = Cassette.load(str(path))

    async def replay():
        async with httpx.AsyncClient(transport=ReplayTransport(loaded)) as ac:
            client = ValhallaClient("https://v.example", client=ac)
            return await client.route([P0, P1])

    replayed = asyncio.run(replay())
    assert replayed["trip"]["summary"]["length"] == 5.0
    assert calls["n"] == 1  # replay did NOT hit the backend again


def test_route_with_eta_end_to_end_over_replay():
    """The real client + trace + fusion path, driven entirely by a cassette."""
    shape = encode_polyline([P0, P1, P2])
    cas = Cassette([
        {
            "key": "POST /route",
            "request": {"method": "POST", "url": "https://v.example/route"},
            "response": {
                "status": 200, "is_json": True,
                "body": {"trip": {"summary": {"length": 3.0, "time": 126},
                                  "legs": [{"shape": shape}]}},
            },
        },
        {
            "key": "POST /trace_attributes",
            "request": {"method": "POST", "url": "https://v.example/trace_attributes"},
            "response": {
                "status": 200, "is_json": True,
                "body": {
                    "shape": shape,
                    "edges": [
                        {"id": 100, "length": 1.0, "speed": 100,
                         "begin_shape_index": 0, "end_shape_index": 1},
                        {"id": 200, "length": 2.0, "speed": 80,
                         "begin_shape_index": 1, "end_shape_index": 2},
                    ],
                },
            },
        },
    ])

    snap = TrafficSnapshot(speeds=[
        LinkSpeed(source_id="gtfs-rt", speed_kph=20, link_id="200", confidence=0.9)
    ])

    async def run():
        async with httpx.AsyncClient(transport=ReplayTransport(cas)) as ac:
            router = TrafficAwareRouter(ValhallaClient("https://v.example", client=ac))
            return await router.route_with_eta(P0, P2, snap)

    result = asyncio.run(run())
    assert result.eta is not None
    # edge 200 dropped from 80 -> 20 kph, so predicted >> base
    assert result.eta.predicted_time_s > result.eta.base_time_s
    measured = [e for e in result.eta.edges if e.source.startswith("measured")]
    assert len(measured) == 1 and measured[0].edge_id == 200
