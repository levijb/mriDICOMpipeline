"""TG-XPL (automated part): concurrency, crash safety and Windows file-system behavior (plan section 5,
TG-XPL; R-XPL-2 to R-XPL-5). R4-XPL-01 (the whole suite on both pydicom ends) is a suite run recorded
in R4_GATES.md; R4-XPL-06 to 09 are manual (tests/round4/manual/WINDOWS_CHECKS.md).

Tier 2 projects. Expected values: README (locks, exit codes, crash recovery), production-hardening
Q2 and invariant 5 (P).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading

import pytest

from r4lib import catalog, crash, e2e
from r4lib.project import R4Project

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

HOLDER = r'''
import sys
from pathlib import Path
from mri_dicom_pipeline import locks
roots = [Path(p) for p in sys.argv[2:]]
with locks.RootLocks(roots, sys.argv[1], "r4-holder"):
    print("held", flush=True)
    sys.stdin.read()
'''


def _hold(command, roots):
    proc = subprocess.Popen([sys.executable, "-c", HOLDER, command, *map(str, roots)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def _release(proc):
    proc.stdin.close()
    proc.wait(timeout=60)


# ---------------------------------------------------------------------------- R4-XPL-02

@pytest.mark.parametrize("holder", ["convert", "select", "prepare"])
def test_r4_xpl_02_commands_sharing_a_folder_exclude_each_other(fake_exam, tmp_path, holder):
    conv = fake_exam("structural", "STR-03-SIEMENS-ESPREE-2D")
    root = conv.root
    out = tmp_path / "Selected"
    roots = {"convert": [f"{root}/DicomExtracted", f"{root}/Niftis", f"{root}/Reports", f"{root}/.work"],
             "select": [f"{root}/DicomExtracted", f"{root}/Niftis", f"{root}/Reports", str(out)],
             "prepare": [f"{root}/DicomUploads", f"{root}/DicomExtracted", f"{root}/Reports", f"{root}/.work"]}[holder]
    proc = _hold(holder, roots)
    try:
        run = conv.select(out)
        dry = conv.select(out, "--dry-run")
    finally:
        _release(proc)
    text = run.result.stdout + run.result.stderr
    assert run.code == 2 and "locked by" in text and holder in text, text[-600:]
    assert dry.code == 0, (dry.code, dry.result.stderr[-600:])


# ---------------------------------------------------------------------------- R4-XPL-03

def _naming_project(tmp_path):
    exam = next(e for e in catalog.load_file("naming")["exams"] if e["id"] == "NAM-06-TWO-STUDIES-ONE-DATE")
    project = R4Project(tmp_path / "project")
    project.place_exam(exam)
    return project, exam


@pytest.mark.parametrize("step", ["zip-renamed", "rename-logged", "naming-cache-saved"])
def test_r4_xpl_03_naming_crash_then_plain_prepare(tmp_path, step):
    project, exam = _naming_project(tmp_path)
    markers = tmp_path / "markers"
    result = crash.run_with_crash(["prepare", "--project-root", project.root], step, str(markers), cwd=project.root)
    if not crash.fired(str(markers), step):
        pytest.fail(f"ERROR, not a finding: {step} never fired (exit {result.returncode})")
    final = project.run("prepare")
    rows = project.read_csv("Reports", "renamed_uploads.csv")
    olds = [r["old_path"] for r in rows]
    want = {s["upload_path"] for s in exam["sessions"]}
    zips = [os.path.join(d, f) for d, _s, fs in os.walk(os.path.join(project.root, "DicomUploads"))
            for f in fs if f.lower().endswith(".zip")]
    cache = os.path.join(project.root, "Reports", ".naming_cache.json")
    problems = []
    if sorted(olds) != sorted(want):
        problems.append(f"log rows for {sorted(olds)}, expected each of {sorted(want)} once")
    if len(zips) != len(exam["sessions"]):
        problems.append(f"{len(zips)} ZIPs, expected {len(exam['sessions'])}")
    if os.path.exists(cache):
        try:
            json.load(open(cache, encoding="utf-8"))
        except ValueError:
            problems.append("naming cache unreadable")
    e2e.record("naming_crash", {"step": step, "final_exit": final.code, "problems": problems})
    assert not problems, problems


# ---------------------------------------------------------------------------- R4-XPL-04

def test_r4_xpl_04_plan_refuses_names_differing_only_in_case(monkeypatch):
    """The plan's name-clash check ignores letter case (Windows file names): two outputs whose names
    differ only in case refuse the visit. The names are forced through bids._name, since no real
    combination of roles produces such a pair."""
    from mri_dicom_pipeline import bids
    from r4lib import records

    ra, va = records.candidate("A", "T1", {}, 3, 0.0)
    rb, vb = records.candidate("B", "T2", {"family": "T2_2D_TSE", "native": [1.0, 1.0, 3.0]}, 5, 600.0)
    session, decisions, fates, _info = records.decide(ra + rb, va + vb)
    chosen = [v for v in session.volumes if fates[v]["fate"] == "selected"]
    assert len(chosen) == 2
    names = iter(["_acq-X_T1w", "_acq-x_T1w"])

    def forced(e):
        if "_forced" not in e:
            e["_forced"] = next(names)
        return e["_forced"]

    monkeypatch.setattr(bids, "_name", forced)
    with pytest.raises(bids.PlanError):
        bids.plan_visit("S900", "20261201", chosen, fates, decisions, "unknown", lambda v: None)


@pytest.mark.skipif(os.name != "nt", reason="the 260-character limit is a Windows limit")
def test_r4_xpl_04_long_paths(tmp_path):
    deep = tmp_path
    while len(str(deep)) < 270:
        deep = deep / ("long_folder_name_" + "x" * 20)
    exam = next(e for e in catalog.load_file("structural")["exams"] if e["id"] == "STR-03-SIEMENS-ESPREE-2D")
    project = R4Project(deep / "project")
    project.place_exam(exam)
    short = str(tmp_path)
    assert len(project.root) > 260
    assert project.run("prepare", cwd=short).code == 0
    args, env = e2e.converter_args(tmp_path, exam, "fake")
    assert project.run("convert", *args, env=env, cwd=short).code == 0
    out = str(deep / "Selected")
    run = project.run("select", "--output-root", out, "--publish", cwd=short)
    assert run.code == 0, run.stderr[-800:]
    manifest = None
    for dirpath, _d, files in os.walk(out):
        if "selection.json" in files:
            manifest = json.load(open(os.path.join(dirpath, "selection.json"), encoding="utf-8"))
            for item in manifest["files"]:
                assert os.path.exists(os.path.join(dirpath, *item["path"].split("/"))), item["path"]
    assert manifest is not None


@pytest.mark.skipif(os.name != "nt", reason="an open file blocks renames only on Windows")
def test_r4_xpl_04_file_held_open_during_republish(tmp_path):
    conv = e2e.build(tmp_path / "b", "hard_cases", "HC-03-REPEAT-CONSISTENT", "fake")
    later, earlier = tmp_path / "later.json", tmp_path / "earlier.json"
    later.write_text(json.dumps({"repeat_policy": "later"}), encoding="utf-8")
    earlier.write_text(json.dumps({"repeat_policy": "earlier"}), encoding="utf-8")
    out = tmp_path / "Selected"
    assert conv.select(out, "--publish", "--profile", str(later)).code == 0
    visit = next(os.path.join(d) for d, _s, fs in os.walk(out) if "selection.json" in fs)
    before = sorted(os.listdir(visit))
    held = next(os.path.join(d, f) for d, _s, fs in os.walk(visit) for f in fs if f.endswith(".nii.gz"))
    with open(held, "rb"):
        run = conv.select(out, "--publish", "--profile", str(earlier))
    assert run.code == 2, run.code
    assert sorted(os.listdir(visit)) == before
    assert not [d for d in os.listdir(os.path.dirname(visit)) if d.startswith((".incoming_", ".staging_"))]


# ---------------------------------------------------------------------------- R4-XPL-05

STOPPER = r'''
import signal, sys
from mri_dicom_pipeline import selection, cli
real = selection.publish_session
calls = [0]
def publish(*a, **k):
    result = real(*a, **k)
    calls[0] += 1
    if calls[0] == 1:
        signal.raise_signal(signal.SIGINT)
    return result
selection.publish_session = publish
sys.exit(cli.main(sys.argv[1:]))
'''


def _two_subject_project(tmp_path):
    a = next(e for e in catalog.load_file("structural")["exams"] if e["id"] == "STR-01-GE-PREMIER-CENSUS")
    b = next(e for e in catalog.load_file("structural")["exams"] if e["id"] == "STR-03-SIEMENS-ESPREE-2D")
    project = R4Project(tmp_path / "project")
    project.write_roster(a["roster_rows"] + b["roster_rows"])
    for exam in (a, b):
        for s in exam["sessions"]:
            project.place(exam, s)
    assert project.run("prepare").code == 0
    args, env = e2e.converter_args(tmp_path, a, "fake")
    assert project.run("convert", *args, env=env).code == 0
    return project


def test_r4_xpl_05_stop_between_visits(tmp_path):
    """A stop raised while select --publish runs (Ctrl+C's signal, raised after the first visit is
    published): exit 130, and no partial visit folder."""
    project = _two_subject_project(tmp_path)
    out = tmp_path / "Selected"
    proc = subprocess.run([sys.executable, "-c", STOPPER, "select", "--project-root", project.root,
                           "--output-root", str(out), "--publish"], capture_output=True, text=True, timeout=600)
    assert proc.returncode == 130, (proc.returncode, proc.stderr[-800:])
    visits = [os.path.join(d) for d, _s, fs in os.walk(out) if ".pipeline_complete.json" in fs]
    leftovers = [d for d, ds, _f in os.walk(out) for x in ds if x.startswith((".staging_", ".incoming_", ".previous_"))]
    assert len(visits) == 1 and not leftovers, (visits, leftovers)


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM to a child process runs on L")
def test_r4_xpl_05_sigterm(tmp_path):
    project = _two_subject_project(tmp_path)
    out = tmp_path / "Selected"
    code = STOPPER.replace("signal.SIGINT", "signal.SIGTERM")
    proc = subprocess.run([sys.executable, "-c", code, "select", "--project-root", project.root,
                           "--output-root", str(out), "--publish"], capture_output=True, text=True, timeout=600)
    assert proc.returncode == 143, proc.returncode
    leftovers = [d for d, ds, _f in os.walk(out) for x in ds if x.startswith((".staging_", ".incoming_", ".previous_"))]
    assert not leftovers
