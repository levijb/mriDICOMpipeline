"""R4-VAL-05 (report only): pixel sampling, series by series, over the converted sessions (spec 8.2; plan 3.6; D-2).

    python tests/round4/realdata/r4_pixels.py --project D:\\mri_project --private D:\\round4\\private \\
        --evidence D:\\round4\\evidence\\realdata

For each series with published NIfTI volumes, oracles/pixel_sampler.sample_series (frozen at G1) draws about 20
stored DICOM samples and looks each one up in the NIfTI at that sample's scanner coordinates. DICOM paths per series come from
<private>/index/<session code>.json (r4_facts.py). A series whose images hold several parts (magnitude, phase,
real, imaginary: the header reader's `parts`) is split by part, DICOM instances by their ImageType tokens,
Siemens ImageTypeText or GE (0043,102F), NIfTI volumes by the part token dcm2niix writes in the sidecar
ImageType; each part is sampled on its own. Echoes split into files are passed with their EchoTime.

Results per session go to <private>/pixels/<session code>.json (so an interrupted run resumes); the combined,
coded pixels.csv goes to the evidence folder. Results are reported and gate nothing; mismatches go to the owner
as findings. Prints counts only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter

import r4real

PART_TOKENS = {"M": "mag", "MAGNITUDE": "mag", "P": "phase", "PHASE": "phase", "R": "real", "REAL": "real",
               "I": "imag", "IMAGINARY": "imag"}
GE_PART = {0: "mag", 1: "phase", 2: "real", 3: "imag"}
FIELDS = ["session", "series", "series_number", "part", "niftis", "status", "reason", "kept", "mismatches", "detail"]


def instance_part(path):
    import pydicom

    ds = pydicom.dcmread(path, stop_before_pixels=True)
    tokens = [str(t).upper() for t in (ds.get("ImageType") or [])]
    text = ds.get((0x0021, 0x1175))
    if text is not None:
        value = text.value
        tokens += [str(t).upper() for t in (value if isinstance(value, (list, tuple)) or value.__class__.__name__ ==
                                            "MultiValue" else str(value).split("\\"))]
    parts = {PART_TOKENS[t] for t in tokens if t in PART_TOKENS}
    if len(parts) == 1:
        return parts.pop()
    ge = ds.get((0x0043, 0x102F))
    if ge is not None:
        value = ge.value
        if isinstance(value, bytes):
            value = int.from_bytes(value[:2], "little", signed=True)
        try:
            return GE_PART.get(int(value[0] if isinstance(value, (list, tuple)) else value), "mag")
        except (TypeError, ValueError):
            pass
    return "mag"


def nifti_part(sidecar):
    tokens = [str(t).upper() for t in (sidecar.get("ImageType") or [])]
    parts = {PART_TOKENS[t] for t in tokens if t in PART_TOKENS}
    return parts.pop() if len(parts) == 1 else "mag"


def session_rows(project, private, sid, label, code):
    from oracles import pixel_sampler

    with open(os.path.join(private, "index", f"{code}.json"), encoding="utf-8") as f:
        index = json.load(f)
    with open(os.path.join(private, "reader", f"{code}.json"), encoding="utf-8") as f:
        reader = json.load(f)["series"]
    doc = r4real.cache_doc(project, sid, label)
    by_series = {}
    for rel, vol in r4real.cache_volumes(doc):
        side = r4real.sidecar(project, rel)
        key = side.get("source_series_key")
        te = side.get("EchoTime")
        by_series.setdefault(key, []).append({"path": os.path.join(project, "Niftis", *rel.split("/")),
                                              "echo_ms": None if te is None else float(te) * 1000.0,
                                              "part": nifti_part(side)})
    rows = []
    for key, niftis in sorted(by_series.items(), key=lambda kv: str(kv[0])):
        rec = reader.get(key) or {}
        number = rec.get("series_number", "")
        paths = index.get(key)
        if not paths:
            rows.append({"session": code, "series": str(key)[:8], "series_number": number, "part": "",
                         "niftis": len(niftis), "status": "not compared", "reason": "no DICOM instances indexed"})
            continue
        multi_part = len([p for p, n in (rec.get("parts") or {}).items() if n]) > 1
        groups = {}
        if multi_part:
            for path in paths:
                groups.setdefault(instance_part(path), []).append(path)
        else:
            groups[next(iter({n["part"] for n in niftis})) if len({n["part"] for n in niftis}) == 1 else "mag"] = paths
        for part, part_paths in sorted(groups.items()):
            part_niftis = [n for n in niftis if n["part"] == part] if multi_part else niftis
            if not part_niftis:
                rows.append({"session": code, "series": str(key)[:8], "series_number": number, "part": part,
                             "niftis": 0, "status": "not compared", "reason": "no NIfTI volume for this part"})
                continue
            entries = [{"path": n["path"], "echo_ms": n["echo_ms"] if len(part_niftis) > 1 else None}
                       for n in part_niftis]
            try:
                res = pixel_sampler.sample_series(sorted(part_paths), entries, f"{key}-{part}")
            except Exception as e:  # noqa: BLE001 - reported per series
                res = {"status": "not compared", "reason": f"error: {type(e).__name__}"}
            rows.append({"session": code, "series": str(key)[:8], "series_number": number, "part": part,
                         "niftis": len(part_niftis), "status": res.get("status"), "reason": res.get("reason", ""),
                         "kept": res.get("n_kept", ""), "mismatches": res.get("n_mismatch", ""),
                         "detail": json.dumps(res.get("first_mismatches") or res.get("groups") or "", default=str)})
    return rows


def main(argv=None):
    p = argparse.ArgumentParser(description="R4-VAL-05 pixel sampling (report only)")
    p.add_argument("--project", required=True)
    p.add_argument("--private", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--limit", type=int)
    a = p.parse_args(argv)
    r4real.check_outputs(a.project, a.private, a.evidence)
    codes = r4real.Codes(a.private)
    os.makedirs(os.path.join(a.private, "pixels"), exist_ok=True)
    sessions = r4real.converted_sessions(a.project)[: a.limit or None]
    started, all_rows = time.monotonic(), []
    for i, (sid, label, _folder, _conv) in enumerate(sessions, 1):
        code = codes.session(sid, label)
        done = os.path.join(a.private, "pixels", f"{code}.json")
        if os.path.exists(done):
            rows = json.load(open(done, encoding="utf-8"))
        else:
            rows = session_rows(a.project, a.private, sid, label, code)
            with open(done, "w", encoding="utf-8", newline="\n") as f:
                json.dump(rows, f)
        all_rows += rows
        if i % 5 == 0 or i == len(sessions):
            print(f"sessions {i}/{len(sessions)}; series parts {len(all_rows)}; {time.monotonic() - started:.0f} s",
                  flush=True)
    r4real.write_csv(os.path.join(a.evidence, "pixels.csv"), all_rows, FIELDS)
    print("status: " + " ".join(f"{k}={v}" for k, v in sorted(Counter(r["status"] for r in all_rows).items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
