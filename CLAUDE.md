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
   from the last session); a real MBTA VehiclePositions snapshot now lives at
   `tests/fixtures/live/mbta_vehiclepositions.pb` to drive that test.
2. **Broaden sources** (data-first). DONE: CBP border wait (XML), OHGO, NCDOT,
   generic ArcGIS + Socrata adapters (parsers + offline tests); a
   `response_format` field + text/XML path in `client.py`; `fetch --discover`
   already auto-ingests the WZDx registry. **2026-06-30 live-probe pass:**
   - Fixed `client.py` Accept header so xml feeds actually get xml (CBP was
     content-negotiating to JSON and silently dropping every event).
   - Repointed `ncdot-incidents` at `https://www.drivenc.gov/api/v2/get/event`
     (the legacy eapps endpoint now returns a redirect notice) -- DriveNC is
     now an IBI511/Travel-IQ stack, key required.
   - Added `arcgis-ncdot-tims` (NCDOT TIMS Incidents via Esri-hosted
     FeatureServer, keyless, enabled).
   - Verified IBI511 base URLs for FL/UT/NewEngland (all return 'Invalid Key'
     400 without `?key=`, i.e. the URLs are right -- just need keys).
   - `ibi511-ia`/`ibi511-ne`: live-confirmed these are **not** IBI511 stacks
     (the sites are SPAs that return index.html for `/api/*`). Left disabled
     with explicit notes; they need bespoke adapters before they can ship.
   - Captured trimmed live fixtures under `tests/fixtures/live/` and added
     `tests/test_live_fixtures.py` so parser regressions against real
     payload shape get caught.
   - STILL TODO: register keys for FL/UT/NC/NewEngland and turn on
     `ibi511-fl-events`/`ibi511-ut-events`/`ncdot-incidents`/`ibi511-newengland-events`
     in production; wire a link-SPEED Socrata parser to turn on
     `socrata-nyc-traffic-speeds`; add Caltrans LCS + PeMS, MassDOT;
     adapters for 511IA and 511NE's actual (non-IBI511) APIs.
3. **Validate Mode B against the live Valhalla** — confirm the `traffic.tar`
   byte format vs the running build; tune breakpoint/congestion fields; add a
   before/after route ETA check. **Blocked from this session: no FT_VALHALLA_URL
   reachable / no shell on the Valhalla host.** Everything offline-testable
   in `valhalla_traffic.py` continues to round-trip.
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
