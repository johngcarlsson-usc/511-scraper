from freetraffic.models import EventType, Severity
from freetraffic.parsers import parse_ibi511_events, parse_ibi511_speeds


def test_ibi511_events(ibi511_events):
    events = parse_ibi511_events(ibi511_events, source_id="ny", jurisdiction="NY")
    assert len(events) == 2

    crash = next(e for e in events if e.id == "ny:NY--1001")
    assert crash.event_type is EventType.INCIDENT
    assert crash.severity is Severity.MAJOR
    assert crash.impact.closed is True
    assert crash.roads[0].name == "I-90"
    assert crash.roads[0].direction == "Eastbound"
    # /Date(ms)/ parsed to a real, tz-aware datetime
    assert crash.updated is not None and crash.updated.year == 2020
    assert crash.geometry.type == "Point"

    work = next(e for e in events if e.id == "ny:NY--1002")
    assert work.event_type is EventType.CONSTRUCTION
    assert work.impact.closed is False
    assert "Lanes affected" in (work.description or "")


def test_ibi511_speeds(ibi511_speeds):
    speeds = parse_ibi511_speeds(ibi511_speeds, source_id="ny", jurisdiction="NY")
    # the record with no Speed is dropped
    assert len(speeds) == 2
    s1 = next(s for s in speeds if s.link_id == "SPD-1")
    # 25 mph -> ~40.2 kph; freeflow 65 mph -> ~104.6 kph
    assert 40 < s1.speed_kph < 41
    assert s1.freeflow_kph and 104 < s1.freeflow_kph < 105
    assert s1.congestion_ratio and s1.congestion_ratio < 0.5  # congested
    assert s1.geometry.type == "Point"


def test_ibi511_records_tolerates_wrapped_and_garbage():
    assert parse_ibi511_events({"events": []}, source_id="x") == []
    assert parse_ibi511_events("nonsense", source_id="x") == []
    assert parse_ibi511_speeds([], source_id="x") == []
