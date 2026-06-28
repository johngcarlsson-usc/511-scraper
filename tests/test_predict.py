from datetime import datetime, timezone

from freetraffic.geometry import Geometry
from freetraffic.models import EventType, Impact, LinkSpeed, Severity, TrafficEvent
from freetraffic.predict import (
    FusionConfig,
    RouteEdge,
    predict_edges,
    predict_eta,
    route_edges_from_trace,
)
from freetraffic.store import TrafficSnapshot


# --- minimal polyline6 encoder (test only) ---------------------------------
def _enc(v):
    v = ~(v << 1) if v < 0 else (v << 1)
    out = ""
    while v >= 0x20:
        out += chr((0x20 | (v & 0x1F)) + 63)
        v >>= 5
    return out + chr(v + 63)


def encode_polyline(coords_lonlat, precision=6):
    factor = 10 ** precision
    out, plat, plon = "", 0, 0
    for lon, lat in coords_lonlat:
        ilat, ilon = round(lat * factor), round(lon * factor)
        out += _enc(ilat - plat) + _enc(ilon - plon)
        plat, plon = ilat, ilon
    return out


P0 = (-122.30, 47.60)
P1 = (-122.31, 47.61)
P2 = (-122.33, 47.62)
MID_A = (-122.305, 47.605)  # midpoint of edge A (P0..P1)


def _trace():
    return {
        "shape": encode_polyline([P0, P1, P2]),
        "edges": [
            {"id": 100, "length": 1.0, "speed": 100,
             "begin_shape_index": 0, "end_shape_index": 1},
            {"id": 200, "length": 2.0, "speed": 80,
             "begin_shape_index": 1, "end_shape_index": 2},
        ],
    }


def _edges():
    return route_edges_from_trace(_trace())


def test_route_edges_from_trace():
    edges = _edges()
    assert len(edges) == 2
    assert edges[0].edge_id == 100 and edges[0].length_m == 1000.0
    assert edges[0].base_speed_kph == 100 and edges[0].geometry.type == "LineString"
    assert edges[1].length_m == 2000.0


def test_base_eta_no_traffic():
    eta = predict_eta(_edges(), TrafficSnapshot())
    # 1000m@100kph = 36s ; 2000m@80kph = 90s
    assert round(eta.base_time_s) == 126
    assert round(eta.predicted_time_s) == 126
    assert eta.delay_s == 0


def test_measured_speed_overrides_edge():
    snap = TrafficSnapshot(speeds=[
        LinkSpeed(source_id="gtfs", speed_kph=40, link_id="100", confidence=0.9)
    ])
    preds = predict_edges(_edges(), snap)
    e100 = next(p for p in preds if p.edge_id == 100)
    assert e100.source == "measured:gtfs"
    assert e100.predicted_speed_kph == 40
    # 1000m@40 = 90s vs 36s base -> +54s delay on that edge
    assert round(e100.predicted_time_s - e100.base_time_s) == 54


def test_full_closure_impassable():
    snap = TrafficSnapshot(events=[
        TrafficEvent(id="x:1", source_id="x", event_type=EventType.CLOSURE,
                     impact=Impact(closed=True), geometry=Geometry.point(*MID_A))
    ])
    preds = predict_edges(_edges(), snap)
    e100 = next(p for p in preds if p.edge_id == 100)
    assert e100.impassable and e100.source == "closed"
    e200 = next(p for p in preds if p.edge_id == 200)
    assert not e200.impassable  # closure only near edge A


def test_incident_lane_penalty_when_no_measurement():
    # 3 lanes, 2 closed -> 1/3 open -> factor ~0.333 on edge A's 100 kph base
    snap = TrafficSnapshot(events=[
        TrafficEvent(id="x:2", source_id="x", event_type=EventType.INCIDENT,
                     severity=Severity.MAJOR,
                     impact=Impact(lanes_total=3, lanes_closed=2),
                     geometry=Geometry.point(*MID_A))
    ])
    e100 = next(p for p in predict_edges(_edges(), snap) if p.edge_id == 100)
    assert e100.source == "modeled"
    assert round(e100.predicted_speed_kph) == 33


def test_weather_polygon_multiplier():
    poly = Geometry("Polygon", [[[-122.31, 47.60], [-122.30, 47.60],
                                 [-122.30, 47.61], [-122.31, 47.61],
                                 [-122.31, 47.60]]])
    snap = TrafficSnapshot(events=[
        TrafficEvent(id="w:1", source_id="nws", event_type=EventType.WEATHER_CONDITION,
                     headline="Winter Storm Warning", geometry=poly)
    ])
    preds = predict_edges(_edges(), snap)
    e100 = next(p for p in preds if p.edge_id == 100)
    assert "weather" in e100.factors and e100.predicted_speed_kph == 70  # 100 * 0.7
    e200 = next(p for p in preds if p.edge_id == 200)
    assert e200.source == "base"  # outside the polygon


def test_reduced_speed_limit_caps():
    snap = TrafficSnapshot(events=[
        TrafficEvent(id="c:1", source_id="wzdx", event_type=EventType.CONSTRUCTION,
                     impact=Impact(reduced_speed_kph=50), geometry=Geometry.point(*MID_A))
    ])
    e100 = next(p for p in predict_edges(_edges(), snap) if p.edge_id == 100)
    assert e100.predicted_speed_kph == 50


def test_measurement_beats_modeled_no_double_count():
    # both an incident AND a measured speed on edge A -> measurement wins
    snap = TrafficSnapshot(
        speeds=[LinkSpeed(source_id="wsdot", speed_kph=55, link_id="100")],
        events=[TrafficEvent(id="x:3", source_id="x", event_type=EventType.INCIDENT,
                             severity=Severity.MAJOR, impact=Impact(closed=False),
                             geometry=Geometry.point(*MID_A))],
    )
    e100 = next(p for p in predict_edges(_edges(), snap) if p.edge_id == 100)
    assert e100.source == "measured:wsdot" and e100.predicted_speed_kph == 55


def test_stale_measurement_ignored():
    old = datetime(2000, 1, 1, tzinfo=timezone.utc)
    snap = TrafficSnapshot(speeds=[
        LinkSpeed(source_id="x", speed_kph=10, link_id="100", observed_at=old)
    ])
    e100 = next(p for p in predict_edges(_edges(), snap) if p.edge_id == 100)
    assert e100.source != "measured:x"  # too old, falls back to base


def test_eta_scenario_combines_signals():
    snap = TrafficSnapshot(
        speeds=[LinkSpeed(source_id="gtfs", speed_kph=20, link_id="200", confidence=0.8)],
        events=[TrafficEvent(id="x:4", source_id="x", event_type=EventType.INCIDENT,
                             severity=Severity.MODERATE, impact=Impact(lanes_total=2, lanes_closed=1),
                             geometry=Geometry.point(*MID_A))],
    )
    eta = predict_eta(_edges(), snap)
    d = eta.to_dict()
    assert d["measured_edge_count"] == 1
    assert eta.delay_s > 0          # both edges slower than free-flow
    assert not eta.has_impassable
