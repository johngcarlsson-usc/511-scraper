"""Mode B: write live speeds into a Valhalla ``traffic.tar`` (native live traffic).

This is the "proper" integration: Valhalla memory-maps a ``traffic.tar`` whose
layout mirrors the routing tiles, and reads a per-edge ``TrafficSpeed`` record
at request time -- so live speeds affect BOTH path choice and travel time, with
no per-request work.

Workflow on the server side (one-time):

    valhalla_build_extract -c valhalla.json --overwrite ...   # build tiles
    # create the (empty) traffic extract that this writer fills:
    valhalla_build_extract -c valhalla.json --traffic --overwrite

Then, each update cycle, call :class:`TrafficTarUpdater` to stamp fresh speeds.
Run a ``valhalla_service`` with the traffic extract configured
(``mjolnir.traffic_extract``); it picks up changes live.

Struct layout is taken verbatim from Valhalla source (baldr/traffictile.h,
baldr/graphid.h):

* ``TrafficSpeed`` -- 8-byte bitfield; speed encoded as ``kph >> 1`` (2 kph
  resolution); ``127`` = unknown.
* ``TrafficTileHeader`` -- 32 bytes: tile_id(u64), last_update(u64),
  directed_edge_count(u32), traffic_tile_version(u32), spare2(u32), spare3(u32).
* ``GraphId`` -- level = v & 0x7, tileid = (v>>3)&0x3FFFFF, index = (v>>25)&0x1FFFFF.

NOTE: ``traffic_tile_version`` tracks Valhalla's major version. This writer
*preserves* the existing header (tile_id/count/version) and only updates
``last_update`` plus the speed records, so it doesn't need to hard-code a
version. Still, validate against your build the first time (see the round-trip
test in tests/test_valhalla_traffic.py and a quick route sanity check).
"""

from __future__ import annotations

import struct
import tarfile
import time
from typing import Dict, Iterable, Optional, Sequence, Tuple

from ..models import LinkSpeed

HEADER_SIZE = 32          # bytes; TrafficTileHeader
RECORD_SIZE = 8           # bytes; TrafficSpeed
UNKNOWN_SPEED_RAW = 127   # 7-bit "unknown"
MAX_ENCODED = 126         # highest *known* encoded value (126<<1 = 252 kph)

_LEVEL_MASK = 0x7
_TILEID_MASK = 0x3FFFFF
_INDEX_MASK = 0x1FFFFF
_TILE_KEY_MASK = 0x1FFFFFF  # level (3) + tileid (22): the header's tile_id


# --------------------------------------------------------------------------- #
# GraphId
# --------------------------------------------------------------------------- #

def decode_graphid(edge_id: int) -> Tuple[int, int, int]:
    """(level, tileid, index) for a Valhalla edge GraphId value."""
    return (
        edge_id & _LEVEL_MASK,
        (edge_id >> 3) & _TILEID_MASK,
        (edge_id >> 25) & _INDEX_MASK,
    )


def tile_key(edge_id: int) -> int:
    """The tile's GraphId (level+tileid, index=0) -- matches the header tile_id."""
    return edge_id & _TILE_KEY_MASK


# --------------------------------------------------------------------------- #
# TrafficSpeed encoding
# --------------------------------------------------------------------------- #

def encode_speed(kph: float) -> int:
    """km/h -> 7-bit encoded value (2 kph resolution), clamped to 'known'."""
    if kph is None or kph < 0:
        return UNKNOWN_SPEED_RAW
    enc = int(round(kph)) >> 1
    return max(0, min(MAX_ENCODED, enc))


def pack_traffic_speed(
    overall: int,
    speed1: int,
    speed2: int = 0,
    speed3: int = 0,
    breakpoint1: int = 255,
    breakpoint2: int = 255,
    congestion1: int = 0,
    congestion2: int = 0,
    congestion3: int = 0,
    has_incidents: int = 0,
) -> bytes:
    value = (
        (overall & 0x7F)
        | ((speed1 & 0x7F) << 7)
        | ((speed2 & 0x7F) << 14)
        | ((speed3 & 0x7F) << 21)
        | ((breakpoint1 & 0xFF) << 28)
        | ((breakpoint2 & 0xFF) << 36)
        | ((congestion1 & 0x3F) << 44)
        | ((congestion2 & 0x3F) << 50)
        | ((congestion3 & 0x3F) << 56)
        | ((has_incidents & 0x1) << 62)
    )
    return struct.pack("<Q", value)


def unpack_traffic_speed(buf: bytes) -> Dict[str, int]:
    (value,) = struct.unpack("<Q", buf)
    return {
        "overall_encoded_speed": value & 0x7F,
        "encoded_speed1": (value >> 7) & 0x7F,
        "encoded_speed2": (value >> 14) & 0x7F,
        "encoded_speed3": (value >> 21) & 0x7F,
        "breakpoint1": (value >> 28) & 0xFF,
        "breakpoint2": (value >> 36) & 0xFF,
        "congestion1": (value >> 44) & 0x3F,
        "congestion2": (value >> 50) & 0x3F,
        "congestion3": (value >> 56) & 0x3F,
        "has_incidents": (value >> 62) & 0x1,
    }


def uniform_speed_record(kph: float) -> bytes:
    """A TrafficSpeed for one uniform live speed across the whole edge."""
    enc = encode_speed(kph)
    # breakpoint1=255 -> speed1 covers the entire edge; overall mirrors it.
    return pack_traffic_speed(overall=enc, speed1=enc, breakpoint1=255, breakpoint2=255)


def unknown_speed_record() -> bytes:
    """A TrafficSpeed meaning 'no live data' (how a fresh extract initializes)."""
    return pack_traffic_speed(
        overall=UNKNOWN_SPEED_RAW,
        speed1=UNKNOWN_SPEED_RAW,
        speed2=UNKNOWN_SPEED_RAW,
        speed3=UNKNOWN_SPEED_RAW,
        breakpoint1=0,
        breakpoint2=0,
    )


# --------------------------------------------------------------------------- #
# The updater
# --------------------------------------------------------------------------- #

class TrafficTarUpdater:
    """Write per-edge live speeds into an existing Valhalla traffic.tar.

    Open it, call :meth:`set_speeds` / :meth:`apply_link_speeds` one or more
    times, then :meth:`close`. Edits are in-place (the archive size is unchanged
    because we only overwrite fixed-size records), so a running service with the
    extract mapped sees the new values.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = open(path, "r+b")
        self._index: Dict[int, Tuple[int, int]] = {}  # tile_key -> (offset_data, edge_count)
        self._build_index()

    def _build_index(self) -> None:
        with tarfile.open(fileobj=self._fh, mode="r") as tar:
            for member in tar.getmembers():
                if not member.isfile() or member.size < HEADER_SIZE:
                    continue
                self._fh.seek(member.offset_data)
                head = self._fh.read(HEADER_SIZE)
                tile_id, _last, edge_count, _ver, _s2, _s3 = struct.unpack("<QQIIII", head)
                self._index[tile_id & _TILE_KEY_MASK] = (member.offset_data, edge_count)

    @property
    def tile_count(self) -> int:
        return len(self._index)

    def set_speeds(self, edge_speeds: Dict[int, float]) -> int:
        """Write {edge_id: speed_kph}. Returns the number of records written.

        Edges whose tile isn't in this extract, or whose index is out of range,
        are skipped (and counted in :attr:`skipped`).
        """
        written = 0
        self.skipped = 0
        touched_tiles = set()
        for edge_id, kph in edge_speeds.items():
            loc = self._index.get(tile_key(edge_id))
            if loc is None:
                self.skipped += 1
                continue
            offset_data, edge_count = loc
            _, _, index = decode_graphid(edge_id)
            if index >= edge_count:
                self.skipped += 1
                continue
            self._fh.seek(offset_data + HEADER_SIZE + index * RECORD_SIZE)
            self._fh.write(uniform_speed_record(kph))
            written += 1
            touched_tiles.add(offset_data)
        self._stamp(touched_tiles)
        self._fh.flush()
        return written

    def read_record(self, edge_id: int) -> Optional[Dict[str, int]]:
        """Decode the current TrafficSpeed for an edge (None if not in extract)."""
        loc = self._index.get(tile_key(edge_id))
        if loc is None:
            return None
        offset_data, edge_count = loc
        _, _, index = decode_graphid(edge_id)
        if index >= edge_count:
            return None
        self._fh.seek(offset_data + HEADER_SIZE + index * RECORD_SIZE)
        return unpack_traffic_speed(self._fh.read(RECORD_SIZE))

    def read_speed_kph(self, edge_id: int) -> Optional[int]:
        """Current overall speed for an edge in km/h, or None if unknown/absent."""
        rec = self.read_record(edge_id)
        if rec is None:
            return None
        enc = rec["overall_encoded_speed"]
        return None if enc == UNKNOWN_SPEED_RAW else enc << 1

    def apply_link_speeds(self, speeds: Sequence[LinkSpeed]) -> int:
        """Convenience: write LinkSpeeds whose ``link_id`` is a Valhalla edge id."""
        mapping: Dict[int, float] = {}
        for s in speeds:
            try:
                mapping[int(s.link_id)] = s.speed_kph
            except (TypeError, ValueError):
                continue
        return self.set_speeds(mapping)

    def _stamp(self, tile_offsets: Iterable[int]) -> None:
        now = int(time.time())
        for offset_data in tile_offsets:
            self._fh.seek(offset_data + 8)  # last_update is the 2nd u64
            self._fh.write(struct.pack("<Q", now))

    def close(self) -> None:
        if self._fh and not self._fh.closed:
            self._fh.flush()
            self._fh.close()

    def __enter__(self) -> "TrafficTarUpdater":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
