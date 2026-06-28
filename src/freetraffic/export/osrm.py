"""Export to OSRM's live-traffic input: the segment speed file.

OSRM applies live traffic by re-running ``osrm-customize`` (MLD) with a
``--segment-speed-file``. The file is CSV with one row per directed segment::

    from_node_id,to_node_id,speed_kph[,weight]

``from_node_id`` / ``to_node_id`` are OSRM *internal* node ids (the values you
get from ``osrm-extract`` / the .osrm graph), and ``speed_kph`` is an integer.
The optional fourth column overrides the routing weight.

Workflow once a file is written (MLD)::

    osrm-customize data.osrm --segment-speed-file live.csv
    osrm-datastore data.osrm    # hot-reload a running osrm-routed -s

This module produces the file; resolving record geometry -> node-pair ids is the
map-matching step (project geometry onto the OSRM graph; out of scope here and
documented in the README roadmap).
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence, Tuple, Union

# (from_node, to_node, speed_kph) or (from_node, to_node, speed_kph, weight)
SegmentRow = Union[
    Tuple[int, int, float],
    Tuple[int, int, float, float],
]

# A fully-closed road: OSRM treats speed 0 as impassable.
CLOSED_SPEED_KPH = 0


def format_segment_speed_file(rows: Iterable[SegmentRow]) -> str:
    """Render rows as an OSRM segment-speed-file (no header, integer speeds)."""
    lines = []
    for row in rows:
        if len(row) == 4:
            f, t, speed, weight = row
            lines.append(f"{int(f)},{int(t)},{int(round(speed))},{weight}")
        else:
            f, t, speed = row  # type: ignore[misc]
            lines.append(f"{int(f)},{int(t)},{int(round(speed))}")
    return "\n".join(lines) + ("\n" if lines else "")


def write_segment_speed_file(rows: Iterable[SegmentRow], path: str) -> int:
    """Write the segment-speed-file; return the number of rows written."""
    rows = list(rows)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(format_segment_speed_file(rows))
    return len(rows)


def closure_rows(
    segments: Iterable[Tuple[int, int]],
    speed_kph: int = CLOSED_SPEED_KPH,
) -> Iterable[SegmentRow]:
    """Turn matched node-pairs of a closed road into segment rows."""
    for f, t in segments:
        yield (f, t, speed_kph)
