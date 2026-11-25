"""R4-VAL-04 (report only): per role, how consistent the selected protocols are across subjects (spec R-VAL-4, D-7).

    python tests/round4/realdata/r4_summary.py --reports D:\\round4\\private\\runA2 --evidence D:\\round4\\evidence\\realdata

From the first-run selection.csv: over the role decisions with outcome selected or selected_fallback, how often
each value of family, tier, mr_acq and native_mm (as reported, r x c x s mm) occurs per role, counted in
decisions and in distinct subjects. Writes summary.csv (no identifiers).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

import r4real


def main(argv=None):
    p = argparse.ArgumentParser(description="R4-VAL-04 cross-subject summary")
    p.add_argument("--reports", required=True)
    p.add_argument("--evidence", required=True)
    a = p.parse_args(argv)
    rows = r4real.read_csv(os.path.join(a.reports, "selection.csv"))
    chosen = [r for r in rows if r["outcome"] in ("selected", "selected_fallback")]
    counts = defaultdict(lambda: [0, set()])
    for r in chosen:
        for field in ("family", "tier", "mr_acq", "native_mm", "outcome"):
            c = counts[(r["role"], field, r[field] or "(none)")]
            c[0] += 1
            c[1].add(r["subject_id"])
    out = [{"role": role, "field": field, "value": value, "decisions": n, "subjects": len(subs)}
           for (role, field, value), (n, subs) in sorted(counts.items())]
    r4real.write_csv(os.path.join(a.evidence, "summary.csv"), out, ["role", "field", "value", "decisions", "subjects"])
    print(f"chosen decisions {len(chosen)}; summary rows {len(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
