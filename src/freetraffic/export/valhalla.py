"""Export to Valhalla's traffic inputs.

Valhalla has two traffic mechanisms:

1. **Predicted / historical traffic** baked into tiles at build time via
   ``valhalla_add_predicted_traffic -t <dir>``. It reads CSV files mirroring the
   tile hierarchy with columns::

       edge_id,freeflow_speed,constrained_speed,historical_speeds

   ``freeflow_speed`` / ``constrained_speed`` are integer km/h. ``historical_speeds``
   is an *optional* compressed string of 2016 five-minute buckets (one week,
   Sunday 00:00 start), DCT-encoded then base64'd.

2. **Live traffic** served from a memory-mapped ``traffic.tar`` (binary
   ``TrafficSpeed`` records keyed by edge id), updated at runtime.

This module covers (1): it emits valid predicted-traffic CSV. The simplest
valid file is just ``edge_id,freeflow_speed,constrained_speed`` with an empty
historical column -- recommended as the starting point. A reference DCT
encoder for the historical column is provided and round-trip tested, but its
*on-the-wire* compatibility with a given Valhalla build should be validated
against that build (the quantization scheme has varied across versions).

The live ``traffic.tar`` path is binary and tied to Valhalla's C++ structs; it
is documented in the README roadmap rather than implemented here. The CSV this
module emits is keyed by Valhalla edge ids, which you obtain by map-matching a
record's geometry (see :mod:`freetraffic.match`).
"""

from __future__ import annotations

import base64
import math
import struct
from typing import Iterable, List, Optional, Sequence, Tuple, Union

BUCKETS_PER_WEEK = 2016  # 7 days * 24 h * 12 (five-minute buckets)

# (edge_id, freeflow_kph, constrained_kph) optionally + historical buckets/string
PredictedRow = Union[
    Tuple[int, float, float],
    Tuple[int, float, float, Optional[object]],
]


def format_predicted_csv(rows: Iterable[PredictedRow]) -> str:
    """Render rows as a Valhalla predicted-traffic CSV.

    The 4th element of a row, if present, is either a pre-encoded historical
    string or a sequence of 2016 speeds (which we encode for you).
    """
    out: List[str] = []
    for row in rows:
        edge_id = int(row[0])
        freeflow = int(round(row[1]))
        constrained = int(round(row[2]))
        historical = ""
        if len(row) >= 4 and row[3] is not None:
            hist = row[3]
            historical = hist if isinstance(hist, str) else encode_historical_speeds(hist)
        out.append(f"{edge_id},{freeflow},{constrained},{historical}")
    return "\n".join(out) + ("\n" if out else "")


def write_predicted_csv(rows: Iterable[PredictedRow], path: str) -> int:
    rows = list(rows)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(format_predicted_csv(rows))
    return len(rows)


def format_live_speed_csv(rows: Iterable[Tuple[int, float]]) -> str:
    """Render a simple ``edge_id,speed_kph`` CSV for live-traffic tooling.

    This is the convenient intermediate to feed a ``traffic.tar`` updater that
    writes binary ``TrafficSpeed`` records per edge id.
    """
    out = [f"{int(edge_id)},{int(round(speed))}" for edge_id, speed in rows]
    return "\n".join(out) + ("\n" if out else "")


# --------------------------------------------------------------------------- #
# Reference historical-speed codec (DCT-II / DCT-III).
#
# IMPORTANT: this is a *reference* codec that demonstrates the DCT-based weekly
# compression Valhalla uses and round-trips exactly. It is NOT byte-compatible
# with any specific Valhalla build's `historical_speeds` quantization. For a
# Valhalla import, prefer the freeflow/constrained columns (directly usable) or
# encode the historical column with your build's own tooling. By default the
# codec keeps every coefficient (lossless up to float32); pass ``keep`` to
# truncate high-frequency terms for a smaller, lossy string.
# --------------------------------------------------------------------------- #

def encode_historical_speeds(
    speeds: Sequence[float], *, keep: Optional[int] = None
) -> str:
    """Compress a week of speeds (2016 km/h values) to a base64 string.

    Applies a DCT-II and packs the coefficients as little-endian float32. With
    ``keep=None`` the transform is fully invertible; ``keep=k`` retains only the
    first ``k`` (lowest-frequency) coefficients for a smaller, lossy result.
    """
    if len(speeds) != BUCKETS_PER_WEEK:
        raise ValueError(
            f"expected {BUCKETS_PER_WEEK} speed buckets, got {len(speeds)}"
        )
    coeffs = _dct2([float(s) for s in speeds])
    if keep is not None:
        coeffs = coeffs[:keep]
    packed = struct.pack(f"<{len(coeffs)}f", *coeffs)
    return base64.b64encode(packed).decode("ascii")


def decode_historical_speeds(encoded: str) -> List[float]:
    """Inverse of :func:`encode_historical_speeds`."""
    raw = base64.b64decode(encoded)
    n = len(raw) // 4
    coeffs = list(struct.unpack(f"<{n}f", raw[: n * 4]))
    # pad truncated coefficient sets back to a full week before inverting
    coeffs = (coeffs + [0.0] * BUCKETS_PER_WEEK)[:BUCKETS_PER_WEEK]
    return [max(0.0, v) for v in _idct(coeffs)]


def _dct2(x: List[float]) -> List[float]:
    """Naive DCT-II. O(n^2); fine for one-off 2016-point weekly profiles."""
    n = len(x)
    factor = math.pi / n
    return [
        sum(x[i] * math.cos(factor * (i + 0.5) * k) for i in range(n))
        for k in range(n)
    ]


def _idct(coeffs: List[float]) -> List[float]:
    """Inverse (DCT-III), normalized to invert :func:`_dct2`."""
    n = len(coeffs)
    factor = math.pi / n
    half0 = coeffs[0] / 2.0
    out = []
    for i in range(n):
        acc = half0 + sum(
            coeffs[k] * math.cos(factor * (i + 0.5) * k) for k in range(1, n)
        )
        out.append(2.0 * acc / n)
    return out
