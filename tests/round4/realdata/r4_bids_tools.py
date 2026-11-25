"""R4-VAL-06: the BIDS validator, PyBIDS and NiBabel on the BIDS dataset that run C wrote (spec 8.4).

    python tests/round4/realdata/r4_bids_tools.py --bids D:\\round4\\BIDS --private D:\\round4\\private \\
        --evidence D:\\round4\\evidence\\realdata [--validator bids-validator-deno]

*   Validator (bids-validator-deno 3.0.2): zero errors expected. The full JSON report names files, so it stays in
    the private folder; evidence gets counts by severity and code, with paths coded.
*   PyBIDS (0.22.0): per session holding both a pre- and a post-contrast T1w, the default T1w query (anat, T1w,
    part mag or none) must return both, and with code/bids_filter_precontrast.json only the pre-contrast one.
*   NiBabel: every image loads; every dwi's .bval count and .bvec columns equal its 4th dimension; every field
    map's IntendedFor resolves, and its grid matches the target's (oracles/geometry.same_grid).
Only reads the BIDS root. Writes bids_tools.json (evidence, coded) and validator_full.json (private).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter

import r4real


def coder(codes):
    pairs = sorted(codes.sessions.items(), key=lambda kv: -len(kv[0]))

    def code(text):
        out = str(text)
        for key, c in pairs:
            sid, label = key.split("/")
            out = out.replace(f"sub-{sid}_ses-{label}", c).replace(f"sub-{sid}/ses-{label}", c)
        for sid, c in codes.subjects.items():
            out = re.sub(rf"sub-{re.escape(sid)}(?![A-Za-z0-9])", f"sub-{c}", out)
        return re.sub(r"(?<!\d)(19|20)\d{6}(?!\d)", "<DATE>", out)
    return code


def main(argv=None):
    import nibabel as nib
    from oracles import geometry

    p = argparse.ArgumentParser(description="R4-VAL-06 BIDS tools on the real export")
    p.add_argument("--bids", required=True)
    p.add_argument("--private", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--validator", default=os.environ.get("MRI_PIPELINE_BIDS_VALIDATOR")
                   or shutil.which("bids-validator-deno"))
    a = p.parse_args(argv)
    code = coder(r4real.Codes(a.private))
    result = {}
    # Validator.
    proc = subprocess.run([a.validator, a.bids, "--format", "json"], capture_output=True, text=True, timeout=7200)
    with open(os.path.join(a.private, "validator_full.json"), "w", encoding="utf-8", newline="\n") as f:
        f.write(proc.stdout)
    report = json.loads(proc.stdout)
    issues = report["issues"]["issues"]
    result["validator"] = {
        "exit": proc.returncode, "severity": dict(Counter(i.get("severity") for i in issues)),
        "codes": dict(Counter(f"{i.get('severity')}:{i.get('code')}" for i in issues)),
        "errors": [{"code": i.get("code"), "location": code(i.get("location", "")), "sub": code(i.get("subCode", ""))}
                   for i in issues if i.get("severity") == "error"][:200]}
    # PyBIDS.
    from bids.layout import BIDSLayout, Query

    layout = BIDSLayout(a.bids, validate=False)
    with open(os.path.join(a.bids, "code", "bids_filter_precontrast.json"), encoding="utf-8") as f:
        filters = json.load(f)["t1w"]
    query = {"datatype": "anat", "suffix": "T1w", "part": ["mag", None], "extension": [".nii", ".nii.gz"]}
    checked, bad = 0, []
    for sub in layout.get_subjects():
        for ses in layout.get_sessions(subject=sub):
            files = [os.path.basename(x) for x in layout.get(return_type="file", subject=sub, session=ses, **query)]
            if not any("_ce-" in x for x in files) or not any("_ce-" not in x for x in files):
                continue
            checked += 1
            filtered = dict(query, **{k: Query.NONE if v is None else v for k, v in filters.items()})
            kept = [os.path.basename(x) for x in layout.get(return_type="file", subject=sub, session=ses, **filtered)]
            if len(files) < 2 or not kept or any("_ce-" in x for x in kept):
                bad.append(code(f"sub-{sub}_ses-{ses}"))
    result["pybids"] = {"sessions_with_pre_and_post_t1w": checked, "failing": bad}
    # NiBabel.
    images = sorted(glob.glob(os.path.join(a.bids, "sub-*", "ses-*", "*", "*.nii.gz")))
    unreadable, grad_bad, fmap_bad, n_dwi, n_fmap = [], [], [], 0, 0
    for path in images:
        rel = os.path.relpath(path, a.bids).replace(os.sep, "/")
        try:
            img = nib.load(path)
            shape = img.shape
        except Exception as e:  # noqa: BLE001
            unreadable.append((code(rel), type(e).__name__))
            continue
        stem = path[:-7]
        if path.endswith("_dwi.nii.gz"):
            n_dwi += 1
            n4 = shape[3] if len(shape) > 3 else 1
            bvals = open(stem + ".bval").read().split()
            bvec = [r.split() for r in open(stem + ".bvec").read().strip().splitlines()]
            if len(bvals) != n4 or len(bvec) != 3 or any(len(r) != n4 for r in bvec):
                grad_bad.append(code(rel))
        if path.endswith("_epi.nii.gz"):
            n_fmap += 1
            side = json.load(open(stem + ".json", encoding="utf-8"))
            for uri in side.get("IntendedFor", []):
                target = os.path.join(a.bids, *uri.replace("bids::", "").split("/"))
                if not os.path.exists(target):
                    fmap_bad.append((code(rel), "IntendedFor does not resolve"))
                    continue
                t = nib.load(target)
                same, _detail = geometry.same_grid(shape[:3], img.affine, t.shape[:3], t.affine)
                if not same:
                    fmap_bad.append((code(rel), "grid differs from its target"))
    result["nibabel"] = {"images": len(images), "unreadable": unreadable, "dwi": n_dwi, "dwi_gradient_mismatch": grad_bad,
                         "fieldmaps": n_fmap, "fieldmap_problems": fmap_bad}
    with open(os.path.join(a.evidence, "bids_tools.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, indent=1, sort_keys=True, default=str)
    v = result["validator"]["severity"]
    print(f"validator errors {v.get('error', 0)} warnings {v.get('warning', 0)}; pybids sessions {checked} failing "
          f"{len(bad)}; images {len(images)} unreadable {len(unreadable)}; dwi {n_dwi} gradient mismatches "
          f"{len(grad_bad)}; field maps {n_fmap} problems {len(fmap_bad)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
