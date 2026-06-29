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
import os
import sys
from typing import List, Optional

from .catalog import FeedSpec, load_builtin_catalog
from .parsers import EVENT_PARSERS, SPEED_PARSERS
from .store import TrafficSnapshot


def _load_dotenv(path: Optional[str] = None) -> None:
    """Load KEY=VALUE lines from ./.env into the environment (no dependency).

    Existing environment variables win; set FT_ENV_FILE to point elsewhere.
    """
    path = path or os.environ.get("FT_ENV_FILE", ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main(argv: Optional[List[str]] = None) -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(prog="freetraffic", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    # check ---------------------------------------------------------------
    p_check = sub.add_parser("check", help="verify Valhalla connectivity/auth (FT_VALHALLA_*)")

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
    p_parse.add_argument(
        "--kind", required=True,
        choices=sorted(set(EVENT_PARSERS) | set(SPEED_PARSERS)),
    )
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

    # route ---------------------------------------------------------------
    p_route = sub.add_parser("route", help="traffic-aware route via Valhalla (network)")
    p_route.add_argument("--from", dest="origin", required=True, metavar="LAT,LON")
    p_route.add_argument("--to", dest="dest", required=True, metavar="LAT,LON")
    p_route.add_argument("--costing", default="auto")
    p_route.add_argument("--snapshot", help="GeoJSON snapshot to apply (from `fetch`/`parse`)")
    p_route.add_argument("--no-avoid", action="store_true",
                         help="don't build exclude_polygons from closures")
    p_route.add_argument("--match-speeds", action="store_true",
                         help="map-match snapshot speeds to Valhalla edges before ETA")

    # traffic-update (Mode B) -------------------------------------------------
    p_tt = sub.add_parser("traffic-update",
                          help="write edge speeds into a Valhalla traffic.tar (Mode B)")
    p_tt.add_argument("--tar", required=True, help="path to an existing traffic.tar")
    p_tt.add_argument("--csv", help="CSV of 'edge_id,speed_kph' rows")
    p_tt.add_argument("--snapshot", help="GeoJSON snapshot; speeds must be edge-keyed")

    # serve (roadmap #1: continuous GTFS-RT -> traffic loop) ------------------
    p_serve = sub.add_parser(
        "serve",
        help="continuous GTFS-RT -> Valhalla traffic service loop (network)",
    )
    p_serve.add_argument(
        "--feed", action="append", default=[], metavar="SOURCE_ID=URL",
        help="GTFS-RT VehiclePositions feed; 'id=url' or bare url. Repeatable. "
             "Defaults to MBTA (Boston) if none given.",
    )
    p_serve.add_argument("--jurisdiction", help="default jurisdiction code for feeds, e.g. MA")
    p_serve.add_argument("--interval", type=float, default=60.0, help="poll interval seconds")
    p_serve.add_argument("--ttl", type=float, default=600.0,
                         help="drop edge speeds older than this many seconds")
    p_serve.add_argument("--tar", help="Valhalla traffic.tar to write (Mode B); "
                                       "omit for in-memory only (Mode A)")
    p_serve.add_argument("--emit-csv", dest="emit_csv",
                         help="write the current edge_id,speed_kph snapshot here each tick")
    p_serve.add_argument("--max-ticks", dest="max_ticks", type=int, default=None,
                         help="stop after N ticks (default: run until Ctrl-C)")
    p_serve.add_argument("--concurrency", type=int, default=8,
                         help="map-match concurrency per tick")
    p_serve.add_argument("--min-samples", dest="min_samples", type=int, default=1,
                         help="min probes per edge before trusting its speed")

    args = parser.parse_args(argv)

    if args.command == "check":
        return _cmd_check(args)
    if args.command == "sources":
        if args.sources_command == "list":
            return _cmd_sources_list(args)
        if args.sources_command == "discover-wzdx":
            return _cmd_discover(args)
    if args.command == "parse":
        return _cmd_parse(args)
    if args.command == "fetch":
        return _cmd_fetch(args)
    if args.command == "route":
        return _cmd_route(args)
    if args.command == "traffic-update":
        return _cmd_traffic_update(args)
    if args.command == "serve":
        return _cmd_serve(args)
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
    if args.kind in EVENT_PARSERS:
        events = EVENT_PARSERS[args.kind](
            payload, source_id=args.source_id, jurisdiction=args.state
        )
        snap = TrafficSnapshot(events=events)
    else:
        speeds = SPEED_PARSERS[args.kind](
            payload, source_id=args.source_id, jurisdiction=args.state
        )
        snap = TrafficSnapshot(speeds=speeds)
    if not args.no_dedupe:
        snap.dedupe()
    _emit(snap, args.out)
    print(
        f"parsed {len(snap.events)} event(s) + {len(snap.speeds)} speed(s)",
        file=sys.stderr,
    )
    return 0


def _cmd_fetch(args) -> int:
    from . import client, discovery

    cat = load_builtin_catalog()
    feeds = cat.select(jurisdiction=args.state, kind=args.kind)
    if args.discover:
        discovered = asyncio.run(discovery.discover_wzdx_feeds())
        for f in discovered:
            if (not args.state or (f.jurisdiction or "").upper() == args.state.upper()) and f.enabled:
                feeds.append(f)
    if not feeds:
        print("no feeds match the filters", file=sys.stderr)
        return 1

    events, speeds, errors = asyncio.run(
        client.collect_feeds(feeds, concurrency=args.concurrency)
    )
    snap = TrafficSnapshot(events=events, speeds=speeds, errors=errors)
    if not args.no_dedupe:
        snap.dedupe()
    _emit(snap, args.out)
    print(
        f"fetched {len(snap.events)} event(s) + {len(snap.speeds)} speed(s) "
        f"from {len(feeds)} feed(s); {len(errors)} error(s)",
        file=sys.stderr,
    )
    return 0


def _cmd_route(args) -> int:
    import asyncio

    from .routing import TrafficAwareRouter, ValhallaClient

    origin = _parse_latlon(args.origin)
    dest = _parse_latlon(args.dest)
    snapshot = None
    if args.snapshot:
        with open(args.snapshot, "r", encoding="utf-8") as fh:
            snapshot = TrafficSnapshot.from_geojson(json.load(fh))

    client = ValhallaClient.from_env()
    if not client.base_url:
        print("set FT_VALHALLA_URL (and FT_VALHALLA_USER/PASS) in the environment",
              file=sys.stderr)
        return 1
    if args.match_speeds and snapshot and snapshot.speeds:
        from .routing.service import map_match_link_speeds

        snapshot.speeds = asyncio.run(map_match_link_speeds(snapshot.speeds, client))
        print(f"map-matched {len(snapshot.speeds)} speed(s) to edges", file=sys.stderr)

    router = TrafficAwareRouter(client)
    try:
        result = asyncio.run(
            router.route_with_eta(origin, dest, snapshot, costing=args.costing,
                                  avoid_closures=not args.no_avoid)
        )
    except Exception as exc:  # noqa: BLE001
        print(f"route failed: {exc}", file=sys.stderr)
        print("  (check the points are within your Valhalla's coverage, and "
              "`freetraffic check` passes)", file=sys.stderr)
        return 1
    out = {
        "summary": result.summary,
        "length_km": result.length_km,
        "valhalla_time_s": result.time_s,
        "predicted_eta": result.eta.to_dict() if result.eta else None,
        "exclusions_applied": result.exclusions_applied,
        "events_on_route": [
            {
                "id": e.id,
                "type": e.event_type.value,
                "severity": e.severity.value,
                "headline": e.headline,
            }
            for e in result.events_on_route
        ],
    }
    print(json.dumps(out, indent=2))
    return 0


def _cmd_check(args) -> int:
    from .routing import ValhallaClient

    client = ValhallaClient.from_env()
    if not client.base_url:
        print("FT_VALHALLA_URL is not set (put it in .env or the environment)",
              file=sys.stderr)
        return 1
    print(f"checking {client.base_url} ...", file=sys.stderr)
    try:
        status = asyncio.run(client.status())
    except Exception as exc:  # noqa: BLE001
        print(f"Valhalla check FAILED: {exc}", file=sys.stderr)
        return 1
    print("Valhalla OK")
    print(json.dumps(status, indent=2))
    return 0


def _cmd_traffic_update(args) -> int:
    from .export.valhalla_traffic import TrafficTarUpdater

    edge_speeds = {}
    if args.csv:
        with open(args.csv, "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) < 2:
                    continue
                try:
                    edge_speeds[int(parts[0])] = float(parts[1])
                except ValueError:
                    continue  # header or bad row
    snapshot = None
    if args.snapshot:
        with open(args.snapshot, "r", encoding="utf-8") as fh:
            snapshot = TrafficSnapshot.from_geojson(json.load(fh))

    with TrafficTarUpdater(args.tar) as up:
        written = up.set_speeds(edge_speeds) if edge_speeds else 0
        if snapshot:
            written += up.apply_link_speeds(snapshot.speeds)
        print(f"wrote {written} edge speed(s) into {args.tar} "
              f"({up.tile_count} tiles); skipped {getattr(up, 'skipped', 0)}")
    return 0


def _parse_feed_arg(spec: str, default_jurisdiction):
    """Parse a --feed value of the form 'source_id=url' or a bare url."""
    from .service import AgencyFeed

    if "=" in spec:
        source_id, url = spec.split("=", 1)
        source_id = source_id.strip()
    else:
        url = spec
        source_id = "gtfsrt"
    return AgencyFeed(
        source_id=source_id, url=url.strip(), jurisdiction=default_jurisdiction
    )


def _cmd_serve(args) -> int:
    import logging

    from .routing import ValhallaClient
    from .service import AgencyFeed, EdgeSpeedStore, run_service_loop
    from .service.loop import MBTA_VEHICLE_POSITIONS

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    # httpx logs every request at INFO; quiet it so per-tick stats stand out.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    client = ValhallaClient.from_env()
    if not client.base_url:
        print("FT_VALHALLA_URL is not set (put it in .env or the environment)",
              file=sys.stderr)
        return 1

    if args.feed:
        agencies = [_parse_feed_arg(f, args.jurisdiction) for f in args.feed]
    else:
        agencies = [AgencyFeed(
            source_id="mbta-rt", url=MBTA_VEHICLE_POSITIONS,
            jurisdiction=args.jurisdiction or "MA",
        )]

    tar_updater = None
    if args.tar:
        from .export.valhalla_traffic import TrafficTarUpdater

        tar_updater = TrafficTarUpdater(args.tar)

    store = EdgeSpeedStore(ttl_s=args.ttl)

    def _on_tick(_stats):
        if args.emit_csv:
            with open(args.emit_csv, "w", encoding="utf-8") as fh:
                fh.write("edge_id,speed_kph\n")
                for edge_id, kph in sorted(store.speeds().items()):
                    fh.write(f"{edge_id},{kph:.2f}\n")

    try:
        asyncio.run(run_service_loop(
            agencies, client,
            interval_s=args.interval,
            store=store,
            tar_updater=tar_updater,
            max_ticks=args.max_ticks,
            concurrency=args.concurrency,
            min_samples=args.min_samples,
            on_tick=_on_tick,
        ))
    except KeyboardInterrupt:
        print("\nstopped (Ctrl-C).", file=sys.stderr)
    finally:
        if tar_updater is not None:
            tar_updater.close()
    print(f"final: {len(store)} edge(s) in store")
    return 0


def _parse_latlon(text: str):
    lat_str, lon_str = text.split(",")
    return (float(lon_str), float(lat_str))  # (lon, lat) internally


def _emit(snap: TrafficSnapshot, out: Optional[str]) -> None:
    text = snap.dumps(indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
