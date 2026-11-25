"""R4-VAL-02: the objective checks on every acquisition `select` chose in a ranked role (spec 8.2; plan 3.5).

    python tests/round4/realdata/r4_objective.py --project D:\\mri_project --private D:\\round4\\private \\
        --reports D:\\round4\\private\\runA2 --evidence D:\\round4\\evidence\\realdata

For each role decision with outcome selected or selected_fallback in T1, T1_POST, T2, FLAIR, DWI, SWI, QSM or GRE,
the acquisition's member series are found through the published sidecars' source_series_key, and
oracles/objective_checks.check_choice (frozen at G1) runs on the header reader's records of those series
(<private>/reader/<session code>.json, written by r4_facts.py) and on the primary volume's NiBabel shape and
gradient files. No fact comes from `select`'s records.

Owner amendment of 2026-11-25 (R4_GATES.md), applied after check_choice and marked `amendment` in the output: an
`SWI` token in a member series' ImageType counts as derived (category derived_as_native), because Siemens
computes that image from magnitude and phase although it labels it ORIGINAL (R4-F-019).

Writes (evidence, coded): objective.csv, objective_summary.json. Prints counts only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

import r4real

RANKED = ("T1", "T1_POST", "T2", "FLAIR", "DWI", "SWI", "QSM", "GRE")
FIELDS = ["visit", "session", "role", "outcome", "category", "rule", "series", "series_number", "detail"]


def main(argv=None):
    import nibabel as nib
    from oracles import objective_checks

    p = argparse.ArgumentParser(description="R4-VAL-02 objective checks on select's choices")
    p.add_argument("--project", required=True)
    p.add_argument("--private", required=True)
    p.add_argument("--reports", required=True, help="folder holding the first-run selection CSVs")
    p.add_argument("--evidence", required=True)
    a = p.parse_args(argv)
    r4real.check_outputs(a.project, a.private, a.evidence)
    codes = r4real.Codes(a.private)
    decisions = r4real.read_csv(os.path.join(a.reports, "selection.csv"))
    vrows = r4real.read_csv(os.path.join(a.reports, "selection_volumes.csv"))
    by_acq = {}
    for r in vrows:
        by_acq.setdefault((r["subject_id"], r["session"], r["acquisition_id"]), []).append(r)
    reader_cache = {}

    def reader_records(subject, label):
        code = codes.session(subject, label)
        if code not in reader_cache:
            with open(os.path.join(a.private, "reader", f"{code}.json"), encoding="utf-8") as f:
                reader_cache[code] = json.load(f)["series"]
        return reader_cache[code]

    out, checked, flagged = [], 0, set()
    for d in decisions:
        if d["role"] not in RANKED or d["outcome"] not in ("selected", "selected_fallback") or not d["candidate_id"]:
            continue
        visit = codes.visit(d["subject_id"], d["session"])
        members = by_acq.get((d["subject_id"], d["session"], d["candidate_id"]), [])
        if not members:
            out.append({"visit": visit, "role": d["role"], "outcome": d["outcome"], "category": "no_member_rows",
                        "rule": "harness", "series_number": "", "detail": "candidate has no volume rows"})
            continue
        checked += 1
        primary_row = next((m for m in members if m["source"] == d["source"]), members[0])
        keys, label_of = [], {}
        for m in [primary_row] + [m for m in members if m is not primary_row]:
            key = r4real.sidecar(a.project, m["source"]).get("source_series_key")
            label_of[key] = m["source"].split("/")[2]
            if key not in keys:
                keys.append(key)
        recs, missing = [], []
        for key in keys:
            rec = reader_records(d["subject_id"], label_of[key]).get(key)
            (recs.append(rec) if rec else missing.append(key))
        if missing:
            out.append({"visit": visit, "role": d["role"], "outcome": d["outcome"], "category": "missing_reader_record",
                        "rule": "harness", "series_number": "", "detail": f"{len(missing)} member series"})
        if not recs:
            continue
        session_records = list(reader_records(d["subject_id"], label_of[keys[0]]).values())
        nifti = os.path.join(a.project, "Niftis", *primary_row["source"].split("/"))
        shape = list(nib.load(nifti).shape)
        volume = {"shape": shape, "rows": recs[0].get("rows"), "cols": recs[0].get("cols"),
                  "n_slices": recs[0].get("n_positions")}
        stem = nifti[:-7]
        if os.path.exists(stem + ".bval"):
            values = [float(x) for x in open(stem + ".bval", encoding="ascii").read().split()]
            volume["n_bvals"], volume["b_values"] = len(values), values
        te = {keys[0]: float(primary_row["echo_te_ms"])} if primary_row.get("echo_te_ms") else None
        flags = objective_checks.check_choice(d["role"], recs, session_records, member_te=te, volume=volume,
                                              chosen_by_override=d["decided_by"] == "override")
        number = {r.get("series_key"): r.get("series_number") for r in recs}
        for fl in flags:
            out.append({"visit": visit, "session": codes.session(d["subject_id"], label_of[keys[0]]), "role": d["role"],
                        "outcome": d["outcome"], "category": fl["category"], "rule": "frozen",
                        "series": (fl["series_key"] or "")[:8], "series_number": number.get(fl["series_key"], ""),
                        "detail": fl["detail"]})
            flagged.add((visit, d["role"]))
        frozen_derived = {fl["series_key"] for fl in flags if fl["category"] == "derived_as_native"}
        for rec in recs:
            if "SWI" in set(rec.get("image_type") or []) and rec.get("series_key") not in frozen_derived:
                out.append({"visit": visit, "session": codes.session(d["subject_id"], label_of[keys[0]]), "role": d["role"],
                            "outcome": d["outcome"], "category": "derived_as_native", "rule": "amendment",
                            "series": (rec.get("series_key") or "")[:8], "series_number": rec.get("series_number"),
                            "detail": "SWI token in ImageType (owner amendment 2026-11-25)"})
                flagged.add((visit, d["role"]))
    r4real.write_csv(os.path.join(a.evidence, "objective.csv"), out, FIELDS)
    real = [r for r in out if r["rule"] != "harness"]
    summary = {"decisions_checked": checked, "decisions_flagged": len(flagged),
               "flags_by_category": dict(Counter(r["category"] for r in real)),
               "flags_by_rule": dict(Counter(r["rule"] for r in real)),
               "flags_by_role": dict(Counter(r["role"] for r in real)),
               "harness_rows": dict(Counter(r["category"] for r in out if r["rule"] == "harness")),
               "amendment": "SWI ImageType token counts as derived (owner, 2026-11-25; R4-F-019)"}
    with open(os.path.join(a.evidence, "objective_summary.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, indent=1, sort_keys=True)
    print(f"decisions checked {checked}; flagged {len(flagged)}; flags "
          + " ".join(f"{k}={v}" for k, v in sorted(summary["flags_by_category"].items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
