import asyncio
import json

import httpx
import pytest

from freetraffic.geometry import BoundingBox, Geometry, decode_polyline
from freetraffic.models import EventType, Impact, TrafficEvent
from freetraffic.routing import TrafficAwareRouter, ValhallaClient
from freetraffic.routing.service import (
    closures_in_bbox,
    decode_route_points,
    events_near_points,
    exclude_polygons_for,
)
from freetraffic.store import TrafficSnapshot


# --- a tiny polyline encoder (test-only) to build Valhalla-style responses ---
def _encode_val(v: int) -> str:
    v = ~(v << 1) if v < 0 else (v << 1)
    out = ""
    while v >= 0x20:
        out += chr((0x20 | (v & 0x1F)) + 63)
        v >>= 5
    return out + chr(v + 63)


def encode_polyline(coords_lonlat, precision=6) -> str:
    factor = 10 ** precision
    out = ""
    plat = plon = 0
    for lon, lat in coords_lonlat:
        ilat, ilon = round(lat * factor), round(lon * factor)
        out += _encode_val(ilat - plat) + _encode_val(ilon - plon)
        plat, plon = ilat, ilon
    return out


def test_decode_polyline_google_example():
    # canonical Google precision-5 example
    coords = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5)
    assert len(coords) == 3
    lon, lat = coords[0]
    assert round(lat, 3) == 38.5 and round(lon, 3) == -120.2


def test_polyline_roundtrip_precision6():
    pts = [(-73.79, 42.68), (-73.80, 42.90)]
    enc = encode_polyline(pts, precision=6)
    back = decode_polyline(enc, precision=6)
    assert len(back) == 2
    assert round(back[1][0], 5) == -73.80 and round(back[1][1], 5) == 42.90


def _closure_event(lon, lat, type_=EventType.CLOSURE):
    return TrafficEvent(
        id=f"t:{lon},{lat}", source_id="t", event_type=type_,
        impact=Impact(closed=True), geometry=Geometry.point(lon, lat),
        headline="closed",
    )


def test_exclude_polygons_and_closures_in_bbox():
    bbox = BoundingBox(-74, 42, -73, 43)
    inside = _closure_event(-73.79, 42.68)
    outside = _closure_event(-80.0, 40.0)
    closures = closures_in_bbox([inside, outside], bbox)
    assert closures == [inside]
    rings = exclude_polygons_for(closures, buffer_m=35)
    assert len(rings) == 1
    ring = rings[0]
    assert len(ring) == 5 and ring[0] == ring[-1]  # closed ring


def test_events_near_points():
    pts = decode_route_points(
        {"trip": {"legs": [{"shape": encode_polyline([(-73.79, 42.68), (-73.80, 42.90)])}]}}
    )
    near = _closure_event(-73.7901, 42.6801, EventType.INCIDENT)
    far = _closure_event(-72.0, 41.0, EventType.INCIDENT)
    found = events_near_points([near, far], pts, max_dist_m=150)
    assert found == [near]


def test_valhalla_client_builds_authed_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["ngrok"] = request.headers.get("ngrok-skip-browser-warning")
        captured["body"] = json.loads(request.content)
        trip = {"trip": {"summary": {"length": 12.3, "time": 900},
                         "legs": [{"shape": encode_polyline([(-73.79, 42.68), (-73.80, 42.90)])}]}}
        return httpx.Response(200, json=trip)

    transport = httpx.MockTransport(handler)

    async def run():
        async with httpx.AsyncClient(transport=transport) as ac:
            client = ValhallaClient(
                "https://valhalla.example", username="u", password="p",
                ngrok_skip=True, client=ac,
            )
            return await client.route([(-73.79, 42.68), (-73.80, 42.90)],
                                      exclude_polygons=[[[0, 0], [0, 1], [1, 1], [0, 0]]])

    resp = asyncio.run(run())
    assert resp["trip"]["summary"]["length"] == 12.3
    assert captured["url"].endswith("/route")
    assert captured["auth"].startswith("Basic ")
    assert captured["ngrok"] == "1"
    assert captured["body"]["locations"][0] == {"lon": -73.79, "lat": 42.68}
    assert "exclude_polygons" in captured["body"]


def test_traffic_aware_router_end_to_end():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        trip = {"trip": {"summary": {"length": 20.0, "time": 1200},
                         "legs": [{"shape": encode_polyline([(-73.79, 42.68), (-73.80, 42.90)])}]}}
        return httpx.Response(200, json=trip)

    snapshot = TrafficSnapshot(events=[
        _closure_event(-73.7902, 42.6802),                       # on route, closed
        _closure_event(-72.0, 41.0),                             # far away
    ])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as ac:
            client = ValhallaClient("https://v.example", client=ac)
            router = TrafficAwareRouter(client)
            return await router.route((-73.79, 42.68), (-73.80, 42.90), snapshot,
                                      on_route_dist_m=200)

    result = asyncio.run(run())
    assert result.length_km == 20.0
    assert result.exclusions_applied == 1            # the nearby closure was excluded
    assert "exclude_polygons" in seen["body"]
    assert len(result.events_on_route) == 1          # only the on-route closure annotated
