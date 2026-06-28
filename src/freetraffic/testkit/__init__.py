"""Testing aids: record real HTTP sessions and replay them deterministically.

Every networked client in this package accepts an injectable ``httpx`` client,
so you can wrap one transport to **record** a real session (your Valhalla + live
feeds) to a JSON cassette, then later run the exact same code against a
**replay** transport with no network. That turns one real run into a permanent,
deterministic end-to-end test -- the bridge between "works on my machine with
the network open" and "verified in CI".

Usage::

    import httpx
    from freetraffic.testkit import RecordingTransport, ReplayTransport, Cassette
    from freetraffic.routing import ValhallaClient, TrafficAwareRouter

    # RECORD (where the network is open):
    cas = Cassette()
    real = httpx.AsyncHTTPTransport()
    async with httpx.AsyncClient(transport=RecordingTransport(real, cas)) as ac:
        router = TrafficAwareRouter(ValhallaClient.from_env(client=ac))
        await router.route_with_eta(origin, dest, snapshot)
    cas.save("scenario.json")

    # REPLAY (offline / CI):
    cas = Cassette.load("scenario.json")
    async with httpx.AsyncClient(transport=ReplayTransport(cas)) as ac:
        ...   # same code, no network
"""

from __future__ import annotations

from .cassette import Cassette, RecordingTransport, ReplayTransport

__all__ = ["Cassette", "RecordingTransport", "ReplayTransport"]
