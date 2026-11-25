"""TG-NAM: how prepare names unnamed upload ZIPs, and its StudyDate check (plan section 5, TG-NAM;
R-NAM-1 to R-NAM-9).

Integration tests run `prepare` on the catalog's naming.json exams in temporary projects and
read renamed_uploads.csv, issues.csv, session_status.csv and the upload tree. The unit tests call
naming.resolve_date only. Expected values: upload-naming rules 1-5 and the README (P), as the
catalog and the literal rows below state them.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest

from r4lib import catalog, crash, dicom_builder
from r4lib.project import R4Project

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
NAMING = catalog.load_file("naming")
EXAMS = {e["id"]: e for e in NAMING["exams"]}
NONBLOCKING = ("date:", "missing:", "spacing:")


# ---------------------------------------------------------------------------- R4-NAM-01 (unit)

L = ["20261101"]
# (header dates, listed dates, allow_unlisted, tolerance, expected date or None). Derived by hand from
# upload-naming rule 2: a listed date when exactly one lies within the tolerance (inclusive), held when
# two do or none does, the StudyDate itself with --allow-unlisted-dates.
RESOLVE_ROWS = [
    (["20261101"], L, False, 2, "20261101"),
    (["20261102"], L, False, 2, "20261101"), (["20261031"], L, False, 2, "20261101"),
    (["20261103"], L, False, 2, "20261101"), (["20261030"], L, False, 2, "20261101"),
    (["20261104"], L, False, 2, None), (["20261029"], L, False, 2, None),
    (["20261102"], L, False, 0, None), (["20261101"], L, False, 0, "20261101"),
    (["20261105"], L, False, 4, "20261101"), (["20261106"], L, False, 4, None),
    (["20261103"], ["20261101", "20261105"], False, 4, None),
    (["20261103"], ["20261101", "20261105"], False, 1, None),
    (["20261103"], [], False, 2, None),
    (["20261110"], L, True, 2, "20261110"),
    (["20261102"], L, True, 2, "20261101"),
    ([], L, True, 2, None), (["20261101", "20261102"], L, False, 2, None), (["20261340"], L, True, 2, None),
]


@pytest.mark.parametrize("dates,listed,allow,tol,want", RESOLVE_ROWS)
def test_r4_nam_01_resolve_date(dates, listed, allow, tol, want):
    from mri_dicom_pipeline import naming

    got, _note = naming.resolve_date(dates, set(listed), allow, tol, "S1")
    assert got == want


# ---------------------------------------------------------------------------- integration helpers

class Naming:
    def __init__(self, tmp_path, exam):
        self.exam = exam
        self.project = R4Project(tmp_path / "project")
        self.project.write_roster(exam["roster_rows"])
        self.uploads = os.path.join(self.project.root, "DicomUploads")
        self.written = {}

    def place(self, keys=None, data_for=None):
        for s in self.exam["sessions"]:
            key = s.get("key", s["label"])
            if keys is not None and key not in keys:
                continue
            if s.get("symlink_target"):
                target = os.path.join(self.project.root, *s["symlink_target"].split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(self.project.zip_bytes(self.exam, s))
                link = self.project.zip_target(s)
                os.makedirs(os.path.dirname(link), exist_ok=True)
                os.symlink(target, link)
                continue
            data = (data_for or {}).get(key)
            path = self.project.place(self.exam, s, data)
            with open(path, "rb") as f:
                self.written[s["upload_path"] if s.get("upload_path") else "Uploads/" + s["zip_name"]] = \
                    hashlib.sha256(f.read()).hexdigest()

    def complete(self, keys):
        for s in self.exam["sessions"]:
            if s.get("key", s["label"]) in keys:
                path = self.project.zip_target(s)
                data = self.project.zip_bytes(self.exam, s)
                with open(path, "wb") as f:
                    f.write(data)

    def run(self, *args):
        return self.project.run("prepare", *args)

    def path(self, rel):
        return os.path.join(self.uploads, *rel.split("/"))

    def log_rows(self):
        return self.project.read_csv("Reports", "renamed_uploads.csv")

    def issues(self):
        return self.project.issues()


def check(n, want, result):
    """Problems for one expected block (or one step's block)."""
    out = []
    rows = n.log_rows()
    for old, new in want.get("renames", {}).items():
        if not any(r["old_path"] == old and r["new_path"] == new for r in rows):
            out.append(f"no log row {old} -> {new}")
        if os.path.exists(n.path(old)):
            out.append(f"{old} still present")
    if "renames" in want and not want["renames"]:
        held_paths = set(want.get("held", []))
        bad = [r for r in rows if r["old_path"] in held_paths]
        if bad:
            out.append(f"renamed a held ZIP: {bad}")
    for rel in want.get("sorted", []):
        if not os.path.exists(n.path(rel)):
            out.append(f"{rel} missing")
    for rel in want.get("held", []):
        if not os.path.exists(n.path(rel)):
            out.append(f"held ZIP {rel} moved")
        elif want.get("zip_sha256_unchanged") and rel in n.written:
            with open(n.path(rel), "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() != n.written[rel]:
                    out.append(f"{rel} changed")
    issues = n.issues()
    for rel in want.get("held", []):
        # R4-NAM-09/10: each held ZIP stays unnamed with a blocking issue that names it.
        full = os.path.normcase(n.path(rel))
        if not any(os.path.normcase(i["path"]) == full and not i["key"].startswith(NONBLOCKING) for i in issues):
            out.append(f"held ZIP {rel} has no blocking issue (exit {result.code})")
    for spec in want.get("issues", []):
        def match(i):
            if "blocking" in spec and (not i["key"].startswith(NONBLOCKING)) is not spec["blocking"]:
                return False
            if spec.get("kind") == "date" and not i["key"].startswith("date:"):
                return False
            return all(d in i["reason"] for d in spec.get("names_dates", []))
        relevant = [i for i in issues if any(h.replace("/", os.sep) in i["path"] or h.split("/")[-2] in i["key"]
                                             for h in want.get("held", []) + list(want.get("renames", {})))] or issues
        if not any(match(i) for i in relevant):
            out.append(f"no issue matching {spec}: {[(i['key'][:40], i['reason'][:80]) for i in relevant]}")
    if "log_rows" in want and len(rows) != want["log_rows"]:
        out.append(f"{len(rows)} log rows, expected {want['log_rows']}")
    if "log_row" in want:
        alias = {"subject": "subject_id"}
        if not any(all(r.get(alias.get(k, k)) == v for k, v in want["log_row"].items()) for r in rows):
            out.append(f"no log row with {want['log_row']}: {rows}")
    if want.get("note_states_gap") and not any("day" in r["note"] for r in rows):
        out.append(f"no note states the gap: {[r['note'] for r in rows]}")
    for name in want.get("log_note_for", []):
        if not any(r["new_path"].endswith(name + ".zip") and r["note"] for r in rows):
            out.append(f"no note for {name}")
    for name in want.get("warning_for", []):
        log = open(os.path.join(n.project.root, "Reports", "latest_prepare.log"), encoding="utf-8").read()
        if not any("WARNING" in line and name in line for line in log.splitlines()):
            out.append(f"no warning for {name}")
    for label in want.get("no_session", []):
        if any(r.get("session") == label for r in n.project.session_status()):
            out.append(f"session {label} exists")
    return out


STEP_EXAMS = [e for e in EXAMS if e not in ("NAM-15-NAMED-DATE-TOLERANCE",)]


@pytest.mark.parametrize("exam_id", STEP_EXAMS)
def test_r4_nam_catalog(tmp_path, exam_id):
    """R4-NAM-02 to R4-NAM-11 from the catalog's steps and expected blocks."""
    exam = EXAMS[exam_id]
    if exam.get("platform") == "L" and os.name == "nt":
        pytest.skip("symbolic links cannot be created on this Windows machine (plan 1); runs on L")
    n = Naming(tmp_path, exam)
    steps = exam.get("steps") or [{"run": ["prepare"]}]
    if not any("place" in s for s in steps):
        n.place()
    problems = {}
    for i, step in enumerate(steps, 1):
        if "place" in step:
            n.place(step["place"])
        if "complete" in step:
            n.complete(step["complete"])
        result = n.run(*step["run"][1:])
        for req, item in exam["expected"].items():
            block = item["outcome"]
            want = block.get(f"step{i}") if any(k.startswith("step") for k in block) else (block if i == len(steps) else None)
            if want:
                found = check(n, want, result)
                if exam_id == "NAM-11-TWO-STUDY-ZIP" and i == len(steps):
                    if not any(not x["key"].startswith(NONBLOCKING) for x in n.issues()):
                        found.append("no blocking issue for the two-study ZIP")
                    if os.path.exists(os.path.join(n.project.root, "DicomExtracted", "Subjects", "S719")):
                        found.append("two-study ZIP extracted")
                if exam_id == "NAM-08-REEXPORT-OF-PREPARED-STUDY" and i == 2:
                    if not os.path.exists(n.path("Uploads/batch2/S707/S707_20261101.zip")):
                        found.append("the renamed copy did not stay in Uploads")
                    log = open(os.path.join(n.project.root, "Reports", "latest_prepare.log"), encoding="utf-8").read()
                    if "DUPLICATE" not in log.upper():
                        found.append("no duplicate reported")
                if found:
                    problems[f"step{i} {req}"] = found
    assert not problems, json.dumps(problems, indent=1)


# ---------------------------------------------------------------------------- R4-NAM-12

def test_r4_nam_12_dry_run_changes_nothing(tmp_path):
    exam = json.loads(json.dumps(EXAMS["NAM-02-LISTED-DATE"]))
    held = EXAMS["NAM-09A-NO-STUDYDATE"]
    near = EXAMS["NAM-03-NEAR-DATE"]
    exam["roster_rows"] += held["roster_rows"] + near["roster_rows"]
    n = Naming(tmp_path, exam)
    n.place()
    for other in (held, near):
        for s in other["sessions"]:
            n.project.place(other, s)
    before = {p: open(p, "rb").read() for p in (n.path(s["upload_path"]) for e in (exam, held, near)
                                                 for s in e["sessions"])}
    result = n.run("--dry-run")
    text = result.stdout + result.stderr
    assert result.code in (0, 2)
    assert "WOULD RENAME" in text
    assert "DRY RUN SUMMARY" in text and "HELD (1)" in text and "CHECK THESE NAMES" in text
    assert all(open(p, "rb").read() == data for p, data in before.items())
    assert not os.path.exists(os.path.join(n.project.root, "Reports", "renamed_uploads.csv"))
    assert not os.path.exists(os.path.join(n.project.root, "Reports", ".naming_cache.json"))


# ---------------------------------------------------------------------------- R4-NAM-13

OPEN_LOGGER = r'''
import builtins, io, json, os, sys, zipfile
log_path = sys.argv[1]; argv = sys.argv[2:]
seen = []
_open_member = zipfile.ZipFile.open
def open_member(self, name, *a, **k):
    seen.append(os.path.basename(str(self.filename)))
    return _open_member(self, name, *a, **k)
zipfile.ZipFile.open = open_member
from mri_dicom_pipeline import cli
code = cli.main(argv)
json.dump(seen, open(log_path, "w"))
sys.exit(code if isinstance(code, int) else 0)
'''


def test_r4_nam_13_unchanged_held_zip_is_read_once(tmp_path):
    n = Naming(tmp_path, EXAMS["NAM-04-TWO-DATES-IN-RANGE"])
    n.place()
    assert n.run("--date-tolerance", "4").code == 2
    log = tmp_path / "members.json"
    proc = subprocess.run([sys.executable, "-c", OPEN_LOGGER, str(log), "prepare", "--project-root", n.project.root,
                           "--date-tolerance", "4"], capture_output=True, text=True, timeout=300)
    assert proc.returncode == 2, proc.stderr[-1000:]
    assert json.loads(log.read_text()) == []


# ---------------------------------------------------------------------------- R4-NAM-14

def test_r4_nam_14_rename_survives_a_crash_in_the_log(tmp_path):
    """README: renamed_uploads.csv must list each rename, also when the process dies right after renaming."""
    n = Naming(tmp_path, EXAMS["NAM-02-LISTED-DATE"])
    n.place()
    markers = tmp_path / "markers"
    result = crash.run_with_crash(["prepare", "--project-root", n.project.root], "zip-renamed", str(markers),
                                  cwd=n.project.root)
    if not crash.fired(str(markers), "zip-renamed"):
        pytest.fail(f"ERROR, not a finding: zip-renamed never fired (exit {result.returncode})")
    n.run()
    rows = [r for r in n.log_rows() if r["old_path"] == "Uploads/batch1/S701/x7f3k.zip"]
    assert len(rows) == 1, n.log_rows()


# ---------------------------------------------------------------------------- R4-NAM-15

def _named_exam(gap):
    exam = json.loads(json.dumps(EXAMS["NAM-15-NAMED-DATE-TOLERANCE"]))
    from datetime import date, timedelta

    d = date(2026, 11, 10) + timedelta(days=gap)
    exam["sessions"][0]["study_date"] = d.strftime("%Y%m%d")
    return exam


MATRIX = [(tol, sign * gap) for tol in (0, 2, 4) for gap in range(0, tol + 2) for sign in (1, -1)
          if not (gap == 0 and sign == -1)]


@pytest.mark.parametrize("tol,gap", MATRIX)
def test_r4_nam_15_date_tolerance(tmp_path, tol, gap):
    exam = _named_exam(gap)
    n = Naming(tmp_path, exam)
    n.place()
    result = n.run("--date-tolerance", str(tol))
    issues = n.issues()
    blocking = [i for i in issues if not i["key"].startswith(NONBLOCKING)]
    warnings = [i for i in issues if i["key"].startswith("date:")]
    held = abs(gap) > tol
    assert bool(blocking) is held, (issues, result.code)
    assert bool(warnings) is (0 < abs(gap) <= tol), issues
    assert result.code == (2 if held else 0)


@pytest.mark.parametrize("key", ["truncated", "bad_crc", "encrypted"])
def test_r4_nam_10_each_damage_kind_alone(tmp_path, key):
    """R4-NAM-10, one damaged ZIP at a time: unnamed, with a blocking issue, and prepare ends
    normally (exit 2 for the unresolved issue)."""
    exam = EXAMS["NAM-10-DAMAGED-ZIPS"]
    n = Naming(tmp_path, exam)
    n.place([key])
    session = next(s for s in exam["sessions"] if s.get("key") == key)
    result = n.run()
    problems = check(n, {"held": [session["upload_path"]], "zip_sha256_unchanged": True}, result)
    assert result.code == 2, (result.code, result.stderr[-600:])
    assert not problems, problems
