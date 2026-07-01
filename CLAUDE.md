# freetraffic — context for Claude Code

A Python package that scrapes **free** public traffic data and turns it into
**traffic-aware travel-time predictions** for routing on a self-hosted
**Valhalla** server. (OSRM was retired; target Valhalla.) Repo: `511-scraper`,
work on branch `claude/traffic-aware-routing-package-wi887n`.

The mission: be the definitive, comprehensive layer that pulls as many free
traffic sources as possible (state 511, Open511, WZDx, transit probes, DOT
travel-times, weather, freemium flow) and fuses them into a predicted speed per
routing-graph edge, then feeds Valhalla.

## The core idea (the "spine")

Everything collapses to **one intermediate: a predicted speed per Valhalla
edge.** Travel time = `Σ length / speed` over a route's edges
(`predict/fusion.py`). Sources combine by precedence:

1. **Hard constraint** — full closures (511/Open511/WZDx) → edge impassable.
2. **Measured speed** (wins; already reflects incidents/weather) — GTFS-RT
   probes, WSDOT travel-times, IBI511 `GetTrafficSpeeds`, TomTom flow. Best by
   confidence × recency; stale dropped.
3. **Modeled fallback** (only where unmeasured) — incident/lane penalty
   (severity & lane fraction), WZDx reduced-speed cap, NWS weather multiplier.

Two ways to apply per-edge speeds to Valhalla:
- **Mode A** (`routing/service.py::route_with_eta`): post-hoc — route, pull the
  chosen edges via `/trace_attributes`, re-time with fused speeds. Any Valhalla,
  no rebuild.
- **Mode B** (`export/valhalla_traffic.py`): write speeds into Valhalla's native
  `traffic.tar` so it uses them for routing AND timing. Needs build access to the
  Valhalla tiles. Struct layout verified vs Valhalla source; round-trips in
  tests but **validate against the real build on first live use**.

## Layout

```
src/freetraffic/
  models.py            canonical TrafficEvent + LinkSpeed (km/h, tz-aware datetimes)
  geometry.py          GeoJSON helpers, polyline decode, haversine, bbox
  catalog.py           FeedSpec + Catalog; registry/feeds.json is the seed
  registry/feeds.json  declarative feed catalog (data, not code)
  client.py            ONLY feed-fetch module (lazy httpx): auth, retry, pagination
  discovery.py         live USDOT WZDx feed-registry discovery
  parsers/             one module per source format -> canonical records
    open511.py wzdx.py ibi511.py wsdot.py nws.py
  probes/              measured-speed probes
    gtfs_rt.py         transit vehicles as floating probes (optional [gtfs] extra)
    tomtom.py          freemium Flow lookups (live-lookup only; ToS: no caching)
  predict/fusion.py    THE SPINE: sources -> per-edge speed -> ETA
  routing/             ValhallaClient (Basic auth + ngrok) + TrafficAwareRouter
  export/              valhalla.py (predicted CSV), osrm.py (legacy),
                       valhalla_traffic.py (Mode B traffic.tar writer)
  testkit/             record/replay httpx transports (cassettes) for e2e tests
  cli.py               `freetraffic` entry point
tests/                 pytest, fully offline (fixtures + mocked/replayed transport)
```

## How to run

```bash
pip install -e '.[dev]'          # core is stdlib-only; dev adds httpx, gtfs, pytest
cp .env.example .env             # fill creds/keys; .env is gitignored + auto-loaded
pytest -q                        # all offline

freetraffic check                            # verify Valhalla URL/auth/ngrok
freetraffic sources list
freetraffic fetch --state NY -o ny.geojson   # needs FT_IBI511_NY_KEY
freetraffic route --from LAT,LON --to LAT,LON --snapshot ny.geojson --match-speeds
freetraffic traffic-update --tar traffic.tar --csv edge_speeds.csv   # Mode B
```

Env vars (see `.env.example`): `FT_VALHALLA_URL/USER/PASS/NGROK`,
`FT_SF511_API_KEY`, `FT_IBI511_<ST>_KEY`, `FT_WSDOT_API_KEY`, `FT_TOMTOM_API_KEY`.

## Conventions (follow these)

- **Core stays pure standard library.** `httpx` only inside `client.py`,
  `routing/`, `probes/`, imported lazily. Optional deps behind extras
  (`[fetch]`, `[gtfs]`).
- **Adding a source = mostly data.** If it speaks Open511/WZDx/IBI511, add a
  `FeedSpec` to `registry/feeds.json` (no code). New format → new
  `parsers/<name>.py` pure function `parse(payload, *, source_id, jurisdiction)`
  returning `List[TrafficEvent]` or `List[LinkSpeed]`; register its `kind` in
  `parsers/__init__.py` (EVENT_PARSERS/SPEED_PARSERS) and `catalog.py`
  (EVENT_KINDS/SPEED_KINDS).
- **Parsers are tolerant** — real feeds drift; never raise on missing/extra
  fields; normalize units to **km/h** and tz-aware datetimes.
- **Every change ships a test**, and tests run **offline** (fixtures under
  `tests/fixtures/`; use `testkit` cassettes or `httpx.MockTransport` for
  network paths). Don't add tests that need live network or real keys.
- **Never commit secrets.** Credentials live only in `.env`. Before committing,
  check nothing key-like is staged.
- **Commit + push to the working branch** with clear messages after each
  coherent unit of work.
- Match existing style; keep functions small and documented.

## Roadmap (do in this order unless told otherwise)

1. **Continuous GTFS-RT → traffic.tar service loop.** A long-running poller:
   fetch VehiclePositions → `GtfsRtProbeTracker.update` → `map_match_probes`
   (Valhalla) → `aggregate_by_edge` → `TrafficTarUpdater.set_speeds`, on a timer,
   for one or more agencies. This is what makes it a *running service*.
   Still needs a live Valhalla end-to-end shakedown (no live server reachable
   from the cloud session); a real MBTA VehiclePositions snapshot lives at
   `tests/fixtures/live/mbta_vehiclepositions.pb` to drive that test.
2. **Broaden sources** (data-first). DONE -- merged from a live-probe pass and a
   doc-research pass (both 2026-06-30):
   - `client.py` sends a format-appropriate Accept header (xml feeds were
     content-negotiating to JSON; CBP silently dropped every event) + a text/xml
     fetch path; `fetch --discover` auto-ingests the WZDx registry.
   - IBI511 is tolerant of both API shapes (OLD `/api/GetEvents` dd/MM/yyyy and
     NEW `/api/v2/get/event` epoch-seconds); ~18 jurisdictions in the registry.
     FL/UT/NewEngland base URLs live-verified; NV/CT/AZ/LA/ID/ON/AB/PA are
     research-derived (noted, verify). Removed the fictional `GetTrafficSpeeds`
     feeds; added `ibi511_traveltimes` (511WI) for measured speeds.
   - `ibi511-ia`/`ibi511-ne` live-confirmed NOT IBI (SPAs) -> disabled; use the
     keyless ArcGIS CARS feeds instead. NCDOT eapps API retired -> DriveNC
     (`ibi511-nc-events`, key) + keyless `arcgis-ncdot-tims` mirror.
   - New parsers: CBP (XML, + BTS port->coord lookup `registry/cbp_ports.json`),
     Caltrans LCS (XML, 12 districts), MassDOT events (XML) + WZDx, OHGO, generic
     ArcGIS + Socrata. Keyless ArcGIS/Socrata inventory (Iowa CARS IA/NE/MN,
     NCDOT TIMS, OR, TN, MD CHART, MI, Austin TX).
   - Live fixtures under `tests/fixtures/live/` + `tests/test_live_fixtures.py`
     guard against parser drift on real payloads.
   STILL TODO (open-network host): register keys + flip on the key-gated IBI feeds;
   verify the research-derived NEW-gen URLs; run `discovery.fetch_cbp_port_coords()`
   for the full CBP port table; add a link-SPEED Socrata parser for
   `socrata-nyc-traffic-speeds` + Maryland CHART / Houston TranStar measured
   speeds; bespoke adapters for 511IA/511NE; Caltrans PeMS bulk ingest.
3. **Validate Mode B against the live Valhalla** — confirm the `traffic.tar`
   byte format vs the running build; tune breakpoint/congestion fields; add a
   before/after route ETA check. **Partial: 2026-06-30 live probe against
   `https://jgc-valhalla.ngrok.app` (Valhalla 3.4.0, tileset 2026-05-31,
   coverage = US+Canada) confirmed Mode A end-to-end** -- `check`, `/route`,
   `/trace_attributes`, and the GTFS-RT probe pipeline (MBTA
   VehiclePositions -> map_match_probes -> aggregate_by_edge) all work.
   See `scripts/live_gtfs_rt_smoke.py`. **Mode B traffic.tar byte-format
   check still blocked**: only the routing endpoints are exposed over ngrok,
   there's no shell on the tile host, so we can't run
   `valhalla_build_extract --traffic` or write into traffic.tar and rerun
   a route to confirm the ETA moves.
4. **Fusion tuning** — calibrate `FusionConfig` multipliers against observed
   data; add per-edge confidence blending when multiple measured sources agree.
5. **Camera CV + NPMRDS** — traffic-camera vehicle detection for density/speed;
   NPMRDS ingest when access lands (historical/predicted).

## Important caveats

- **Cloud Claude Code sessions may have locked egress** — the managed sandbox
  blocked `data.transportation.gov` and the ngrok Valhalla host (HTTP 403 at the
  org proxy). Live fetch/route/Mode-B validation must run where the network is
  open (this local machine). Don't disable TLS or route around proxies.
- The Valhalla `traffic.tar` writer is verified against source structs and unit
  round-trips, but the on-disk format is version-sensitive — sanity-check on the
  real server the first time.
- TomTom/HERE/Mapbox are **freemium**: live lookups only, never cache or
  redistribute their data (ToS). Build your own dataset only from free/open
  sources (511, WZDx, GTFS-RT, NWS, DOT feeds).
