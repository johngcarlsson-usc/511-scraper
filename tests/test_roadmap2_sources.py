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
    parse_cbp_border_wait,
    parse_ncdot,
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


# --- NCDOT ------------------------------------------------------------------
def test_ncdot():
    events = parse_ncdot(_json("ncdot.json"), source_id="nc")
    assert len(events) == 2
    con = next(e for e in events if e.id == "nc:1")
    assert con.event_type is EventType.CONSTRUCTION
    assert con.roads[0].name == "I-40"
    assert con.starts is not None and con.ends is not None
    inc = next(e for e in events if e.id == "nc:2")
    assert inc.event_type is EventType.INCIDENT and inc.impact.closed


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


# --- catalog + the new XML fetch path --------------------------------------
def test_registry_has_new_feeds_and_formats():
    cat = load_builtin_catalog()
    cbp = cat.get("cbp-border-wait")
    assert cbp is not None and cbp.kind == "cbp" and cbp.response_format == "xml"
    assert cat.get("ohgo-construction").kind == "ohgo"
    assert cat.get("arcgis-example").kind == "arcgis"
    # the new event kinds are recognized
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
