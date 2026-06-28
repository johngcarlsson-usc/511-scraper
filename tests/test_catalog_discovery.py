from freetraffic.catalog import FeedSpec, load_builtin_catalog
from freetraffic.discovery import registry_rows_to_feeds


def test_builtin_catalog_loads():
    cat = load_builtin_catalog()
    assert len(cat) >= 1
    sf = cat.get("sf-bay-open511")
    assert sf is not None
    assert sf.kind == "open511"
    assert sf.needs_key is True
    assert "CA" in cat.jurisdictions()


def test_catalog_select_filters():
    cat = load_builtin_catalog()
    open511 = cat.select(kind="open511", enabled_only=False)
    assert all(f.kind == "open511" for f in open511)
    events = cat.select(events_only=True, enabled_only=False)
    assert all(f.produces_events for f in events)
    speeds = cat.select(speeds_only=True, enabled_only=False)
    assert all(f.produces_speeds for f in speeds)


def test_feedspec_key_from_env(monkeypatch):
    spec = FeedSpec(id="x", name="X", kind="wzdx", url="http://x", api_key_env="FT_TEST_KEY")
    assert spec.has_key is False
    monkeypatch.setenv("FT_TEST_KEY", "abc")
    assert spec.has_key is True
    assert spec.api_key == "abc"


def test_registry_rows_to_feeds():
    rows = [
        {
            "feedname": "Iowa DOT Work Zones",
            "apiurl": "https://example.iowadot.gov/wzdx",
            "state": "Iowa",
            "version": "4.0",
            "apikeyrequired": "no",
        },
        {
            "datafeed_name": "Some County",
            "url": "https://example.county.gov/feed",
            "issuingorganization": "Maricopa County, Arizona",
            "apikeyrequired": "yes",
        },
        {"feedname": "no url here"},  # dropped: no url
    ]
    feeds = registry_rows_to_feeds(rows)
    assert len(feeds) == 2
    iowa = feeds[0]
    assert iowa.kind == "wzdx"
    assert iowa.jurisdiction == "IA"
    assert iowa.enabled is True
    az = feeds[1]
    assert az.jurisdiction == "AZ"
    assert az.needs_key is True
    assert az.enabled is False  # key-required feeds default off
    # ids are unique + slugged
    assert len({f.id for f in feeds}) == 2
