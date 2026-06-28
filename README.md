# freetraffic

**A definitive, comprehensive layer for scraping free traffic data and making
origin→destination routing traffic-aware — on your own Valhalla server.**

You run [Valhalla](https://github.com/valhalla/valhalla). What it lacks out of
the box is *live conditions*. `freetraffic` scrapes every freely available
public traffic source it can — state **511** systems (the shared Arcadis/IBI
"Travel-IQ" platform), **Open511** event feeds, **WZDx** work-zone feeds, and
(on the roadmap) live-speed sensor/probe feeds — normalizes them into one
canonical model, and feeds them into Valhalla so routes avoid closures and
surface the incidents along the way.

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
        ┌──────────────────── sources ────────────────────┐
  IBI511 platform (GetEvents/GetTrafficSpeeds)   Open511   WZDx   speed feeds
  NY·WI·GA·UT·FL·NH/ME/VT·...                    SF Bay   (registry)  (roadmap)
        │                       │                 │         │          │
        ▼                       ▼                 ▼         ▼          ▼
   parsers/ibi511      parsers/ibi511      parsers/open511 parsers/wzdx  (bespoke)
   (events)            (speeds)
        └───────────────────────┼──────────────────────────┘
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

Priority is **breadth of sources**, then deeper Valhalla integration:

1. **More platform coverage** — finish the IBI511 state list, add bespoke
   adapters for big non-platform DOTs (WSDOT, OHGO, NCDOT, Caltrans LCS/PeMS,
   MassDOT), and wire WZDx-registry feeds into `fetch` automatically.
2. **Live-speed → `traffic.tar`** — map-match `LinkSpeed` records to edge ids and
   write Valhalla's binary live-traffic extract for true traffic-aware times.
3. **Fusion policy** — translate event severity / lane fractions / reduced speed
   limits into principled edge-speed penalties when no measured speed exists.
4. **Scheduler / service** — periodic refresh + a long-running routing service.
5. **NPMRDS ingest** — when access lands (historical/predicted, not live).

---

## Development

```bash
pip install -e '.[dev]'
pytest -q          # 31 tests, fully offline (fixtures under tests/fixtures/)
```

The Valhalla client is tested against a mocked transport, so no live server or
credentials are needed to run the suite.

## License

MIT.
