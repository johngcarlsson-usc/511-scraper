# freetraffic

**A definitive, comprehensive layer for scraping free traffic data and making
origin→destination routing traffic-aware.**

You already run [Valhalla](https://github.com/valhalla/valhalla) and
[OSRM](https://github.com/Project-OSRM/osrm-backend). What they lack out of the
box is *live conditions*. `freetraffic` scrapes every freely available public
traffic source it can — state **511** / **Open511** event feeds, **WZDx**
work-zone feeds, and (on the roadmap) live-speed sensor/probe feeds —
normalizes them into one canonical model, and exports them into the live-traffic
inputs Valhalla and OSRM actually consume.

---

## The honest landscape (read this first)

There is no single magic "live traffic" firehose you can scrape for free. There
are two *different kinds* of signal, and a comprehensive package has to treat
them differently:

| Signal | What it tells you | Free availability | Routing use |
|---|---|---|---|
| **Events** (incidents, closures, construction, work zones) | *This road is blocked / lane-reduced* | **Abundant.** Every US state runs a 511 system; **Open511** and **WZDx** are real cross-jurisdiction standards. The [USDOT WZDx Feed Registry](https://data.transportation.gov/Roadways-and-Bridges/Work-Zone-Data-Exchange-WZDx-Feed-Registry/69qe-yiui) enumerates feeds machine-readably. | Edge **avoidance / penalties** — block or slow affected edges. |
| **Link speeds** (sensor loops, probe data) | *This road is currently moving at X km/h* | **Scarce for free.** A handful of states publish detector speeds (Caltrans PeMS, WSDOT). NPMRDS is probe-based but not live. Commercial (INRIX/HERE/TomTom) fills the gap but isn't free. | True traffic-aware **travel times** — set live edge speeds. |

Most "511 scrapers" only ever get you the first row. `freetraffic` models both
and keeps them in one schema so you can fuse them: events become restrictions,
speeds become edge speeds, and both flow into the same Valhalla/OSRM update.

> **Caveat on "the format is consistent":** the *standards* (Open511, WZDx) are
> consistent; real deployments drift — different auth, field nesting, WZDx
> versions, and quirks. The parsers here are deliberately tolerant, and adding a
> jurisdiction that speaks Open511/WZDx is a **data change**, not code.

---

## Architecture

```
            ┌─────────────── sources ───────────────┐
 511 / Open511 feeds   WZDx feeds   speed feeds (roadmap)
            │              │              │
            ▼              ▼              ▼
        parsers/open511  parsers/wzdx   (per-source)     ← tolerant normalizers
            └──────────────┼──────────────┘
                           ▼
                  canonical model  (models.py)
              TrafficEvent  +  LinkSpeed   (km/h, tz-aware)
                           ▼
                 store.py  (dedupe, GeoJSON)
                           ▼
        match.py  ── geometry → engine graph ids ──┐
                           ▼                        ▼
              export/valhalla.py            export/osrm.py
        predicted CSV / live edge CSV     segment-speed-file
                           ▼                        ▼
                      Valhalla                    OSRM
```

* **`catalog.py` + `registry/feeds.json`** — declarative feed specs. The
  comprehensive WZDx set is discovered live from the USDOT registry
  (`discovery.py`); the seed file only carries what discovery can't find
  (Open511 + speed feeds).
* **`client.py`** — the only networked module (lazy `httpx`): per-feed auth,
  retries with backoff, Open511 pagination, per-feed error isolation.
* **Core is pure standard library** — models, parsers, store and exporters
  install and run with **zero third-party deps**. `httpx` is only needed to
  fetch.

---

## Install

```bash
pip install -e .            # core only (parse/normalize/export, no network)
pip install -e '.[fetch]'   # + httpx, for live fetching
pip install -e '.[dev]'     # + pytest
```

Requires Python 3.9+.

## Usage

### CLI

```bash
# What feeds do we know about?
freetraffic sources list
freetraffic sources list --state CA --kind open511

# Discover every registered WZDx feed (live USDOT registry)
freetraffic sources discover-wzdx --save feeds.json

# Parse an already-downloaded payload — fully offline, no httpx needed
freetraffic parse --kind wzdx --state IA raw_wzdx.json -o snapshot.geojson
freetraffic parse --kind open511 raw_open511.json

# Fetch + normalize live (needs an API key in the env for keyed feeds)
export FT_SF511_API_KEY=...      # free token: https://511.org/open-data/token
freetraffic fetch --state CA --discover -o ca_snapshot.geojson
```

### Library

```python
import asyncio
from freetraffic import load_builtin_catalog, collect_feeds, TrafficSnapshot

cat = load_builtin_catalog()
events, errors = asyncio.run(collect_feeds(cat.select(jurisdiction="CA")))
snap = TrafficSnapshot(events=events, errors=errors).dedupe()
print(snap.dumps(indent=2))     # canonical GeoJSON FeatureCollection
```

Offline normalization (no network):

```python
from freetraffic import parse_wzdx, TrafficSnapshot
events = parse_wzdx(payload, source_id="ia-dot", jurisdiction="IA")
snap = TrafficSnapshot(events=events).dedupe()
```

### Feeding the routers

Both engines key live traffic by their **own** graph ids, so the geometry of an
event/speed must be map-matched first. `match.py` bridges this for Valhalla via
`/trace_attributes`; the exporters then emit byte-exact files:

```python
from freetraffic.export import osrm, valhalla

# OSRM: re-customize (MLD) with a segment-speed-file, then hot-reload
osrm.write_segment_speed_file([(from_node, to_node, speed_kph)], "live.csv")
#   osrm-customize data.osrm --segment-speed-file live.csv && osrm-datastore data.osrm

# Valhalla: predicted-traffic CSV (freeflow/constrained are directly usable)
valhalla.write_predicted_csv([(edge_id, freeflow_kph, constrained_kph)], "edges.csv")
#   valhalla_add_predicted_traffic -t <dir-of-csv>
valhalla.format_live_speed_csv([(edge_id, speed_kph)])   # intermediate for traffic.tar tooling
```

---

## How traffic gets into each engine (reference)

* **OSRM** — live traffic is applied by re-running `osrm-customize` (MLD) with
  `--segment-speed-file` (CSV: `from_node,to_node,speed_kph[,weight]`, speed `0`
  = impassable), then `osrm-datastore` hot-reloads a running `osrm-routed -s`.
* **Valhalla** — predicted/historical traffic is baked into tiles with
  `valhalla_add_predicted_traffic` from CSV
  (`edge_id,freeflow_speed,constrained_speed,historical_speeds`); live traffic is
  served from a memory-mapped `traffic.tar` of binary `TrafficSpeed` records.

> The `historical_speeds` codec in `export/valhalla.py` is a **reference**
> DCT-based implementation that round-trips exactly; it is *not* byte-compatible
> with a specific Valhalla build's quantization. Use the freeflow/constrained
> columns (directly usable) or your build's own encoder for that column.

---

## Roadmap

This first cut prioritizes **broad source coverage** (the chosen milestone):
canonical model, tolerant Open511 + WZDx parsers, live WZDx feed discovery,
dedupe, GeoJSON, CLI, and the export *formats*. Next:

1. **Live-speed feed parsers** — WSDOT, Caltrans PeMS, and other state detector
   feeds into `LinkSpeed` (the second, scarcer signal).
2. **Map-matching the full pipeline** — wire `match.py` (Valhalla
   `/trace_attributes`, OSRM `/match`) end-to-end so events/speeds land on edges
   automatically; write the binary `traffic.tar` for Valhalla live traffic.
3. **Fusion policy** — turn event severity / lane fractions / reduced speed
   limits into principled edge-speed penalties when no measured speed exists.
4. **Scheduler** — periodic refresh + delta updates feeding a running engine.
5. **NPMRDS ingest** — when access is available (historical/predicted, not live).

---

## Development

```bash
pip install -e '.[dev]'
pytest -q
```

The test suite runs fully offline (fixtures under `tests/fixtures/`); no network
or API keys required.

## License

MIT.
