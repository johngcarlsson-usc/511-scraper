from freetraffic.models import EventStatus, EventType, Severity
from freetraffic.parsers import parse_open511, parse_wzdx


def test_open511_basic(open511_payload):
    events = parse_open511(open511_payload, source_id="src", jurisdiction="CA")
    assert len(events) == 2

    e1 = next(e for e in events if e.id == "src:EVT-1")
    assert e1.event_type is EventType.CONSTRUCTION
    assert e1.severity is Severity.MODERATE
    assert e1.jurisdiction == "CA"
    assert e1.geometry.type == "Point"
    # 3 lanes, 1 open -> 2 closed, not a full closure
    assert e1.impact.lanes_total == 3
    assert e1.impact.lanes_closed == 2
    assert e1.impact.closed is False
    assert e1.impact.lane_fraction_open == 1 / 3
    assert e1.updated is not None and e1.starts is not None


def test_open511_full_closure(open511_payload):
    events = parse_open511(open511_payload, source_id="src")
    e2 = next(e for e in events if e.id == "src:EVT-2")
    assert e2.event_type is EventType.INCIDENT
    assert e2.impact.closed is True
    assert e2.impact.lane_fraction_open == 0.0


def test_open511_tolerates_garbage():
    assert parse_open511({}, source_id="s") == []
    assert parse_open511({"events": [None, 5, "x"]}, source_id="s") == []


def test_wzdx_v4_core_details(wzdx_payload):
    events = parse_wzdx(wzdx_payload, source_id="ia", jurisdiction="IA")
    assert len(events) == 2

    wz = next(e for e in events if e.id == "ia:wz-100")
    assert wz.event_type is EventType.CONSTRUCTION
    assert wz.roads[0].name == "I-235"
    assert wz.roads[0].direction == "eastbound"
    assert wz.impact.lanes_total == 2
    assert wz.impact.lanes_closed == 1
    assert wz.impact.closed is False
    assert wz.impact.reduced_speed_kph == 72
    assert wz.severity is Severity.MODERATE
    assert wz.status is EventStatus.ACTIVE


def test_wzdx_full_closure_and_string_road(wzdx_payload):
    events = parse_wzdx(wzdx_payload, source_id="ia")
    wz = next(e for e in events if e.id == "ia:wz-101")
    assert wz.impact.closed is True
    assert wz.severity is Severity.MAJOR
    assert wz.roads[0].name == "US-69"  # string road_names normalized to list
