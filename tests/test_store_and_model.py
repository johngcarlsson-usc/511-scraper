import json

from freetraffic.geometry import BoundingBox, Geometry, haversine_m
from freetraffic.models import TrafficEvent, parse_datetime
from freetraffic.parsers import parse_open511, parse_wzdx
from freetraffic.store import TrafficSnapshot


def test_snapshot_geojson_roundtrips(open511_payload):
    events = parse_open511(open511_payload, source_id="src", jurisdiction="CA")
    snap = TrafficSnapshot(events=events)
    gj = json.loads(snap.dumps())
    assert gj["type"] == "FeatureCollection"
    assert gj["metadata"]["event_count"] == 2
    assert all(f["type"] == "Feature" for f in gj["features"])


def test_dedupe_keeps_newest(open511_payload, wzdx_payload):
    events = parse_open511(open511_payload, source_id="a", jurisdiction="CA")
    # duplicate the same native id from a different source feed, but older
    dup = parse_open511(open511_payload, source_id="b", jurisdiction="CA")
    for e in dup:
        e.updated = parse_datetime("2000-01-01T00:00:00Z")
    snap = TrafficSnapshot(events=events + dup).dedupe()
    # 2 unique native ids despite 4 input events
    assert len(snap.events) == 2
    # the kept copies are the newer (source "a") ones
    assert all(e.source_id == "a" for e in snap.events)


def test_geometry_representative_point_and_bbox():
    line = Geometry.line([[-122.5, 37.7], [-122.3, 37.9]])
    pt = line.representative_point()
    assert round(pt[0], 2) == -122.40
    bbox = BoundingBox(-123, 37, -122, 38)
    assert bbox.contains_geometry(line)
    assert not BoundingBox(0, 0, 1, 1).contains_geometry(line)


def test_haversine_reasonable():
    # ~1.11 km per 0.01 deg latitude
    d = haversine_m((-122.0, 37.0), (-122.0, 37.01))
    assert 1100 < d < 1120


def test_parse_datetime_variants():
    assert parse_datetime("2026-06-28T06:30:00Z").tzinfo is not None
    assert parse_datetime("2026-06-28").year == 2026
    assert parse_datetime(None) is None
    assert parse_datetime(1_750_000_000).year >= 2025  # epoch seconds
