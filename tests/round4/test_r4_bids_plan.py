"""TG-BPL: the BIDS plan (plan section 5, TG-BPL; R-BID-1 to R-BID-3, R-BID-6 to R-BID-8, R-BID-12).

Exports come from `select --bids --publish` on catalog exams converted with the tier 2 converter
(names and arrangement depend on decisions, not on converter geometry); the plan's two "input"
rows (P08, P18) and R4-BPL-04 call bids.plan_visit, the unit function the group names, on records
built by r4lib.records. Expected values: expected/unit/bids_plan.json, oracles/bids_rules.py (S),
and the BIDS design (P).
"""

from __future__ import annotations

import csv
import fnmatch
import json
import os

import pytest

from oracles import bids_rules
from r4lib import catalog, e2e, records

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
TABLE = records.unit_table("bids_plan.json")
ADDABLE = {"ContrastBolusIngredient", "Units", "IntendedFor"}
FORBIDDEN = {"subject_id", "session", "session_date", "dicom_study_date", "scan_type", "source_archive_sha256",
             "source_series_key", "source_nifti"}
CATALOG_OF = {e["id"]: name for name, data in catalog.load_all().items() for e in data["exams"]}


def data_files(root):
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        if not rel_dir.startswith("sub-"):
            continue
        for f in files:
            if f.startswith(".") or f.endswith(("_scans.tsv", "_scans.json", "_sessions.tsv", "_sessions.json")):
                continue
            out.append(f"{rel_dir}/{f}")
    return sorted(out)


EXPORT_EXAMS = sorted({(c, e) for c, e, _ in e2e.exam_cases({"R-RNK-3", "R-BID-5", "R-BID-2", "R-BID-4", "R-REV-3"})})


@pytest.mark.parametrize("cat_name,exam_id", EXPORT_EXAMS)
def test_r4_bpl_01_every_exported_path_follows_bids(fake_exam, cat_name, exam_id):
    _run, root = fake_exam(cat_name, exam_id).bids()
    bad = {}
    for rel in data_files(root):
        problems, _parsed = bids_rules.check_path(rel)
        if problems:
            bad[rel] = problems
    assert not bad, bad


@pytest.mark.parametrize("subject", TABLE["subject_ids"]["refused"])
def test_r4_bpl_01_subject_ids_with_separators_are_refused(subject):
    from mri_dicom_pipeline import bids

    recs, vols = records.candidate("A", "T1", {}, 3, 0.0)
    session, decisions, fates, _info = records.decide(recs, vols)
    with pytest.raises(bids.PlanError):
        bids.plan_visit(subject, "20261201", session.volumes, fates, decisions, "unknown", lambda v: None)
    assert not bids_rules.valid_subject_label(subject)


# ---------------------------------------------------------------------------- R4-BPL-02

def _ignored(root, rel):
    path = os.path.join(root, ".bidsignore")
    patterns = [l.strip() for l in open(path, encoding="utf-8") if l.strip() and not l.startswith("#")] \
        if os.path.exists(path) else []
    name = rel.split("/")[-1]
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p.replace("**/", "")) for p in patterns)


def _expand(case):
    if "files_pattern" in case:
        base = case["files_pattern"]
        return [base.replace("{1..4}", str(e)).replace("{mag,phase}", p) for e in range(1, 5) for p in ("mag", "phase")]
    return list(case["files"])


EXAM_ROWS = [c for c in TABLE["cases"] if "exam" in c and "override" not in c]


# Rows whose expected file set depends on what the real converter writes (dcm2niix's _ADC companion).
REAL_ROWS = {"P21"}


@pytest.mark.parametrize("case", [pytest.param(c, marks=pytest.mark.r4real) if c["id"] in REAL_ROWS else c
                                  for c in EXAM_ROWS], ids=lambda c: c["id"])
def test_r4_bpl_02_design_table_rows(request, case):
    exam_id = case["exam"][0]
    store = request.getfixturevalue("real_exam" if case["id"] in REAL_ROWS else "fake_exam")
    run, root = store(CATALOG_OF[exam_id], exam_id).bids()
    problems = []
    files = _expand(case)
    if "n_files" in case and len(files) != case["n_files"]:
        problems.append("table pattern size")
    for rel in files:
        if not os.path.exists(os.path.join(root, *rel.split("/"))):
            problems.append(f"missing {rel}")
            continue
        status = "conventional" if _ignored(root, rel) else "standard"
        if rel.endswith(".nii.gz") and status != case["status"]:
            problems.append(f"{rel}: {status}, expected {case['status']}")
        sidecar_path = os.path.join(root, *rel.split("/"))[:-7] + ".json" if rel.endswith(".nii.gz") else None
        adds = dict(case.get("sidecar_adds", {}))
        if "part-phase" in rel:
            adds.update(case.get("phase_sidecar_adds", {}))
        if sidecar_path and adds:
            sidecar = json.load(open(sidecar_path, encoding="utf-8"))
            for k, v in adds.items():
                if sidecar.get(k) != v:
                    problems.append(f"{rel}: {k} {sidecar.get(k)!r}, expected {v!r}")
    if case.get("bidsignore"):
        text = open(os.path.join(root, ".bidsignore"), encoding="utf-8").read()
        if case["bidsignore"] not in text:
            problems.append(f".bidsignore lacks {case['bidsignore']}")
    for sel, reason in case.get("omitted", {}).items():
        rows = e2e.rows_for(run, sel)
        if not rows or any(r["export_status"] != "omitted" or r["export_reason"] != reason for r in rows):
            problems.append(f"{sel}: export {[(r['export_status'], r['export_reason']) for r in rows]}, expected omitted {reason}")
    for sel, reason in case.get("omitted_companions", {}).items():
        manifest = next((json.load(open(os.path.join(d, ".selection.json"), encoding="utf-8"))
                         for d, _s, fs in os.walk(root) if ".selection.json" in fs), {})
        if not any(o["reason"] == reason and o["source"].endswith("_" + sel.split(":")[1].upper() + ".nii.gz")
                   for o in manifest.get("omitted", [])):
            problems.append(f"{sel}: not listed as omitted with {reason}")
    assert not problems, problems


def test_r4_bpl_05_map_without_its_source(tmp_path):
    """P20 (R4-BPL-05): DIF-13's unlinked ADC map chosen by override; its source DWI is not exported
    with it, so its name carries acq-unlinked."""
    case = next(c for c in TABLE["cases"] if c["id"] == "P20")
    conv = e2e.build(tmp_path / "b", "diffusion", "DIF-13-UNLINKED-ADC", "fake")
    probe = conv.select(tmp_path / "probe")
    gid = next(r["derived_group_id"] for r in probe.vrows if r["_uid"] == 500)
    path = tmp_path / "ov.csv"
    path.write_text("subject_id,session,role,action,candidate_id\n"
                    f"S413,20260922,ADC,choose,{gid}\nS413,20260922,DWI,none,\n", encoding="utf-8")
    root = tmp_path / "BIDS"
    run = conv.select(root, "--bids", "--publish", "--overrides", str(path))
    assert run.role_row("ADC")["decided_by"] == "override"
    for rel in case["files"]:
        assert os.path.exists(os.path.join(root, *rel.split("/"))), (rel, data_files(root))


# ---------------------------------------------------------------------------- P08, P18: input rows

def _input_plan(case):
    from mri_dicom_pipeline import bids

    spec = case["input"]
    if spec["role"] == "DWI":
        dwi_recs, dwi_vols = records.candidate("D", "DWI", {"n_dirs": 6}, 5, 0.0)
        for v in dwi_vols:
            v.companions = [{"name": v.relpath.rsplit("/", 1)[-1][:-7] + ".bval", "kind": "bval", "sha256": "0"},
                            {"name": v.relpath.rsplit("/", 1)[-1][:-7] + ".bvec", "kind": "bvec", "sha256": "0"}]
        trace = records.series("T", series_number=500, image_type=spec["image_type"],
                               for_hash=dwi_recs[0]["for_hash"], **dict(records.NEUTRAL_EPI))
        vols = dwi_vols + [records.volume(trace, "DWI", image_type=spec["image_type"])]
        recs = dwi_recs + [trace]
    else:
        rec = records.series("M", series_number=8, **dict(records.NEUTRAL_GR))
        recs, vols = [rec], [records.volume(rec, spec["role"])]
    session, decisions, fates, _info = records.decide(recs, vols)
    from mri_dicom_pipeline import selection, selection_rules

    profile = selection_rules.load_profile()
    chosen = [v for v in session.volumes if selection.copied(v, fates[v], profile)]
    return bids.plan_visit(spec["subject"], spec["session"], chosen, fates, decisions, "unknown", lambda v: None)


@pytest.mark.parametrize("case", [c for c in TABLE["cases"] if "input" in c], ids=lambda c: c["id"])
def test_r4_bpl_02_input_rows(case):
    plan = _input_plan(case)
    spec = case["input"]
    paths = {f"sub-{spec['subject']}/ses-{spec['session']}/{e['path']}" for e in plan if e["status"] != "omitted"}
    for rel in case["files"]:
        assert rel in paths, (rel, sorted(paths))


# ---------------------------------------------------------------------------- R4-BPL-03

@pytest.mark.parametrize("cat_name,exam_id", EXPORT_EXAMS)
def test_r4_bpl_03_sidecars_hold_only_the_allowed_keys(fake_exam, cat_name, exam_id):
    conv = fake_exam(cat_name, exam_id)
    run, root = conv.bids()
    problems = []
    manifests = [json.load(open(os.path.join(d, ".selection.json"), encoding="utf-8"))
                 for d, _s, fs in os.walk(root) if ".selection.json" in fs]
    for manifest in manifests:
        for item in manifest["files"]:
            if not item["path"].endswith(".json"):
                continue
            visit = os.path.join(root, f"sub-{manifest['subject_id']}", f"ses-{manifest['session']}")
            got = json.load(open(os.path.join(visit, *item["path"].split("/")), encoding="utf-8"))
            src = json.load(open(os.path.join(conv.root, "Niftis", *item["source"].split("/")), encoding="utf-8"))
            bad = [k for k in got if k in FORBIDDEN or k.startswith("selection_")]
            added = [k for k in got if k not in src and k not in ADDABLE]
            changed = [k for k in got if k in src and got[k] != src[k]]
            if bad or added or changed:
                problems.append((item["path"], bad, added, changed))
            for key in ("EchoTime", "RepetitionTime", "InversionTime"):
                if isinstance(got.get(key), (int, float)) and got[key] > 30:
                    problems.append((item["path"], f"{key} {got[key]} is not in seconds"))
    if not manifests:
        pytest.skip("no visit of this exam was exported (every role went to review or was not exported)")
    assert not problems, problems


# ---------------------------------------------------------------------------- R4-BPL-04

def test_r4_bpl_04_echo_set_lacking_an_echo_time_is_omitted_whole():
    from mri_dicom_pipeline import bids, selection, selection_rules

    """The set is selected with every echo time present; one echo's sidecar then loses EchoTime before
    the plan, so the plan's own whole-set rule decides (selection would otherwise stop it earlier)."""
    recs, vols = records.candidate("E", "GRE", {"family": "GRE_2D", "n_echoes": 3, "native": [1.0, 1.0, 3.0]}, 4, 0.0)
    session, decisions, fates, _info = records.decide(recs, vols)
    profile = selection_rules.load_profile()
    chosen = [v for v in session.volumes if selection.copied(v, fates[v], profile)]
    assert len(chosen) == 3, [fates[v] for v in session.volumes]
    chosen[1].sidecar.pop("EchoTime", None)
    plan = bids.plan_visit("S900", "20261201", chosen, fates, decisions, "unknown", lambda v: None)
    assert plan and all(e["status"] == "omitted" for e in plan), [(e["status"], e["reason"]) for e in plan]


def test_r4_bpl_04_phase_only_and_magnitude_phase():
    from mri_dicom_pipeline import bids, selection, selection_rules

    recs, vols = records.candidate("W", "SWI", {"family": "SWI_3D", "has_phase": True}, 10, 0.0)
    session, decisions, fates, _info = records.decide(recs, vols)
    profile = selection_rules.load_profile()
    chosen = [v for v in session.volumes if selection.copied(v, fates[v], profile)]
    plan = bids.plan_visit("S900", "20261201", chosen, fates, decisions, "unknown", lambda v: None)
    names = sorted(e["path"] for e in plan if e["status"] != "omitted")
    assert any("part-mag" in n for n in names) and any("part-phase" in n for n in names), names
    for e in plan:
        if "part-phase" in e.get("path", ""):
            assert e["extra"].get("Units") == "arbitrary", e["extra"]


# ---------------------------------------------------------------------------- R4-BPL-06

def _read_tsv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def test_r4_bpl_06_root_marker_and_contrast_agent(tmp_path):
    conv = e2e.build(tmp_path / "b", "structural", "STR-04-GE-SESSION-F-CONTRAST", "fake")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "x.txt").write_text("not a BIDS export", encoding="utf-8")
    assert conv.select(foreign, "--bids", "--publish").code == 2
    root = tmp_path / "BIDS"
    first = conv.select(root, "--bids", "--publish")
    assert first.code == 0, first.result.stderr[-800:]
    anat = os.path.join(root, "sub-S104", "ses-20260803", "anat")
    assert any("ce-unknown" in f for f in os.listdir(anat))
    assert conv.select(root, "--publish").code == 2, "the default layout must refuse a BIDS root"
    second = conv.select(root, "--bids", "--publish", "--bids-contrast-agent", "gadolinium")
    assert second.code == 0
    names = os.listdir(anat)
    assert not any("ce-unknown" in f for f in names), names
    post = [f for f in names if "ce-gadolinium" in f and f.endswith(".json")]
    assert post and all(json.load(open(os.path.join(anat, f)))["ContrastBolusIngredient"] == "GADOLINIUM" for f in post)
    scans = _read_tsv(os.path.join(root, "sub-S104", "ses-20260803", "sub-S104_ses-20260803_scans.tsv"))
    assert any("ce-gadolinium" in r["filename"] for r in scans) and not any("ce-unknown" in r["filename"] for r in scans)
    assert json.load(open(os.path.join(root, ".bids_export.json")))["contrast_agent"] == "gadolinium"
    third = conv.select(root, "--bids", "--publish")
    assert third.code == 0 and any("ce-gadolinium" in f for f in os.listdir(anat)), "the recorded agent is reused"


# ---------------------------------------------------------------------------- R4-BPL-07

@pytest.mark.parametrize("cat_name,exam_id", [("structural", "STR-04-GE-SESSION-F-CONTRAST"),
                                              ("susceptibility", "SUS-01-SIEMENS-SWI-BUNDLE"),
                                              ("diffusion", "DIF-03-REVERSE-PE-B0")])
def test_r4_bpl_07_bids_changes_names_not_choices(fake_exam, tmp_path, cat_name, exam_id):
    conv = fake_exam(cat_name, exam_id)
    default = conv.select(tmp_path / "d")
    with_bids = conv.select(tmp_path / "b", "--bids")
    a = {r["role"]: (r["outcome"], r["candidate_id"]) for r in default.rows}
    b = {r["role"]: (r["outcome"], r["candidate_id"]) for r in with_bids.rows}
    assert a == b


# Added in phase 2 after TG-SEED (fault F-BPL-1, acq and ce swapped in the entity order, survived: no
# catalog plan names a file with both). bids._name builds every planned file name (plan_visit); a name
# holding acq, ce, run, echo and part must follow the BIDS 1.11.1 entity order (oracles/bids_rules.py, S).
def test_r4_bpl_01_entity_order_of_a_full_name():
    from mri_dicom_pipeline import bids

    name = bids._name({"entities": {"part": "mag", "echo": 1, "run": 2, "ce": "gadolinium", "acq": "mprage"},
                       "suffix": "T1w"})
    problems, _parsed = bids_rules.check_path(f"sub-S900/ses-20261201/anat/sub-S900_ses-20261201{name}.nii.gz")
    assert not problems, (name, problems)
