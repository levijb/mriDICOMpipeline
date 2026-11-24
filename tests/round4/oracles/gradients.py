"""Gradient oracle: statistics of a .bval/.bvec pair under the definitions of scan-selection.md 5.2.

Oracle classes S (the FSL/BIDS text layout of the two files) and P (the 5.2 definitions).
Imports no pipeline module.

Returned values:
*   ``n_volumes``: how many b-values the file holds; ``mismatch`` is true when that count is not
    the image's fourth dimension.
*   ``b0``: how many b-values are at or below 50 s/mm^2.
*   ``shells``: the distinct values among b > 50 after rounding each to a multiple of 50 (a
    remainder of exactly 25 rounds up).
*   ``b_max``: the highest of those shells.
*   ``n_dirs``: for each shell, the number of non-zero vectors that point in different
    directions, treating v and -v as one direction and two vectors as one when the absolute
    cosine between them is at least 0.995; the largest count over the shells.
"""

from __future__ import annotations

import math

ZERO_NORM = 1e-6


def read_bval(text):
    return [float(v) for v in text.split()]


def read_bvec(text):
    rows = [[float(v) for v in line.split()] for line in text.strip().splitlines() if line.strip()]
    if len(rows) != 3 or len({len(r) for r in rows}) != 1:
        raise ValueError("a .bvec file has three rows of equal length")
    return [(rows[0][i], rows[1][i], rows[2][i]) for i in range(len(rows[0]))]


def round_to_50(b):
    return int(math.floor(b / 50.0 + 0.5) * 50)


def distinct_directions(vectors, threshold=0.995):
    reps = []
    for v in vectors:
        n = math.sqrt(sum(c * c for c in v))
        if n < ZERO_NORM:
            continue
        u = tuple(c / n for c in v)
        if not any(abs(sum(a * b for a, b in zip(u, r))) >= threshold for r in reps):
            reps.append(u)
    return len(reps)


def gradient_stats(bvals, bvecs=None, dim4=None, min_tensor_directions=6):
    """Statistics of one gradient table. ``bvecs`` may be None (b-values only)."""
    if bvecs is not None and len(bvecs) != len(bvals):
        raise ValueError("bval and bvec counts differ")
    n = len(bvals)
    b0 = sum(1 for b in bvals if b <= 50)
    shells = sorted({round_to_50(b) for b in bvals if b > 50})
    per_shell = {}
    for s in shells:
        vecs = [bvecs[i] for i, b in enumerate(bvals) if b > 50 and round_to_50(b) == s] if bvecs else []
        per_shell[s] = distinct_directions(vecs)
    n_dirs = max(per_shell.values()) if per_shell else 0
    if not shells:
        family = "DWI_B0_ONLY"
    elif n_dirs >= min_tensor_directions:
        family = "DWI_TENSOR"
    else:
        family = "DWI_TRACE"
    return {
        "n_volumes": n,
        "b0": b0,
        "shells": shells,
        "b_max": max(shells) if shells else None,
        "n_dirs": n_dirs,
        "per_shell_dirs": per_shell,
        "mismatch": (dim4 is not None and dim4 != n),
        "family": family,
    }


def stats_from_files(bval_path, bvec_path, dim4=None):
    with open(bval_path, encoding="ascii") as f:
        bvals = read_bval(f.read())
    with open(bvec_path, encoding="ascii") as f:
        bvecs = read_bvec(f.read())
    return gradient_stats(bvals, bvecs, dim4)
