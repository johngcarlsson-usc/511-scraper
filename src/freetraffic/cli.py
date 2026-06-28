"""Command-line interface for freetraffic.

    freetraffic sources list [--state CA] [--kind wzdx]
    freetraffic sources discover-wzdx [--limit N] [--save feeds.json]
    freetraffic parse --kind open511 raw.json [--source-id s] [--state CA] [-o out.geojson]
    freetraffic fetch [--state CA] [--kind wzdx] [--discover] [-o snapshot.geojson]

``parse`` works fully offline (no network, no httpx) -- handy for testing and
for processing already-downloaded payloads. ``fetch`` / ``discover-wzdx`` need
the [fetch] extra (httpx).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import List, Optional

from .catalog import FeedSpec, load_builtin_catalog
from .parsers import EVENT_PARSERS
from .store import TrafficSnapshot


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="freetraffic", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    # sources -------------------------------------------------------------
    p_sources = sub.add_parser("sources", help="inspect the feed catalog")
    src_sub = p_sources.add_subparsers(dest="sources_command", required=True)

    p_list = src_sub.add_parser("list", help="list catalog feeds")
    p_list.add_argument("--state", help="filter by jurisdiction code, e.g. CA")
    p_list.add_argument("--kind", help="filter by kind: open511 | wzdx | speed")
    p_list.add_argument("--json", action="store_true", help="emit JSON")

    p_disc = src_sub.add_parser("discover-wzdx", help="pull the live WZDx registry")
    p_disc.add_argument("--limit", type=int, default=5000)
    p_disc.add_argument("--save", help="write discovered feeds to a JSON file")
    p_disc.add_argument("--json", action="store_true", help="emit JSON to stdout")

    # parse ---------------------------------------------------------------
    p_parse = sub.add_parser("parse", help="parse a local raw feed file (offline)")
    p_parse.add_argument("file", help="raw feed JSON file")
    p_parse.add_argument("--kind", required=True, choices=sorted(EVENT_PARSERS))
    p_parse.add_argument("--source-id", default="local")
    p_parse.add_argument("--state", default=None)
    p_parse.add_argument("-o", "--out", help="output GeoJSON file (default: stdout)")
    p_parse.add_argument("--no-dedupe", action="store_true")

    # fetch ---------------------------------------------------------------
    p_fetch = sub.add_parser("fetch", help="fetch + normalize feeds (network)")
    p_fetch.add_argument("--state", help="filter by jurisdiction code")
    p_fetch.add_argument("--kind", help="filter by kind")
    p_fetch.add_argument("--discover", action="store_true",
                         help="also include WZDx feeds from the live registry")
    p_fetch.add_argument("--concurrency", type=int, default=8)
    p_fetch.add_argument("-o", "--out", help="output GeoJSON file (default: stdout)")
    p_fetch.add_argument("--no-dedupe", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "sources":
        if args.sources_command == "list":
            return _cmd_sources_list(args)
        if args.sources_command == "discover-wzdx":
            return _cmd_discover(args)
    if args.command == "parse":
        return _cmd_parse(args)
    if args.command == "fetch":
        return _cmd_fetch(args)
    parser.print_help()
    return 1


def _cmd_sources_list(args) -> int:
    cat = load_builtin_catalog()
    feeds = cat.select(jurisdiction=args.state, kind=args.kind, enabled_only=False)
    if args.json:
        print(json.dumps([f.to_dict() for f in feeds], indent=2))
        return 0
    if not feeds:
        print("no feeds match", file=sys.stderr)
        return 0
    width = max(len(f.id) for f in feeds)
    for f in feeds:
        flags = []
        if not f.enabled:
            flags.append("disabled")
        if f.needs_key:
            flags.append("key-set" if f.has_key else "needs-key")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"{f.id:<{width}}  {f.kind:<8} {f.jurisdiction or '--':<3}  {f.name}{suffix}")
    print(f"\n{len(feeds)} feed(s). States: {', '.join(cat.jurisdictions()) or 'none'}")
    return 0


def _cmd_discover(args) -> int:
    from . import discovery

    feeds = asyncio.run(discovery.discover_wzdx_feeds(limit=args.limit))
    by_state: dict = {}
    for f in feeds:
        by_state.setdefault(f.jurisdiction or "??", 0)
        by_state[f.jurisdiction or "??"] += 1
    if args.json:
        print(json.dumps([f.to_dict() for f in feeds], indent=2))
    else:
        print(f"discovered {len(feeds)} WZDx feed(s) across {len(by_state)} jurisdiction(s)")
        for state in sorted(by_state):
            print(f"  {state:<4} {by_state[state]}")
    if args.save:
        with open(args.save, "w", encoding="utf-8") as fh:
            json.dump({"feeds": [f.to_dict() for f in feeds]}, fh, indent=2)
        print(f"saved -> {args.save}", file=sys.stderr)
    return 0


def _cmd_parse(args) -> int:
    with open(args.file, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    parser_fn = EVENT_PARSERS[args.kind]
    events = parser_fn(payload, source_id=args.source_id, jurisdiction=args.state)
    snap = TrafficSnapshot(events=events)
    if not args.no_dedupe:
        snap.dedupe()
    _emit(snap, args.out)
    print(f"parsed {len(snap.events)} event(s)", file=sys.stderr)
    return 0


def _cmd_fetch(args) -> int:
    from . import client, discovery

    cat = load_builtin_catalog()
    feeds = cat.select(jurisdiction=args.state, kind=args.kind, events_only=True)
    if args.discover:
        discovered = asyncio.run(discovery.discover_wzdx_feeds())
        for f in discovered:
            if (not args.state or (f.jurisdiction or "").upper() == args.state.upper()) and f.enabled:
                feeds.append(f)
    if not feeds:
        print("no event feeds match the filters", file=sys.stderr)
        return 1

    events, errors = asyncio.run(client.collect_feeds(feeds, concurrency=args.concurrency))
    snap = TrafficSnapshot(events=events, errors=errors)
    if not args.no_dedupe:
        snap.dedupe()
    _emit(snap, args.out)
    print(
        f"fetched {len(snap.events)} event(s) from {len(feeds)} feed(s); "
        f"{len(errors)} error(s)",
        file=sys.stderr,
    )
    return 0


def _emit(snap: TrafficSnapshot, out: Optional[str]) -> None:
    text = snap.dumps(indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
