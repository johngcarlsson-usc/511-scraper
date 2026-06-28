"""Async fetching of feeds.

This is the only module that touches the network, and the only one that needs
``httpx`` (imported lazily so the rest of the library installs/runs with zero
third-party dependencies). It handles per-feed auth, retries with exponential
backoff, and Open511 pagination.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from .catalog import FeedSpec
from .models import LinkSpeed, TrafficEvent
from .parsers import EVENT_PARSERS

DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 3
USER_AGENT = "freetraffic/0.1 (+https://github.com/johngcarlsson-usc/511-scraper)"


class FetchError(RuntimeError):
    def __init__(self, feed_id: str, message: str) -> None:
        super().__init__(f"[{feed_id}] {message}")
        self.feed_id = feed_id


def _require_httpx():
    try:
        import httpx  # noqa: WPS433 (lazy import is intentional)
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Network fetching requires httpx. Install with: pip install 'freetraffic[fetch]'"
        ) from exc
    return httpx


def _build_request(feed: FeedSpec) -> Tuple[str, Dict[str, str], Dict[str, str]]:
    """Return (url, params, headers) with auth applied."""
    params: Dict[str, str] = dict(feed.params)
    headers: Dict[str, str] = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if feed.needs_key:
        key = feed.api_key
        if not key:
            raise FetchError(
                feed.id,
                f"requires an API key; set the {feed.api_key_env} environment variable",
            )
        if feed.api_key_in == "header":
            headers[feed.api_key_param] = key
        else:
            params[feed.api_key_param] = key
    return feed.url, params, headers


async def fetch_raw(feed: FeedSpec, *, client: Any = None) -> List[dict]:
    """Fetch a feed's raw JSON page(s). Returns a list of page payloads."""
    httpx = _require_httpx()
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True)
    try:
        url, params, headers = _build_request(feed)
        pages: List[dict] = []
        next_url: Optional[str] = url
        next_params: Optional[Dict[str, str]] = params
        seen = 0
        while next_url:
            payload = await _get_json(client, feed, next_url, next_params, headers)
            pages.append(payload)
            seen += 1
            if not feed.paginated or seen >= 50:
                break
            pagination = payload.get("pagination") if isinstance(payload, dict) else None
            next_url = pagination.get("next_url") if isinstance(pagination, dict) else None
            next_params = None  # next_url already carries its query string
        return pages
    finally:
        if owns_client:
            await client.aclose()


async def _get_json(
    client: Any,
    feed: FeedSpec,
    url: str,
    params: Optional[Dict[str, str]],
    headers: Dict[str, str],
) -> dict:
    httpx = _require_httpx()
    last_exc: Optional[Exception] = None
    for attempt in range(DEFAULT_RETRIES):
        try:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:  # ValueError: bad JSON
            last_exc = exc
            if attempt < DEFAULT_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
    raise FetchError(feed.id, f"fetch failed after {DEFAULT_RETRIES} attempts: {last_exc}")


def parse_pages(feed: FeedSpec, pages: List[dict]) -> List[TrafficEvent]:
    """Parse already-fetched pages into canonical events."""
    parser = EVENT_PARSERS.get(feed.kind)
    if parser is None:
        return []
    events: List[TrafficEvent] = []
    for page in pages:
        events.extend(parser(page, source_id=feed.id, jurisdiction=feed.jurisdiction))
    return events


async def collect_feed(feed: FeedSpec, *, client: Any = None) -> List[TrafficEvent]:
    """Fetch + parse a single event feed."""
    pages = await fetch_raw(feed, client=client)
    return parse_pages(feed, pages)


async def collect_feeds(
    feeds: List[FeedSpec], *, concurrency: int = 8
) -> Tuple[List[TrafficEvent], Dict[str, str]]:
    """Fetch + parse many feeds concurrently.

    Returns ``(events, errors)`` where ``errors`` maps feed id -> message for
    feeds that failed, so one broken jurisdiction never sinks the whole run.
    """
    httpx = _require_httpx()
    sem = asyncio.Semaphore(concurrency)
    events: List[TrafficEvent] = []
    errors: Dict[str, str] = {}

    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:

        async def _one(feed: FeedSpec) -> None:
            async with sem:
                try:
                    events.extend(await collect_feed(feed, client=client))
                except Exception as exc:  # noqa: BLE001 - isolate per-feed failures
                    errors[feed.id] = str(exc)

        await asyncio.gather(*(_one(f) for f in feeds))
    return events, errors
