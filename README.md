# freetraffic

**A definitive, comprehensive layer for scraping free traffic data and making
origin→destination routing traffic-aware — on your own Valhalla server.**

You run [Valhalla](https://github.com/valhalla/valhalla). What it lacks out of
the box is *live conditions*. `freetraffic` scrapes every freely available
public traffic source it can and normalizes them into one canonical model, then
feeds them into Valhalla so routes avoid closures and reflect real conditions.

**Sources implemented today:**

- **State 511 events + speeds** — the shared Arcadis/IBI "Travel-IQ" platform
  (`GetEvents` / `GetTrafficSpeeds`), one adapter across many states.
- **Open511** event feeds and **WZDx** work-zone feeds (with live USDOT-registry discovery).
- **DOT travel-time feeds** (WSDOT `GetTravelTimes`) → measured speeds.
- **GTFS-Realtime transit vehicles as floating speed probes** — free, national.
- **National Weather Service** alerts → driving-relevant weather events.
- **TomTom Traffic Flow** (freemium, live-lookup only) for on-demand enrichment.

> OSRM has been retired in favour of Valhalla; the routing layer targets
> Valhalla. An OSRM `segment-speed-file` exporter is still included for anyone
> who wants it, but it isn't the focus.

---

## The honest landscape (read this first)

There is no single magic "live traffic" firehose you can scrape for free. There
are two *different kinds* of signal, and a comprehensive package treats them
differently:

| Signal | What it tells you | Free availability | Routing use |
|---|---|---|---|
| **Events** (incidents, closures, construction, work zones) | *This road is blocked / lane-reduced* | **Abundant.** Every US state runs a 511 system; **Open511** and **WZDx** are real standards, and most states share **one vendor API** (see below). The [USDOT WZDx Feed Registry](https://data.transportation.gov/Roadways-and-Bridges/Work-Zone-Data-Exchange-WZDx-Feed-Registry/69qe-yiui) enumerates work-zone feeds machine-readably. | Edge **avoidance** (`exclude_polygons`) + route annotation. |
| **Link speeds** (sensor loops, probe data) | *This road is currently moving at X km/h* | **Scarcer for free**, but the IBI511 platform's `GetTrafficSpeeds` exposes them for many states, plus Caltrans PeMS / WSDOT. NPMRDS is probe-based but not live. | True traffic-aware **travel times** (live edge speeds → `traffic.tar`). |

`freetraffic` models **both** (`TrafficEvent` + `LinkSpeed`) in one schema so
they fuse: events become restrictions, speeds become edge speeds.

### The breadth multiplier: one API, many states

A large set of state/provincial 511 systems run on **one vendor platform**
(Arcadis / IBI Group "Travel-IQ", hosted under `*.ibi511.com` and on
state-branded domains). They all expose the **same** REST API:

```
GET /api/GetEvents?key={key}&format=json          → incidents/closures/construction
GET /api/GetTrafficSpeeds?key={key}&format=json   → live link speeds
GET /api/GetConstruction | GetIncidents | GetCameras | GetMessageSigns ...
```

So a single adapter (`parsers/ibi511.py`) normalizes **NY, WI, GA, UT, FL, the
NH/ME/VT tri-state, and more** at once — this is very likely the "consistent
format" you had in mind. Each portal just needs its own free developer key.

---

## Architecture

```
        ┌────────────────────────── sources ──────────────────────────┐
  IBI511 platform   Open511   WZDx      WSDOT       GTFS-RT      NWS      TomTom
  events + speeds   SF Bay   (registry) traveltimes transit      alerts   flow
  NY·WI·GA·UT·...            discovery  (speeds)    probes       weather  (freemium)
        │             │        │          │           │            │         │
        ▼             ▼        ▼          ▼           ▼            ▼         ▼
   parsers/ibi511  open511   wzdx     parsers/wsdot  probes/    parsers/  probes/
   (events+speeds)                              gtfs_rt   nws      tomtom
        └─────────────────────────────┼──────────────────────────────────┘
                                 ▼
                    canonical model  (models.py)
                TrafficEvent  +  LinkSpeed   (km/h, tz-aware)
                                 ▼
                       store.py  (dedupe, GeoJSON, reload)
                                 ▼
                 routing/  TrafficAwareRouter ──► ValhallaClient ──► your Valhalla
                 (exclude closures + annotate route)        (Basic auth, ngrok)

           export/valhalla.py  ·  export/osrm.py   (edge-keyed traffic files; map-match first)
```

* **`catalog.py` + `registry/feeds.json`** — declarative feed specs. WZDx
  coverage is discovered live from the USDOT registry (`discovery.py`); the seed
  file carries the IBI511, Open511 and speed feeds.
* **`client.py`** — the only feed-fetching module (lazy `httpx`): per-feed auth,
  retries, pagination, per-feed error isolation.
* **`routing/`** — `ValhallaClient` (Basic auth + ngrok header, from env) and
  `TrafficAwareRouter`.
* **Core is pure standard library** — models, parsers, store, exporters, and the
  routing *logic* run with **zero third-party deps**. `httpx` is only needed to
  hit the network.

---

## Install & configure

```bash
pip install -e '.[fetch]'    # core + httpx (for fetching/routing)
pip install -e '.[dev]'      # + pytest

cp .env.example .env         # then fill in keys & Valhalla creds (.env is gitignored)
```

Requires Python 3.9+. **Credentials live only in `.env` / the environment —
never in the repo.** Relevant variables (see `.env.example`):

```
FT_VALHALLA_URL, FT_VALHALLA_USER, FT_VALHALLA_PASS, FT_VALHALLA_NGROK
FT_SF511_API_KEY, FT_IBI511_NY_KEY, FT_IBI511_WI_KEY, FT_IBI511_GA_KEY, ...
```

## Usage

### CLI

```bash
freetraffic sources list                     # the feed catalog
freetraffic sources discover-wzdx --save feeds.json

# Offline: normalize an already-downloaded payload (no network/httpx needed)
freetraffic parse --kind ibi511 --state NY raw_getevents.json -o ny.geojson
freetraffic parse --kind wzdx raw_wzdx.json
freetraffic parse --kind open511 raw_open511.json

# Live: fetch + normalize (needs the relevant *_KEY in the env)
freetraffic fetch --state NY -o ny.geojson
freetraffic fetch --discover -o everything.geojson      # all configured + WZDx registry

# Traffic-aware route on your Valhalla (reads FT_VALHALLA_* from env)
freetraffic route --from 42.68,-73.79 --to 40.71,-74.01 --snapshot ny.geojson
```

### Library — traffic-aware routing

```python
import asyncio
from freetraffic import (
    load_builtin_catalog, collect_feeds, TrafficSnapshot,
    ValhallaClient, TrafficAwareRouter,
)

async def main():
    # 1. scrape live conditions for the corridor's states
    cat = load_builtin_catalog()
    events, speeds, errors = await collect_feeds(cat.select(jurisdiction="NY"))
    snap = TrafficSnapshot(events=events, speeds=speeds, errors=errors).dedupe()

    # 2. route around closures + learn what's on the way
    router = TrafficAwareRouter(ValhallaClient.from_env())
    result = await router.route((-73.79, 42.68), (-74.01, 40.71), snap, costing="auto")

    print(result.length_km, "km;", result.time_s, "s")
    print(result.exclusions_applied, "closures excluded")
    for e in result.events_on_route:
        print(" •", e.event_type.value, e.severity.value, e.headline)

asyncio.run(main())
```

`TrafficAwareRouter` needs **no tile rebuild**: it converts active closures in
the corridor into Valhalla `exclude_polygons`, then annotates the returned route
with the events lying along it.

### Library — live speeds from transit probes (GTFS-Realtime)

Transit buses are free, live floating probes. Poll an agency's GTFS-RT
`VehiclePositions` feed repeatedly; the tracker diffs each vehicle's movement
into segment speeds, which you map-match to Valhalla edges and aggregate:

```python
from freetraffic.probes import GtfsRtProbeTracker, aggregate_by_edge
from freetraffic.probes.gtfs_rt import fetch_vehicle_positions, map_match_probes
from freetraffic.routing import ValhallaClient

tracker = GtfsRtProbeTracker()           # keep this across polls (it's stateful)
valhalla = ValhallaClient.from_env()

# each polling cycle (e.g. every 20-30s):
samples = await fetch_vehicle_positions("https://agency.example/gtfs-rt/vehicles")
probes = tracker.update(samples)                      # vehicle movement -> speeds
matched = await map_match_probes(probes, valhalla)    # attach Valhalla edge ids
link_speeds = aggregate_by_edge(matched, source_id="gtfs-rt", min_samples=2)
# -> LinkSpeed per edge; feed export/valhalla.py or your traffic.tar updater
```

Needs the `[gtfs]` extra (`pip install 'freetraffic[gtfs]'`). Dwell-at-stop,
GPS-jump and stale samples are filtered; the speed-derivation logic is pure and
unit-tested.

### Library — freemium enrichment (TomTom), ToS-respecting

```python
from freetraffic.probes.tomtom import TomTomFlowClient  # set FT_TOMTOM_API_KEY
flow = await TomTomFlowClient().flow_segment(lon=-122.33, lat=47.6)  # LinkSpeed
```

Live-lookup only — TomTom's terms forbid caching/redistribution, so use the
result for the current request and discard it (don't crawl the network with it).

### Feeding live *speeds* into the engine (deeper integration)

Valhalla keys traffic by its own edge ids, so a `LinkSpeed`/closure geometry
must be map-matched first (`routing.ValhallaClient.trace_attributes` /
`match.py`). The exporters then emit edge-keyed files:

```python
from freetraffic.export import valhalla, osrm
valhalla.write_predicted_csv([(edge_id, freeflow_kph, constrained_kph)], "edges.csv")
osrm.write_segment_speed_file([(from_node, to_node, speed_kph)], "live.csv")  # legacy
```

---

## Predicting travel times (the spine)

Every source collapses to **one intermediate: a predicted speed per routing-graph
edge.** Travel time is then `Σ length / speed` over the route's edges
(`freetraffic.predict.fusion`). Sources combine by an explicit, tunable
precedence (`FusionConfig`):

| Precedence | Sources | Effect on the edge |
|---|---|---|
| 1. Hard constraint | full closures (511/Open511/WZDx) | edge impassable |
| 2. Measured speed *(wins — already reflects incidents/weather)* | GTFS-RT probes, WSDOT travel-times, IBI511 speeds, TomTom flow | edge speed = measured (best by confidence × recency; stale dropped) |
| 3. Modeled fallback *(only where unmeasured)* | incidents/lane closures (severity & lane-fraction), WZDx reduced limit, NWS weather | base speed × penalty, capped by posted limit |

```python
from freetraffic.predict import predict_eta, route_edges_from_trace
edges = route_edges_from_trace(trace)          # Valhalla /trace_attributes -> edges
eta = predict_eta(edges, snapshot)
eta.base_time_s, eta.predicted_time_s, eta.delay_s   # free-flow vs traffic-aware
for e in eta.edges:
    e.edge_id, e.predicted_speed_kph, e.source, e.factors   # full provenance per edge
```

Example (3-edge route; buses crawling on edge 2, snow over edge 3):

```
edge 1: base 105 -> 105.0 kph  [base]
edge 2: base  80 ->  22.0 kph  [measured:gtfs-rt]
edge 3: base  50 ->  35.0 kph  [modeled] {weather: 0.7}
Base ETA 208 s  ->  Predicted ETA 417 s  (+100%)
```

### Two ways to apply it to Valhalla

* **Mode A — post-hoc ETA correction (any Valhalla, no rebuild):**
  `TrafficAwareRouter.route_with_eta(...)` routes (avoiding closures), pulls the
  chosen route's edges via `/trace_attributes`, and re-times them with fused
  speeds. Implemented today.
* **Mode B — native live traffic (`traffic.tar`):** write the same per-edge
  speeds into Valhalla's memory-mapped traffic extract so it uses them for both
  routing *and* timing. Needs build-side access to your tiles (roadmap).

Feeds whose speeds carry native segment ids (IBI511/WSDOT/TomTom) are mapped to
Valhalla edge ids first with `routing.service.map_match_link_speeds`; GTFS-RT
probe speeds are already edge-keyed.

## How traffic gets into Valhalla (reference)

* **Per-request avoidance (what `TrafficAwareRouter` does today):**
  `exclude_polygons` / `exclude_locations` in the `/route` body — instant, no
  rebuild.
* **Predicted/historical:** `valhalla_add_predicted_traffic` from CSV
  (`edge_id,freeflow_speed,constrained_speed,historical_speeds`).
* **Live speeds:** memory-mapped `traffic.tar` of binary `TrafficSpeed` records,
  updated at runtime (needs server-side build access — roadmap).

> The `historical_speeds` codec in `export/valhalla.py` is a **reference**
> DCT implementation that round-trips exactly; it is *not* byte-compatible with a
> specific Valhalla build's quantization. Use freeflow/constrained (directly
> usable) or your build's encoder for that column.

---

## Roadmap

Done: IBI511 platform (events+speeds), Open511, WZDx + registry discovery,
WSDOT travel-times, GTFS-RT transit probes, NWS weather, TomTom freemium flow,
and per-request traffic-aware routing on Valhalla. Next:

1. **More coverage** — finish the IBI511 state list; bespoke adapters for OHGO
   (OH), NCDOT, Caltrans LCS/PeMS, MassDOT; CBP border-wait-times; city/county
   open-data portals (Socrata/ArcGIS); auto-ingest WZDx-registry feeds in `fetch`.
2. **Live-speed → `traffic.tar`** — write Valhalla's binary live-traffic extract
   from aggregated `LinkSpeed` (probes + travel-times) for true traffic-aware times.
3. **Fusion policy** — combine measured speeds with event severity / lane
   fractions / weather into principled per-edge penalties.
4. **Scheduler / service** — periodic refresh loop + a long-running routing service.
5. **Camera CV + NPMRDS** — traffic-camera vehicle detection; NPMRDS when access lands.

---

## Development

```bash
pip install -e '.[dev]'    # includes httpx + gtfs-realtime-bindings
pytest -q          # 48 tests, fully offline (fixtures under tests/fixtures/)
```

The Valhalla client is tested against a mocked transport, so no live server or
credentials are needed to run the suite.

## License

MIT.
