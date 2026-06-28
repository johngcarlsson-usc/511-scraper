"""The source catalog: where the feeds live and how to read them.

A :class:`FeedSpec` is a declarative description of one feed -- its URL, the
parser ``kind`` that understands it, the jurisdiction it covers, and how to
authenticate. The catalog is seeded from ``registry/feeds.json`` (a small,
hand-verified set) and can be extended at runtime from the live USDOT WZDx feed
registry (see :mod:`freetraffic.discovery`).

Keeping feeds *declarative* is the whole trick to "every state": adding a new
jurisdiction is a data change (one JSON object), not a code change, as long as
it speaks Open511 or WZDx. Genuinely bespoke feeds get a custom parser
registered in :mod:`freetraffic.parsers`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Dict, List, Optional

# How a feed's data is shaped / what it yields.
EVENT_KINDS = {"open511", "wzdx"}
SPEED_KINDS = {"speed"}  # sensor / probe link-speed feeds (per-source parsers)


@dataclass
class FeedSpec:
    """Declarative description of a single data feed."""

    id: str
    name: str
    kind: str  # "open511" | "wzdx" | "speed" | <custom>
    url: str
    jurisdiction: Optional[str] = None  # US state / province code
    country: str = "US"
    # Name of an environment variable holding the API key, if the feed needs one.
    api_key_env: Optional[str] = None
    # Where the key goes: "query" (?api_key=...) or "header".
    api_key_param: str = "api_key"
    api_key_in: str = "query"
    # Extra static query parameters to send.
    params: Dict[str, str] = field(default_factory=dict)
    # Whether the feed pages (Open511 ``pagination.next_url``).
    paginated: bool = False
    enabled: bool = True
    notes: Optional[str] = None

    @property
    def produces_events(self) -> bool:
        return self.kind in EVENT_KINDS

    @property
    def produces_speeds(self) -> bool:
        return self.kind in SPEED_KINDS

    @property
    def api_key(self) -> Optional[str]:
        return os.environ.get(self.api_key_env) if self.api_key_env else None

    @property
    def needs_key(self) -> bool:
        return self.api_key_env is not None

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_dict(cls, obj: Dict[str, Any]) -> "FeedSpec":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in obj.items() if k in known})

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, {}, [])}


class Catalog:
    """A collection of :class:`FeedSpec`, queryable by state / kind."""

    def __init__(self, feeds: Optional[List[FeedSpec]] = None) -> None:
        self._feeds: Dict[str, FeedSpec] = {}
        for feed in feeds or []:
            self.add(feed)

    def add(self, feed: FeedSpec) -> None:
        self._feeds[feed.id] = feed

    def extend(self, feeds: List[FeedSpec]) -> None:
        for feed in feeds:
            self.add(feed)

    def __len__(self) -> int:
        return len(self._feeds)

    def __iter__(self):
        return iter(self._feeds.values())

    def get(self, feed_id: str) -> Optional[FeedSpec]:
        return self._feeds.get(feed_id)

    def select(
        self,
        *,
        jurisdiction: Optional[str] = None,
        kind: Optional[str] = None,
        enabled_only: bool = True,
        events_only: bool = False,
        speeds_only: bool = False,
    ) -> List[FeedSpec]:
        result = []
        for feed in self._feeds.values():
            if enabled_only and not feed.enabled:
                continue
            if jurisdiction and (feed.jurisdiction or "").upper() != jurisdiction.upper():
                continue
            if kind and feed.kind != kind:
                continue
            if events_only and not feed.produces_events:
                continue
            if speeds_only and not feed.produces_speeds:
                continue
            result.append(feed)
        return result

    def jurisdictions(self) -> List[str]:
        return sorted({f.jurisdiction for f in self._feeds.values() if f.jurisdiction})


def load_builtin_catalog() -> Catalog:
    """Load the hand-verified seed catalog shipped with the package."""
    text = resources.files("freetraffic.registry").joinpath("feeds.json").read_text(
        encoding="utf-8"
    )
    data = json.loads(text)
    feeds = [FeedSpec.from_dict(obj) for obj in data.get("feeds", [])]
    return Catalog(feeds)
