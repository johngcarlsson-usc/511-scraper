import asyncio
import json
from pathlib import Path

import httpx
import pytest

from freetraffic.catalog import FeedSpec, load_builtin_catalog
from freetraffic.client import collect_feed
from freetraffic.models import EventType, Severity
from freetraffic.parsers import (
    parse_arcgis,
    parse_caltrans_lcs,
    parse_cbp_border_wait,
    parse_ibi511_events,
    parse_massdot_events,
    parse_ohgo,
    parse_socrata,
)

FX = Path(__file__).parent / "fixtures"


def _text(name):
    return (FX / name).read_text()


def _json(name):
    return json.loads(_text(name))


# --- CBP border wait (XML) --------------------------------------------------
def test_cbp_border_wait():
    events = parse_cbp_border_wait(_text("cbp_waittimes.xml"), source_id="cbp")
    assert len(events) == 2
    sy = next(e for e in events if "San Ysidro" in (e.headline or ""))
    assert sy.event_type is EventType.RESTRICTION
    assert sy.severity is Severity.MAJOR          # 45 min
    assert sy.raw["passenger_standard_delay_min"] == 45
    assert sy.raw["commercial_standard_delay_min"] == 20
    assert sy.updated is not None and sy.updated.year == 2026
    otay = next(e for e in events if "Otay" in (e.headline or ""))
    assert otay.severity is Severity.MINOR        # 15 min


def test_cbp_tolerates_garbage():
    assert parse_cbp_border_wait("not xml <<", source_id="cbp") == []
    assert parse_cbp_border_wait("", source_id="cbp") == []


# --- OHGO -------------------------------------------------------------------
def test_ohgo():
    events = parse_ohgo(_json("ohgo.json"), source_id="ohgo")
    assert len(events) == 2
    c = next(e for e in events if e.id == "ohgo:c1")
    assert c.event_type is EventType.CONSTRUCTION and not c.impact.closed
    assert c.geometry.type == "Point"
    i = next(e for e in events if e.id == "ohgo:i2")
    assert i.event_type is EventType.INCIDENT and i.impact.closed
    assert i.severity is Severity.MAJOR


# --- Generic ArcGIS (Esri JSON + Web-Mercator) ------------------------------
def test_arcgis_esri_mercator():
    events = parse_arcgis(_json("arcgis_esri.json"), source_id="ag", jurisdiction="CA")
    assert len(events) == 1
    e = events[0]
    assert e.event_type is EventType.CONSTRUCTION
    assert e.roads[0].name == "Main St"
    assert e.geometry.type == "Point"
    lon, lat = e.geometry.coordinates
    # Web-Mercator converted back to ~ SF Bay lon/lat
    assert -123 < lon < -122 and 37 < lat < 38


def test_arcgis_geojson_passthrough():
    payload = {"features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-73.9, 40.8]},
        "properties": {"OBJECTID": 1, "type": "Incident", "road": "Broadway",
                       "description": "Crash"},
    }]}
    events = parse_arcgis(payload, source_id="ag")
    assert events[0].event_type is EventType.INCIDENT
    assert events[0].geometry.coordinates == [-73.9, 40.8]


# --- Generic Socrata --------------------------------------------------------
def test_socrata():
    events = parse_socrata(_json("socrata.json"), source_id="soc", jurisdiction="NY")
    assert len(events) == 2
    closure = next(e for e in events if e.id == "soc:s1")
    assert closure.event_type is EventType.CLOSURE and closure.impact.closed
    assert closure.geometry.type == "Point"
    crash = next(e for e in events if e.id == "soc:s2")
    assert crash.event_type is EventType.INCIDENT
    assert crash.geometry.coordinates == [-73.9, 40.8]  # from location point column


# --- Caltrans LCS (XML) -----------------------------------------------------
def test_caltrans_lcs():
    events = parse_caltrans_lcs(_text("caltrans_lcs.xml"), source_id="ct")
    assert len(events) == 1
    e = events[0]
    assert e.event_type is EventType.CONSTRUCTION          # "Lane" closure, not full
    assert e.geometry.type == "LineString"
    assert e.roads[0].name == "I-5" and e.roads[0].direction == "North"
    assert e.impact.lanes_total == 2 and e.impact.lanes_closed == 2
    assert e.updated is not None and e.updated.year == 2026
    assert e.starts is not None and e.ends is not None


# --- MassDOT events (XML) ---------------------------------------------------
def test_massdot_events():
    events = parse_massdot_events(_text("massdot_events.xml"), source_id="ma")
    assert len(events) == 1
    e = events[0]
    assert e.event_type is EventType.INCIDENT
    assert e.roads[0].name == "I-90" and e.roads[0].direction == "WB"
    assert e.geometry.type == "Point"
    assert "Exit MM78.6" in (e.headline or "")


# --- IBI511 NEW generation (epoch-seconds timestamps) -----------------------
def test_ibi511_new_generation_epoch():
    events = parse_ibi511_events(_json("ibi511_new_events.json"), source_id="ut", jurisdiction="UT")
    assert len(events) == 1
    e = events[0]
    assert e.event_type is EventType.CONSTRUCTION          # "roadwork"
    assert e.geometry.type == "Point"
    # epoch-seconds 1696369067 -> Oct 2023 (NOT misparsed as ms)
    assert e.updated is not None and e.updated.year == 2023


# --- CBP coordinate join (bundled BTS lookup) -------------------------------
def test_cbp_geolocates_known_port():
    events = parse_cbp_border_wait(_text("cbp_with_known_port.xml"), source_id="cbp")
    assert len(events) == 1
    # port_number 070801 -> BTS port_code 0708 (Alexandria Bay) -> has coords
    assert events[0].geometry is not None and events[0].geometry.type == "Point"
    lon, lat = events[0].geometry.coordinates
    assert round(lat, 1) == 44.3 and round(lon, 1) == -76.0


# --- catalog + the new XML fetch path --------------------------------------
def test_registry_has_new_feeds_and_formats():
    cat = load_builtin_catalog()
    cbp = cat.get("cbp-border-wait")
    assert cbp is not None and cbp.kind == "cbp" and cbp.response_format == "xml"
    assert cat.get("ohgo-construction").kind == "ohgo"
    assert cat.get("arcgis-ia-cars").kind == "arcgis"
    assert cat.get("caltrans-lcs-d03").response_format == "xml"
    # IBI NEW vs OLD generations both present, no fictional GetTrafficSpeeds feed
    assert "v2/get/event" in cat.get("ibi511-nv-events").url        # NEW gen
    assert cat.get("ibi511-ny-events").url.endswith("/api/GetEvents")  # OLD gen
    assert cat.get("ibi511-ny-speeds") is None                      # fictional, removed
    assert cat.get("ibi511-wi-traveltimes").kind == "ibi511_traveltimes"
    assert cbp.produces_events


def test_client_xml_path():
    """An xml feed: fetcher returns text, parser consumes the string."""
    def handler(request):
        return httpx.Response(200, text=_text("cbp_waittimes.xml"),
                              headers={"content-type": "application/xml"})

    feed = FeedSpec(id="cbp", name="CBP", kind="cbp",
                    url="https://bwt.example/api/waittimes", response_format="xml")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as ac:
            return await collect_feed(feed, client=ac)

    events, speeds = asyncio.run(run())
    assert len(events) == 2 and speeds == []


def test_client_accept_header_matches_response_format():
    """Regression: CBP (and any other content-negotiated XML feed) was getting
    an ``Accept: application/json`` header and silently coming back as JSON,
    which the XML parser then rejected as 'not xml'. The fetcher must request
    the format the parser actually wants."""
    captured = {}

    def handler(request):
        captured["accept"] = request.headers.get("accept", "")
        if "xml" in captured["accept"]:
            return httpx.Response(200, text="<border_wait_time/>",
                                  headers={"content-type": "application/xml"})
        return httpx.Response(200, json={"events": []})

    xml_feed = FeedSpec(id="cbp", name="CBP", kind="cbp",
                       url="https://bwt.example/api/waittimes", response_format="xml")
    json_feed = FeedSpec(id="ev", name="ev", kind="ibi511",
                        url="https://x.example/api/GetEvents")

    async def run(feed):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as ac:
            await collect_feed(feed, client=ac)

    asyncio.run(run(xml_feed))
    assert "xml" in captured["accept"] and "*/*" not in captured["accept"]

    asyncio.run(run(json_feed))
    assert captured["accept"] == "application/json"
