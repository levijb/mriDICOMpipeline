"""TG-BEX: BIDS export and tools (plan section 5, TG-BEX; R-BID-4, R-BID-5, R-BID-9, R-BID-10).

Tier 3: catalog exams converted with the real dcm2niix and exported with `select --bids --publish`.
Expected values: the catalog's R-BID keys, BIDS (S), the BIDS design 4.3, 5.3 and 6 (P); NiBabel,
the BIDS validator and PyBIDS as independent tools (T).
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess

import pytest

from oracles import geometry
from r4lib import catalog, e2e
from r4lib.project import R4Project

pytestmark = [pytest.mark.r4real, pytest.mark.filterwarnings("ignore::DeprecationWarning")]
CATALOG_OF = {e["id"]: name for name, data in catalog.load_all().items() for e in data["exams"]}
DWI_EXAMS = sorted({(c, e) for c, e, _ in e2e.exam_cases({"R-BID-5", "R-BID-4", "R-IDN-7"})})
# bids-output-research.md 5.3: the block lists selection.csv, selection_volumes.csv and the three
# conventional suffix patterns.
IGNORE_BLOCK = {"selection.csv", "selection_volumes.csv", "**/*_swi.*", "**/*_minIP.*", "**/*_QSM.*"}


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def visit_folder(root, exam):
    subject = exam["roster_rows"][0][0]
    return os.path.join(root, f"sub-{subject}", f"ses-{exam['sessions'][0]['label'][:8]}")


def manifest(visit):
    path = os.path.join(visit, ".selection.json")
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {"files": [], "omitted": []}


# ---------------------------------------------------------------------------- R4-BEX-01

@pytest.mark.parametrize("cat_name,exam_id", DWI_EXAMS)
def test_r4_bex_01_gradient_files_match_the_image(real_exam, cat_name, exam_id):
    import nibabel as nib

    conv = real_exam(cat_name, exam_id)
    _run, root = conv.bids()
    visit = visit_folder(root, conv.exam)
    m = manifest(visit)
    problems = []
    for item in m["files"]:
        if item["path"].endswith("_dwi.nii.gz"):
            stem = os.path.join(visit, *item["path"].split("/"))[:-7]
            n4 = nib.load(stem + ".nii.gz").shape
            n4 = n4[3] if len(n4) > 3 else 1
            bvals = open(stem + ".bval").read().split()
            rows = [r.split() for r in open(stem + ".bvec").read().strip().splitlines()]
            if len(bvals) != n4 or len(rows) != 3 or any(len(r) != n4 for r in rows):
                problems.append(f"{item['path']}: {len(bvals)} b-values, bvec {[len(r) for r in rows]}, 4th dim {n4}")
        if item["path"].endswith((".bval", ".bvec")):
            src = os.path.join(conv.root, "Niftis", *item["source"].split("/"))
            if sha(os.path.join(visit, *item["path"].split("/"))) != sha(src):
                problems.append(f"{item['path']}: not a byte copy")
    assert not problems, problems


# ---------------------------------------------------------------------------- R4-BEX-02

def _pe_axis(direction):
    return {"i": 0, "j": 1, "k": 2}[direction[0]]


def _sidecars(root):
    for dirpath, _d, files in os.walk(root):
        for f in files:
            if f.endswith(".json") and not f.startswith("."):
                path = os.path.join(dirpath, f)
                try:
                    yield path, json.load(open(path, encoding="utf-8"))
                except ValueError:
                    pass


def _dangling(root):
    out = []
    for path, data in _sidecars(root):
        for uri in data.get("IntendedFor", []) if isinstance(data, dict) else []:
            rel = uri[len("bids::"):] if uri.startswith("bids::") else uri
            if not os.path.exists(os.path.join(root, *rel.split("/"))):
                out.append((os.path.relpath(path, root), uri))
    return out


@pytest.mark.parametrize("cat_name,exam_id", sorted({(c, e) for c, e, _ in e2e.exam_cases({"R-BID-5"})}))
def test_r4_bex_02_field_maps(real_exam, cat_name, exam_id):
    import nibabel as nib

    conv = real_exam(cat_name, exam_id)
    run, root = conv.bids()
    want = conv.exam["expected"]["R-BID-5"]["outcome"]
    visit = visit_folder(root, conv.exam)
    problems = []
    fmap_dir = os.path.join(visit, "fmap")
    fmaps = sorted(f for f in os.listdir(fmap_dir) if f.endswith(".nii.gz")) if os.path.isdir(fmap_dir) else []
    if want.get("fmap") is None:
        if fmaps:
            problems.append(f"field maps exported: {fmaps}")
    else:
        path = os.path.join(visit, *want["fmap"].split("/"))
        if not os.path.exists(path):
            problems.append(f"missing {want['fmap']} (have {fmaps})")
        else:
            side = json.load(open(path[:-7] + ".json", encoding="utf-8"))
            for uri in want.get("intended_for", []):
                if uri not in side.get("IntendedFor", []):
                    problems.append(f"IntendedFor {side.get('IntendedFor')} lacks {uri}")
            exp_side = want.get("fmap_sidecar", {})
            if "PhaseEncodingDirection" in exp_side and side.get("PhaseEncodingDirection") != exp_side["PhaseEncodingDirection"]:
                problems.append(f"PhaseEncodingDirection {side.get('PhaseEncodingDirection')!r}")
            if exp_side.get("TotalReadoutTime_positive") and not (side.get("TotalReadoutTime") or 0) > 0:
                problems.append(f"TotalReadoutTime {side.get('TotalReadoutTime')!r}")
            if want.get("partner_bval_exported") is False and os.path.exists(path[:-7] + ".bval"):
                problems.append("the partner's .bval was exported")
            if want.get("phase_axis_same_image_axis_in_both"):
                dwi_rel = side["IntendedFor"][0][len("bids::"):]
                dwi_path = os.path.join(root, *dwi_rel.split("/"))
                dwi_side = json.load(open(dwi_path[:-7] + ".json", encoding="utf-8"))
                a = geometry.axis_directions(nib.load(path).affine)[_pe_axis(side["PhaseEncodingDirection"])]
                b = geometry.axis_directions(nib.load(dwi_path).affine)[_pe_axis(dwi_side["PhaseEncodingDirection"])]
                if abs(sum(x * y for x, y in zip(a, b))) < 0.99:
                    problems.append(f"phase-encoding axes differ in world space: {a} vs {b}")
    for sel, reason in want.get("omitted", {}).items():
        rows = e2e.rows_for(run, sel)
        if not rows or any((r["export_status"], r["export_reason"]) != ("omitted", reason) for r in rows):
            problems.append(f"{sel}: {[(r['export_status'], r['export_reason']) for r in rows]}, expected omitted {reason}")
    if want.get("dangling_intended_for") is False or "fmap" in want:
        problems += [f"dangling IntendedFor {d}" for d in _dangling(root)]
    if "dwi" in want and want["dwi"] is None and os.path.isdir(os.path.join(visit, "dwi")):
        problems.append("a dwi folder was exported")
    for rel in want.get("exported_anat", []):
        if not os.path.exists(os.path.join(visit, *rel.split("/"))):
            problems.append(f"missing {rel}")
    if want.get("intended_for_anywhere") is False:
        found = [p for p, d in _sidecars(root) if isinstance(d, dict) and "IntendedFor" in d]
        if found:
            problems.append(f"IntendedFor in {found}")
    if "R-BID-5-DIR" in conv.exam["expected"]:
        for f in fmaps:
            if "_dir-" not in f:
                problems.append(f"{f}: no dir entity")
    e2e.record("bids_fieldmaps", {"exam": exam_id, "fmaps": fmaps, "problems": problems})
    assert not problems, problems


# ---------------------------------------------------------------------------- R4-BEX-03

def _tsv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def _write_tsv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def test_r4_bex_03_user_rows_columns_and_edits_are_kept(tmp_path, r4_dcm2niix):
    a = next(e for e in catalog.load_file("structural")["exams"] if e["id"] == "STR-01-GE-PREMIER-CENSUS")
    b = next(e for e in catalog.load_file("structural")["exams"] if e["id"] == "STR-03-SIEMENS-ESPREE-2D")
    project = R4Project(tmp_path / "project")
    project.write_roster(a["roster_rows"] + b["roster_rows"])
    for exam in (a, b):
        for s in exam["sessions"]:
            project.place(exam, s)
    assert project.run("prepare").code == 0
    assert project.run("convert", "--dcm2niix-bin", r4_dcm2niix).code == 0
    subjects = tmp_path / "s101.csv"
    subjects.write_text("subject_id\nS101\n", encoding="utf-8")
    root = tmp_path / "BIDS"
    assert project.run("select", "--output-root", str(root), "--bids", "--publish", "--subjects", str(subjects)).code == 0
    part = os.path.join(root, "participants.tsv")
    rows = _tsv(part)
    fields = list(rows[0]) + ["age"]
    for r in rows:
        r["age"] = "42"
    rows.append({k: "" for k in fields} | {"participant_id": "sub-X99", "age": "7"})
    _write_tsv(part, rows, fields)
    sess = os.path.join(root, "sub-S101", "sub-S101_sessions.tsv")
    srows = _tsv(sess)
    sfields = list(srows[0]) + ["note"]
    for r in srows:
        r["note"] = "kept"
    _write_tsv(sess, srows, sfields)
    with open(os.path.join(root, "README.md"), "a", encoding="utf-8") as f:
        f.write("\nA line the user added.\n")
    assert project.run("select", "--output-root", str(root), "--bids", "--publish").code == 0
    rows = {r["participant_id"]: r for r in _tsv(part)}
    assert rows.get("sub-S101", {}).get("age") == "42", rows
    assert "sub-X99" in rows and rows["sub-X99"].get("age") == "7", rows
    assert "sub-S103" in rows, rows
    assert all(r.get("note") == "kept" for r in _tsv(sess))
    assert "A line the user added." in open(os.path.join(root, "README.md"), encoding="utf-8").read()


# ---------------------------------------------------------------------------- R4-BEX-04 / 05

VALIDATED = [("structural", "STR-04-GE-SESSION-F-CONTRAST"), ("diffusion", "DIF-01-GE-TENSOR-ADC-EADC"),
             ("diffusion", "DIF-03-REVERSE-PE-B0"), ("susceptibility", "SUS-01-SIEMENS-SWI-BUNDLE"),
             ("susceptibility", "SUS-03-QSM-MULTI-ECHO"), ("susceptibility", "SUS-04-MULTI-ECHO-GRE"),
             ("kept_roles", "KR-01-TOF-MIP-LOCALIZER"), ("visits", "VIS-01-TWO-SESSIONS")]


@pytest.mark.r4tool
@pytest.mark.parametrize("cat_name,exam_id", VALIDATED)
def test_r4_bex_04_validator_zero_errors(real_exam, r4_tools, cat_name, exam_id):
    conv = real_exam(cat_name, exam_id)
    _run, root = conv.bids()
    result = subprocess.run([r4_tools["validator"], root, "--format", "json"], capture_output=True, text=True,
                            timeout=600)
    report = json.loads(result.stdout)
    issues = report["issues"]["issues"]
    errors = [i for i in issues if i.get("severity") == "error"]
    e2e.record("bids_validator", {"exam": exam_id, "errors": len(errors),
                                  "codes": sorted({i.get("code") for i in issues})})
    assert not errors, json.dumps(errors, indent=1)[:4000]
    lines = open(os.path.join(root, ".bidsignore"), encoding="utf-8").read().splitlines()
    block = {l.strip() for l in lines if l.strip() and not l.startswith("#")}
    assert block == IGNORE_BLOCK, block


@pytest.mark.r4tool
def test_r4_bex_05_pybids_t1w_query_and_filter(real_exam, r4_tools):
    from bids.layout import BIDSLayout, Query

    conv = real_exam("structural", "STR-04-GE-SESSION-F-CONTRAST")
    _run, root = conv.bids()
    layout = BIDSLayout(root, validate=False)
    query = {"datatype": "anat", "suffix": "T1w", "part": ["mag", None], "extension": [".nii", ".nii.gz"]}
    found = sorted(os.path.basename(f) for f in layout.get(return_type="file", **query))
    assert found == ["sub-S104_ses-20260803_T1w.nii.gz", "sub-S104_ses-20260803_ce-unknown_T1w.nii.gz"], found
    filters = json.load(open(os.path.join(root, "code", "bids_filter_precontrast.json"), encoding="utf-8"))["t1w"]
    query.update({k: Query.NONE if v is None else v for k, v in filters.items()})
    found = sorted(os.path.basename(f) for f in layout.get(return_type="file", **query))
    assert found == ["sub-S104_ses-20260803_T1w.nii.gz"], found
