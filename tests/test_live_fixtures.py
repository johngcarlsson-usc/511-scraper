"""Offline tests against trimmed real responses captured under
tests/fixtures/live/. These exercise the parsers on real-world shape drift
so we catch regressions when a feed changes its payload.

Captured on 2026-06-30 from open/keyless endpoints (CBP waittimes XML,
NWS active alerts for CA, NCDOT TIMS via ArcGIS, NYC DOT traffic-speed
Socrata, MBTA GTFS-RT VehiclePositions). API keys, where present in
URLs, were stripped before commit.
"""
from __future__ import annotations

import json
from pathlib import Path

from freetraffic.parsers import (
    parse_arcgis,
    parse_cbp_border_wait,
    parse_nws_alerts,
)
from freetraffic.models import EventType

LIVE = Path(__file__).parent / "fixtures" / "live"


def test_cbp_live_xml_parses_to_events():
    xml = (LIVE / "cbp_waittimes.xml").read_text()
    events = parse_cbp_border_wait(xml, source_id="cbp-border-wait")
    # The CBP feed lists every port whether open or closed; we should get a
    # healthy batch out of a live snapshot.
    assert len(events) >= 50
    assert all(e.event_type is EventType.RESTRICTION for e in events)
    # At least one US-Canada and one US-Mexico port should be present in any
    # nationwide snapshot.
    borders = {e.raw.get("border") for e in events}
    assert "Canadian Border" in borders
    assert "Mexican Border" in borders


def test_nws_live_alerts_parses():
    # State-level snapshot: a small live response shouldn't make the parser
    # crash even when none of the alerts are driving-relevant (CA at capture
    # time only had Lake Wind / Air Quality, both filtered out).
    ca = json.loads((LIVE / "nws_alerts_ca.json").read_text())
    ca_events = parse_nws_alerts(ca, source_id="nws-alerts", jurisdiction="CA")
    assert isinstance(ca_events, list)
    assert all(e.event_type is EventType.WEATHER_CONDITION for e in ca_events)

    # National sample (pre-filtered to driving-relevant categories with
    # geometry) should yield events through the parser.
    nat = json.loads((LIVE / "nws_alerts_national_sample.json").read_text())
    events = parse_nws_alerts(nat, source_id="nws-alerts", jurisdiction=None)
    assert len(events) >= 1
    assert all(e.event_type is EventType.WEATHER_CONDITION for e in events)
    assert all(e.geometry is not None for e in events)


def test_arcgis_live_ncdot_tims_parses():
    gj = json.loads((LIVE / "arcgis_ncdot_tims.geojson").read_text())
    events = parse_arcgis(gj, source_id="arcgis-ncdot-tims", jurisdiction="NC")
    assert len(events) >= 1
    # NCDOT TIMS mostly publishes roadwork/closures.
    types = {e.event_type for e in events}
    assert types & {EventType.CONSTRUCTION, EventType.CLOSURE, EventType.INCIDENT}


def test_gtfs_rt_live_protobuf_decodes():
    # The protobuf bindings are an optional dep; if missing, skip rather than
    # making the suite require them just for the live fixture.
    gtfs_rt = __import__("importlib").util.find_spec("google.transit.gtfs_realtime_pb2")
    if gtfs_rt is None:  # pragma: no cover - environment dependent
        import pytest

        pytest.skip("gtfs-realtime-bindings not installed")
    from google.transit import gtfs_realtime_pb2

    pb = (LIVE / "mbta_vehiclepositions.pb").read_bytes()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(pb)
    assert feed.header.gtfs_realtime_version
    assert len(feed.entity) >= 10
    # At least one entity should carry a VehiclePosition with coordinates.
    have_position = any(
        e.HasField("vehicle") and (e.vehicle.position.latitude or e.vehicle.position.longitude)
        for e in feed.entity
    )
    assert have_position
