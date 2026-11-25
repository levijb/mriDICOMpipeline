"""TG-VIS: visits (plan section 5, TG-VIS; R-VIS-1 to R-VIS-4).

Tier 2 (the tier 2 converter): catalog visits.json. Expected values: README "Scan selection"
(sessions on one date are one visit) and scan-selection.md 7.2 (P); the timeline offsets are
computed independently here (C): each later session's times move by k x 86,400 s and its series
numbers by k x 1,000,000, k being its position within the date.
"""

from __future__ import annotations

import json
import os

import pytest

from r4lib import catalog, e2e

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _exam(exam_id):
    return next(e for e in catalog.load_file("visits")["exams"] if e["id"] == exam_id)


# ---------------------------------------------------------------------------- R4-VIS-01

@pytest.mark.parametrize("exam_id", ["VIS-01-TWO-SESSIONS", "VIS-02-THREE-SESSIONS-CONTRAST-IN-SECOND"])
def test_r4_vis_01_one_folder_and_one_row_set_per_date(fake_exam, tmp_path, exam_id):
    conv = fake_exam("visits", exam_id)
    exam = conv.exam
    out = tmp_path / "Selected"
    run = conv.select(out, "--publish", "--extras")
    labels = [s["label"] for s in exam["sessions"]]
    date = labels[0][:8]
    subject = exam["roster_rows"][0][0]
    want = exam["expected"].get("R-VIS-1", {}).get("outcome", {})
    assert {r["session"] for r in run.rows} == {want.get("session", date)}
    assert {r["sessions"] for r in run.rows} == {";".join(want.get("sessions", labels))}
    roles = [r["role"] for r in run.rows]
    assert len(roles) == len(set(roles)), "one row per role"
    folders = sorted(f"Subjects/{subject}/{d}" for d in os.listdir(out / "Subjects" / subject))
    assert folders == want.get("visit_folders", [f"Subjects/{subject}/{date}"])
    assert {r["session"] for r in run.vrows} == {date}


@pytest.mark.parametrize("exam_id,req", [("VIS-01-TWO-SESSIONS", "R-RNK-3"), ("VIS-01-TWO-SESSIONS", "R-REV-3"),
                                         ("VIS-02-THREE-SESSIONS-CONTRAST-IN-SECOND", "R-RNK-3"),
                                         ("VIS-02-THREE-SESSIONS-CONTRAST-IN-SECOND", "R-REV-3")])
def test_r4_vis_decisions(fake_exam, exam_id, req):
    problems = fake_exam("visits", exam_id).check(req)
    assert not problems, json.dumps(problems, indent=1)


# ---------------------------------------------------------------------------- R4-VIS-02

def test_r4_vis_02_visit_waits_for_its_last_session(tmp_path):
    exam = _exam("VIS-03-THIRD-SESSION-NOT-CONVERTED")
    want = exam["expected"]["R-VIS-2"]["outcome"]
    conv = e2e.build(tmp_path / "b", "visits", exam["id"], "fake", sessions=["20261003", "20261003_2"])
    run = conv.select(tmp_path / "o")
    assert run.rows and {r["outcome"] for r in run.rows} == {want["every_role"]}
    for text in want["reasons_include"]:
        assert all(text in r["reasons"] for r in run.rows), {r["reasons"] for r in run.rows}
    assert run.code == exam["expected"]["R-REV-3"]["outcome"]["exit_code"]


# ---------------------------------------------------------------------------- R4-VIS-03

def test_r4_vis_03_later_sessions_count_as_later(fake_exam):
    conv = fake_exam("visits", "VIS-02-THREE-SESSIONS-CONTRAST-IN-SECOND")
    run = conv.standard()[0]
    problems = e2e.check_phases(run, conv.exam["expected"]["R-VIS-3"]["outcome"]["phases"])
    assert not problems, problems


@pytest.mark.parametrize("exam_id", ["VIS-01-TWO-SESSIONS", "VIS-02-THREE-SESSIONS-CONTRAST-IN-SECOND"])
def test_r4_vis_03_timeline_offsets(fake_exam, exam_id):
    """The visit's merged records (selection.visit_series, the 7.2 merge `select` applies in memory)
    against offsets computed here from each session's cached records."""
    from mri_dicom_pipeline import selection

    conv = fake_exam("visits", exam_id)
    conv.standard()
    exam = conv.exam
    offsets = exam["expected"]["R-VIS-3"]["outcome"]["timeline_offsets"]
    subject = exam["roster_rows"][0][0]
    parts = []
    for s in exam["sessions"]:
        path = os.path.join(conv.root, "Reports", ".selection_cache", subject, f"{s['label']}.json")
        with open(path, encoding="utf-8") as f:
            parts.append(json.load(f)["series"])
    merged = selection.visit_series(parts)
    problems = []
    for k, (session, records_k) in enumerate(zip(exam["sessions"], parts)):
        want = offsets[session["label"]]
        assert want["acq_s"] == k * 86400 and want["series_number"] == k * 1_000_000, "catalog offsets"
        for key, rec in records_k.items():
            got = merged[key]
            if rec.get("acq_rel_s") is not None and abs(got["acq_rel_s"] - (rec["acq_rel_s"] + want["acq_s"])) > 1e-6:
                problems.append((session["label"], "acq_rel_s"))
            if rec.get("series_number") is not None and got["series_number"] != rec["series_number"] + want["series_number"]:
                problems.append((session["label"], "series_number"))
    assert not problems, problems


# ---------------------------------------------------------------------------- R4-VIS-04

def test_r4_vis_04_override_names_the_date(fake_exam, tmp_path):
    conv = fake_exam("visits", "VIS-01-TWO-SESSIONS")
    path = tmp_path / "overrides.csv"
    path.write_text("subject_id,session,role,action,candidate_id\nS1001,20261001,T1,none,\n", encoding="utf-8")
    run = conv.select(tmp_path / "o", "--overrides", str(path))
    t1 = run.role_row("T1")
    assert (t1["outcome"], t1["decided_by"]) == ("no_eligible_candidate", "override"), t1


def test_r4_vis_04_session_option_evaluates_the_whole_visit(fake_exam, tmp_path):
    conv = fake_exam("visits", "VIS-01-TWO-SESSIONS")
    run = conv.select(tmp_path / "o", "--session", "S1001/20261001_2")
    assert {r["session"] for r in run.rows} == {"20261001"}
    assert run.uids_of(run.role_row("T1")["candidate_id"]) == [3]
    assert run.uids_of(run.role_row("DWI")["candidate_id"]) == [105]
