import json
from pathlib import Path

import pytest

from freetraffic.models import EventType, Severity
from freetraffic.parsers import parse_nws_alerts, parse_wsdot_travel_times
from freetraffic.probes import (
    GtfsRtProbeTracker,
    ProbeSpeed,
    VehicleSample,
    aggregate_by_edge,
)
from freetraffic.probes.tomtom import _flow_to_link_speed

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


# --- WSDOT travel times -> LinkSpeed ----------------------------------------
def test_wsdot_travel_times():
    speeds = parse_wsdot_travel_times(_load("wsdot_travel_times.json"), source_id="wsdot")
    assert len(speeds) == 1  # the zero-distance row is dropped
    s = speeds[0]
    # 10 mi over 20 min current -> ~48.3 kph; 10 min typical -> ~96.6 kph freeflow
    assert 48 < s.speed_kph < 49
    assert s.freeflow_kph and 96 < s.freeflow_kph < 97
    assert round(s.congestion_ratio, 2) == 0.5
    assert s.roadway == "I-5"
    assert s.geometry.type == "LineString"
    assert s.observed_at is not None and s.observed_at.year == 2020


# --- NWS weather alerts -> WEATHER_CONDITION events -------------------------
def test_nws_alerts_filters_to_driving_relevant():
    events = parse_nws_alerts(_load("nws_alerts.json"), source_id="nws", jurisdiction="WA")
    assert len(events) == 1  # Air Quality Alert filtered out
    e = events[0]
    assert e.event_type is EventType.WEATHER_CONDITION
    assert e.severity is Severity.MAJOR
    assert e.geometry.type == "Polygon"
    assert "Winter Storm" in (e.headline or "")
    assert e.ends is not None


# --- GTFS-RT probe tracker (pure logic) ------------------------------------
def test_probe_tracker_basic_speed():
    tracker = GtfsRtProbeTracker(min_dt_s=5, max_dt_s=180)
    # first sample establishes state, yields nothing
    assert tracker.update([VehicleSample("bus1", -122.0, 47.0, 1000)]) == []
    # ~111 m north over 30 s -> ~13.4 kph
    probes = tracker.update([VehicleSample("bus1", -122.0, 47.001, 1030)])
    assert len(probes) == 1
    assert 12 < probes[0].speed_kph < 15
    assert probes[0].vehicle_id == "bus1"


def test_probe_tracker_filters():
    t = GtfsRtProbeTracker(min_dt_s=5, max_dt_s=120, max_speed_kph=130)
    t.update([VehicleSample("b", -122.0, 47.0, 0)])
    # dt too large -> dropped
    assert t.update([VehicleSample("b", -122.0, 47.001, 1000)]) == []
    # GPS jump (huge distance in short time) -> dropped as bad fix
    t.reset()
    t.update([VehicleSample("b", -122.0, 47.0, 0)])
    assert t.update([VehicleSample("b", -120.0, 47.0, 10)]) == []
    # stopped-at-stop sample -> dropped (dwell, not road speed)
    t.reset()
    t.update([VehicleSample("b", -122.0, 47.0, 0)])
    out = t.update([VehicleSample("b", -122.0, 47.001, 30, current_status=1)])
    assert out == []


def test_aggregate_by_edge_median():
    probes = [
        ProbeSpeed("a", (-122, 47), (-122, 47.001), 100, 30, 30.0, edge_id=5),
        ProbeSpeed("b", (-122, 47), (-122, 47.001), 100, 30, 40.0, edge_id=5),
        ProbeSpeed("c", (-122, 47), (-122, 47.001), 100, 30, 50.0, edge_id=5),
        ProbeSpeed("d", (-122, 47), (-122, 47.001), 100, 30, 99.0, edge_id=9),
    ]
    links = aggregate_by_edge(probes, source_id="gtfs", min_samples=2)
    assert len(links) == 1  # edge 9 has only 1 sample, dropped
    assert links[0].link_id == "5"
    assert links[0].speed_kph == 40.0  # median of 30/40/50


# --- GTFS-RT protobuf round trip (needs [gtfs] extra) ----------------------
def test_parse_vehicle_positions_roundtrip():
    pb = pytest.importorskip("google.transit.gtfs_realtime_pb2")
    from freetraffic.probes import parse_vehicle_positions

    feed = pb.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    ent = feed.entity.add()
    ent.id = "e1"
    ent.vehicle.vehicle.id = "bus42"
    ent.vehicle.trip.route_id = "10"
    ent.vehicle.position.latitude = 47.5
    ent.vehicle.position.longitude = -122.3
    ent.vehicle.position.speed = 12.0
    ent.vehicle.timestamp = 1_700_000_000

    samples = parse_vehicle_positions(feed.SerializeToString())
    assert len(samples) == 1
    s = samples[0]
    assert s.vehicle_id == "bus42" and s.route_id == "10"
    assert round(s.lat, 4) == 47.5 and round(s.lon, 4) == -122.3
    assert s.speed_mps == 12.0


# --- TomTom freemium flow parsing ------------------------------------------
def test_tomtom_flow_to_link_speed():
    data = {
        "flowSegmentData": {
            "currentSpeed": 40,
            "freeFlowSpeed": 100,
            "confidence": 0.9,
            "coordinates": {
                "coordinate": [
                    {"latitude": 47.5, "longitude": -122.3},
                    {"latitude": 47.51, "longitude": -122.31},
                ]
            },
        }
    }
    ls = _flow_to_link_speed(data, "tomtom")
    assert ls.speed_kph == 40 and ls.freeflow_kph == 100
    assert ls.confidence == 0.9
    assert ls.geometry.type == "LineString"
    assert round(ls.congestion_ratio, 1) == 0.4
    assert _flow_to_link_speed({}, "tomtom") is None
