"""Auto-discover WZDx feeds from the live USDOT WZDx Feed Registry.

The registry (hosted on the USDOT Socrata data portal) is the authoritative,
machine-readable list of every work-zone feed that conforms to WZDx/CWZ across
US states, counties and provinces. Pulling it turns "every state" from a
hand-maintained list into a live query: each registry row becomes a
:class:`~freetraffic.catalog.FeedSpec` with ``kind="wzdx"``.

Registry dataset: data.transportation.gov resource ``69qe-yiui``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .catalog import FeedSpec

# Socrata JSON export of the WZDx Feed Registry.
WZDX_REGISTRY_URL = "https://data.transportation.gov/resource/69qe-yiui.json"

# Map common state names -> USPS codes so feeds get a jurisdiction.
_STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _guess_jurisdiction(row: Dict[str, Any]) -> Optional[str]:
    for key in ("state", "jurisdiction", "issuingorganization", "feedname"):
        value = str(row.get(key, "")).strip()
        if not value:
            continue
        low = value.lower()
        if value.upper() in _STATE_CODES.values():
            return value.upper()
        for name, code in _STATE_CODES.items():
            if name in low:
                return code
    return None


def registry_rows_to_feeds(rows: List[Dict[str, Any]]) -> List[FeedSpec]:
    """Convert raw WZDx registry rows into FeedSpecs (pure; no network)."""
    feeds: List[FeedSpec] = []
    seen_ids: set = set()
    for row in rows:
        url = (
            row.get("apiurl")
            or row.get("url")
            or row.get("feedurl")
            or row.get("datafeed_url")
        )
        if not url:
            continue
        name = (
            row.get("feedname")
            or row.get("datafeed_name")
            or row.get("issuingorganization")
            or "WZDx feed"
        )
        feed_id = "wzdx-" + _slug(str(name))[:48]
        # de-duplicate ids that collapse to the same slug
        base, n = feed_id, 1
        while feed_id in seen_ids:
            n += 1
            feed_id = f"{base}-{n}"
        seen_ids.add(feed_id)

        needs_key = str(row.get("apikeyrequired", "")).strip().lower() in ("yes", "true", "1")
        feeds.append(
            FeedSpec(
                id=feed_id,
                name=str(name),
                kind="wzdx",
                url=str(url),
                jurisdiction=_guess_jurisdiction(row),
                api_key_env=(f"FT_{feed_id.upper().replace('-', '_')}_KEY" if needs_key else None),
                api_key_param="apiKey",
                api_key_in="query",
                enabled=not needs_key,  # key-required feeds are off until configured
                notes=str(row.get("version") and f"WZDx v{row.get('version')}" or "") or None,
            )
        )
    return feeds


async def discover_wzdx_feeds(*, client: Any = None, limit: int = 5000) -> List[FeedSpec]:
    """Fetch the live WZDx registry and return discovered FeedSpecs."""
    from .client import _require_httpx  # local import to keep httpx optional

    httpx = _require_httpx()
    owns = client is None
    if owns:
        client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    try:
        resp = await client.get(WZDX_REGISTRY_URL, params={"$limit": str(limit)})
        resp.raise_for_status()
        rows = resp.json()
        return registry_rows_to_feeds(rows if isinstance(rows, list) else [])
    finally:
        if owns:
            await client.aclose()
