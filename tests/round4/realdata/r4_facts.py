"""R4-VAL-01: the facts `select` recorded against the independent header reader, for every series of every
converted session (spec 8.2; plan 3.4).

    python tests/round4/realdata/r4_facts.py --project D:\\mri_project --private D:\\round4\\private \\
        --evidence D:\\round4\\evidence\\realdata

`select`'s facts are the series records of its header cache (Reports/.selection_cache/<subject>/<session>.json,
scan-selection.md 7.3). The reader is the frozen oracles/header_reader.py; fields are compared with its own
comparator (plan 3.4), with two owner readings: `software` is the SoftwareVersions values joined as the cache
stores them and compared as an exact string (R4-F-010, 2026-11-25), and an empty `echoes` list equals an absent
one (5.1: multi-echo series only). Volume records: `dims` against NiBabel's header, gradient statistics against
oracles/gradients.py.

Writes (evidence, coded): facts.csv (every compared field), mismatches.csv (mismatch or absent),
facts_summary.json. Writes (private): reader/<session code>.json (reader records, for r4_objective.py) and
index/<session code>.json (DICOM paths per series, for r4_pixels.py). Prints counts only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from collections import Counter

import r4real

FIELDS = ["session", "series", "series_number", "field", "result", "reader", "select"]


def load_reader():
    path = os.path.join(r4real.ROUND4, "oracles", "header_reader.py")
    spec = importlib.util.spec_from_file_location("r4_header_reader", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["r4_header_reader"] = module
    spec.loader.exec_module(module)
    return module


def software_result(reader_rec, select_rec, values):
    expected = "\\".join(v.strip().upper() for v in (values or []) if v.strip())
    got = select_rec.get("software") or ""
    reader_set = sorted(str(v).strip().upper() for v in (reader_rec.get("software") or []))
    same = reader_set == sorted(x for x in expected.split("\\") if x)
    return {"field": "software", "result": "equal" if got == expected and same else "mismatch",
            "reader": expected, "select": got}


def compare_session(reader, project, sid, label, folder, conv_folder, code, private):
    import nibabel as nib
    from oracles import gradients as grad_oracle

    index = r4real.dicom_index(folder)
    mine = reader.read_session(folder)
    with open(os.path.join(private, "reader", f"{code}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(mine, f, sort_keys=True, default=str)
    with open(os.path.join(private, "index", f"{code}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({k: v["paths"] for k, v in index.items()}, f)
    doc = r4real.cache_doc(project, sid, label)
    theirs = doc["series"]
    rows = []
    for key in sorted(set(mine["series"]) | set(theirs)):
        m, t = mine["series"].get(key), theirs.get(key)
        number = (m or t or {}).get("series_number")
        if m is None or t is None:
            rows.append({"session": code, "series": key[:8], "series_number": number, "field": "<series>",
                         "result": "absent", "reader": json.dumps(m is not None), "select": json.dumps(t is not None)})
            continue
        t = dict(t)
        if t.get("echoes") == []:
            t["echoes"] = None
        for item in reader.compare_series(m, t):
            if item["field"] == "software":
                item = software_result(m, t, index.get(key, {}).get("software"))
            rows.append({"session": code, "series": key[:8], "series_number": number, "field": item["field"],
                         "result": item["result"], "reader": json.dumps(item["reader"], default=str),
                         "select": json.dumps(item["select"], default=str)})
    for rel, vol in r4real.cache_volumes(doc):
        path = os.path.join(project, "Niftis", *rel.split("/"))
        token = os.path.basename(rel).rsplit("_", 1)[-1][:8]
        try:
            dims = [int(x) for x in nib.load(path).header["dim"]]
        except Exception as e:  # noqa: BLE001 - reported, never printed with a path
            rows.append({"session": code, "series": token, "series_number": "", "field": "volume_dims",
                         "result": "unreadable", "reader": type(e).__name__, "select": json.dumps(vol.get("dims"))})
            continue
        rows.append({"session": code, "series": token, "series_number": "", "field": "volume_dims",
                     "result": "equal" if [int(x) for x in vol.get("dims", [])] == dims else "mismatch",
                     "reader": json.dumps(dims), "select": json.dumps(vol.get("dims"))})
        stem = path[:-7]
        if os.path.exists(stem + ".bval") and os.path.exists(stem + ".bvec"):
            want = grad_oracle.stats_from_files(stem + ".bval", stem + ".bvec", dims[4] if dims[0] >= 4 else 1)
            got = vol.get("gradients") or {}
            for k in ("b0", "shells", "b_max", "n_dirs", "mismatch"):
                rows.append({"session": code, "series": token, "series_number": "", "field": f"volume_gradients.{k}",
                             "result": "equal" if got.get(k) == want.get(k) else "mismatch",
                             "reader": json.dumps(want.get(k)), "select": json.dumps(got.get(k))})
    return rows, mine["counts"]


def main(argv=None):
    p = argparse.ArgumentParser(description="R4-VAL-01 header facts against the independent reader")
    p.add_argument("--project", required=True)
    p.add_argument("--private", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--limit", type=int, help="first N sessions only (smoke tests)")
    a = p.parse_args(argv)
    r4real.check_outputs(a.project, a.private, a.evidence)
    codes = r4real.Codes(a.private)
    for sub in ("reader", "index"):
        os.makedirs(os.path.join(a.private, sub), exist_ok=True)
    reader = load_reader()
    sessions = r4real.converted_sessions(a.project)[: a.limit or None]
    all_rows, counts, started = [], Counter(), time.monotonic()
    for i, (sid, label, folder, conv) in enumerate(sessions, 1):
        rows, c = compare_session(reader, a.project, sid, label, folder, conv, codes.session(sid, label), a.private)
        all_rows += rows
        counts.update({k: v for k, v in c.items() if isinstance(v, int)})
        if i % 10 == 0 or i == len(sessions):
            print(f"sessions {i}/{len(sessions)}; rows {len(all_rows)}; {time.monotonic() - started:.0f} s", flush=True)
    r4real.write_csv(os.path.join(a.evidence, "facts.csv"), all_rows, FIELDS)
    bad = [r for r in all_rows if r["result"] in ("mismatch", "absent", "unreadable")]
    r4real.write_csv(os.path.join(a.evidence, "mismatches.csv"), bad, FIELDS)
    results = Counter(r["result"] for r in all_rows)
    by_field = {}
    for r in all_rows:
        by_field.setdefault(r["field"], Counter())[r["result"]] += 1
    summary = {"sessions": len(sessions), "rows": len(all_rows), "results": dict(results),
               "by_field": {k: dict(v) for k, v in sorted(by_field.items())}, "reader_counts": dict(counts),
               "readings": ["software: exact string as the cache joins it (owner, R4-F-010, 2026-11-25)",
                            "echoes: empty list equals absent (5.1 multi-echo only)"]}
    with open(os.path.join(a.evidence, "facts_summary.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, indent=1, sort_keys=True)
    print("results: " + " ".join(f"{k}={v}" for k, v in sorted(results.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
