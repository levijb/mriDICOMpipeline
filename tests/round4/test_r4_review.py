"""TG-REV: review and overrides (plan section 5, TG-REV; R-REV-1 to R-REV-3).

Tier 2: catalog exams converted with the tier 2 converter (r4lib/fake_dcm2niix.py); only review,
override and exit-code behavior is checked, none of which depends on what real converter output
looks like. Expected values: README "Scan selection" and the runbook's overrides section (P).
"""

from __future__ import annotations

import csv
import json
import os

import pytest

from r4lib import e2e, sentinels

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _overrides(path, rows, extra_columns=()):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["subject_id", "session", "role", "action", "candidate_id", *extra_columns])
        w.writerows(rows)
    return str(path)


def _visit_folder(out, run):
    row = run.rows[0]
    return os.path.join(str(out), "Subjects", row["subject_id"], row["session"])


def _role_files(folder, role):
    path = os.path.join(folder, role)
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


# ---------------------------------------------------------------------------- R4-REV-01

REVIEW_CASES = [
    ("hard_cases", "HC-02-DUPLICATE", "T1", "duplicate_series"),
    ("hard_cases", "HC-03-REPEAT-CONSISTENT", "T1", "repeat_needs_review"),
    ("diffusion", "DIF-13-UNLINKED-ADC", "ADC", "derived_unlinked"),
    ("diffusion", "DIF-04-XA-DWI-NO-GRADIENTS", "DWI", "no_gradient_table"),
]


@pytest.mark.parametrize("cat_name,exam_id,role,reason", REVIEW_CASES, ids=[c[1] for c in REVIEW_CASES])
def test_r4_rev_01_review_leaves_the_role_folder_empty(fake_exam, tmp_path, cat_name, exam_id, role, reason):
    """Duplicates, repeats, an unlinkable map and missing evidence (DIF-04 through the tier 2
    converter, which writes no gradient files when the catalog gives the series none)."""
    conv = fake_exam(cat_name, exam_id)
    out = tmp_path / "Selected"
    run = conv.select(out, "--publish")
    row = run.role_row(role)
    assert row["outcome"] == "review_required", row
    assert not e2e.reasons_include(row["reasons"], [reason]), row["reasons"]
    assert run.code == 2
    assert _role_files(_visit_folder(out, run), role) == []


# ---------------------------------------------------------------------------- R4-REV-02

@pytest.fixture
def hc03(fake_exam, tmp_path):
    conv = fake_exam("hard_cases", "HC-03-REPEAT-CONSISTENT")
    first = conv.select(tmp_path / "first")
    ids = {r["_uid"]: r["acquisition_id"] for r in first.vrows}
    return conv, ids, first.rows[0]["subject_id"], first.rows[0]["session"]


def test_r4_rev_02_choose_exclude_none(hc03, tmp_path):
    conv, ids, sub, ses = hc03
    choose = conv.select(tmp_path / "c", "--overrides",
                         _overrides(tmp_path / "c.csv", [[sub, ses, "T1", "choose", ids[3]]]))
    t1 = choose.role_row("T1")
    assert (t1["outcome"], t1["decided_by"], choose.uids_of(t1["candidate_id"])) == ("selected", "override", [3]), t1
    exclude = conv.select(tmp_path / "x", "--overrides",
                          _overrides(tmp_path / "x.csv", [[sub, ses, "T1", "exclude", ids[7]]]))
    t1 = exclude.role_row("T1")
    assert t1["outcome"] in ("selected", "selected_fallback") and exclude.uids_of(t1["candidate_id"]) == [3], t1
    assert "override_applied" in e2e.split(t1["warnings"]), t1
    none = conv.select(tmp_path / "n", "--publish", "--overrides",
                       _overrides(tmp_path / "n.csv", [[sub, ses, "T1", "none", ""]]))
    t1 = none.role_row("T1")
    assert (t1["outcome"], t1["decided_by"], t1["candidate_id"]) == ("no_eligible_candidate", "override", ""), t1
    folder = os.path.join(str(tmp_path / "n"), "Subjects", sub, ses)
    assert _role_files(folder, "T1") == []


def test_r4_rev_02_stale_override(hc03, tmp_path):
    conv, ids, sub, ses = hc03
    run = conv.select(tmp_path / "s", "--overrides",
                      _overrides(tmp_path / "s.csv", [[sub, ses, "T1", "choose", "0123456789ab"]]))
    t1 = run.role_row("T1")
    assert t1["outcome"] == "review_required" and "override_stale" in e2e.split(t1["reasons"]), t1
    assert run.code == 2


def test_r4_rev_02_invalid_row_stops_the_run_naming_it(hc03, tmp_path):
    conv, ids, sub, ses = hc03
    path = _overrides(tmp_path / "bad.csv", [[sub, ses, "T1", "exclude", ids[7]],
                                            [sub, ses, "T1", "promote", ids[3]]])
    out = tmp_path / "bad"
    run = conv.select(out, "--overrides", path)
    assert run.code == 2
    assert "row 3" in (run.result.stdout + run.result.stderr), (run.result.stdout[-500:], run.result.stderr[-500:])
    assert not os.path.exists(out / "selection.csv")


def test_r4_rev_02_extra_columns_never_copied(hc03, tmp_path):
    conv, ids, sub, ses = hc03
    note = "R4SENT_OVERRIDE_NOTE"
    out = tmp_path / "note"
    run = conv.select(out, "--publish", "--overrides",
                      _overrides(tmp_path / "note.csv", [[sub, ses, "T1", "choose", ids[3], note]], ("note",)))
    assert run.role_row("T1")["decided_by"] == "override"
    hits = sentinels.scan_tree(str(out), [note])
    hits.update({"Reports/" + k: v for k, v in sentinels.scan_tree(os.path.join(conv.root, "Reports"), [note]).items()})
    assert not hits, hits


# ---------------------------------------------------------------------------- R4-REV-03

def test_r4_rev_03_exit_code_clean(fake_exam, tmp_path):
    assert fake_exam("structural", "STR-01-GE-PREMIER-CENSUS").select(tmp_path / "o").code == 0


def test_r4_rev_03_exit_code_review(fake_exam, tmp_path):
    assert fake_exam("hard_cases", "HC-03-REPEAT-CONSISTENT").select(tmp_path / "o").code == 2


def test_r4_rev_03_exit_code_not_converted_only(tmp_path):
    """A visit waiting only for a session that is not converted yet: exit 0 (VIS-03's steps)."""
    conv = e2e.build(tmp_path / "b", "visits", "VIS-03-THIRD-SESSION-NOT-CONVERTED", "fake",
                     sessions=["20261003", "20261003_2"])
    run = conv.select(tmp_path / "o")
    assert {r["outcome"] for r in run.rows} == {"source_not_ready"}
    assert run.code == 0


@pytest.mark.parametrize("change", ["renamed", "edited_in_place_with_audit"])
def test_r4_rev_03_exit_code_output_modified(tmp_path, change):
    """README "Fast repeated runs": a renamed output invalidates the conversion receipt; an in-place
    edit is caught by --audit (the fast check does not see it, as the README states)."""
    conv = e2e.build(tmp_path / "b", "structural", "STR-03-SIEMENS-ESPREE-2D", "fake")
    nifti = next(os.path.join(d, f) for d, _s, fs in os.walk(os.path.join(conv.root, "Niftis", "Subjects"))
                 for f in fs if f.endswith(".nii.gz"))
    if change == "renamed":
        os.rename(nifti, nifti + ".moved")
        run = conv.select(tmp_path / "o")
    else:
        with open(nifti, "ab") as f:
            f.write(b"\0")
        run = conv.select(tmp_path / "o", "--audit")
    reasons = {r["reasons"] for r in run.rows}
    assert {r["outcome"] for r in run.rows} == {"source_not_ready"} and reasons == {"output_modified"}, reasons
    assert run.code == 2


def test_r4_rev_03_exit_code_refused_publication(tmp_path):
    """A published visit holding an added file, then a changed decision: refused, exit 2 (7.5 rule 5)."""
    conv = e2e.build(tmp_path / "b", "hard_cases", "HC-03-REPEAT-CONSISTENT", "fake")
    later, earlier = tmp_path / "later.json", tmp_path / "earlier.json"
    later.write_text(json.dumps({"repeat_policy": "later"}), encoding="utf-8")
    earlier.write_text(json.dumps({"repeat_policy": "earlier"}), encoding="utf-8")
    out = tmp_path / "o"
    first = conv.select(out, "--publish", "--profile", str(later))
    assert first.code == 0, first.rows
    folder = _visit_folder(out, first)
    with open(os.path.join(folder, "notes.txt"), "w", encoding="utf-8") as f:
        f.write("added by a person\n")
    second = conv.select(out, "--publish", "--profile", str(earlier))
    assert second.code == 2
    assert os.path.exists(os.path.join(folder, "notes.txt"))
