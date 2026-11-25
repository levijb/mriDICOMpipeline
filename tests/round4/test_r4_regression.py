"""TG-REG: regression of `prepare` and `convert` (plan section 5, TG-REG; R-REG-1 to R-REG-5).

Expected values: independent SHA-256 of the catalog UIDs (C), the README checklist (P), and the
record keys README and scan-selection.md 5.2 and 7.2 rely on (P).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

import pytest

from r4lib import catalog, dicom_builder, e2e
from r4lib.project import R4Project

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sidecars(conv):
    for dirpath, _d, files in os.walk(os.path.join(conv.root, "Niftis", "Subjects")):
        for f in files:
            if f.endswith(".json") and not f.startswith("."):
                yield os.path.join(dirpath, f), json.load(open(os.path.join(dirpath, f), encoding="utf-8"))


def _xa_shared_series_time():
    """HC-07 (Siemens XA classic export) with both series given one SeriesTime, as XA exports do."""
    exam = json.loads(json.dumps(next(e for e in catalog.load_file("hard_cases")["exams"]
                                      if e["id"] == "HC-07-XA-CLASSIC")))
    exam["uid_exam"] = 9750
    for s in exam["sessions"][0]["series"]:
        s["tags"]["SeriesTime"] = "100000"
    return exam


@pytest.mark.r4real
@pytest.mark.parametrize("variant", ["classic", "xa_shared_series_time"])
def test_r4_reg_01_outputs_map_to_their_series(tmp_path, r4_dcm2niix, variant):
    if variant == "classic":
        exam = next(e for e in catalog.load_file("structural")["exams"] if e["id"] == "STR-01-GE-PREMIER-CENSUS")
    else:
        exam = _xa_shared_series_time()
    project = R4Project(tmp_path / "project")
    project.place_exam(exam)
    assert project.run("prepare").code == 0
    assert project.run("convert", "--dcm2niix-bin", r4_dcm2niix).code == 0
    conv = e2e.Converted(project, exam, "real", [])
    keys = {hashlib.sha256(dicom_builder.series_uid(exam["uid_exam"], s["uid_series"]).encode()).hexdigest()[:20]:
            s["uid_series"] for s in e2e.all_series(exam)}
    found = {}
    for path, side in _sidecars(conv):
        found.setdefault(side.get("source_series_key"), []).append(os.path.basename(path))
    unknown = [k for k in found if k not in keys]
    missing = [keys[k] for k in keys if k not in found]
    assert not unknown and not missing, (unknown, missing)
    status = {r["session"]: r for r in project.session_status()}
    assert all("convert" in str(r).lower() for r in status.values())


def _inventories(conv):
    for dirpath, _d, files in os.walk(os.path.join(conv.root, "DicomExtracted", "Subjects")):
        if ".inventory.json" in files:
            yield json.load(open(os.path.join(dirpath, ".inventory.json"), encoding="utf-8"))


@pytest.mark.parametrize("cat_name,exam_id", [("naming", "NAM-11-TWO-STUDY-ZIP"),
                                              ("visits", "VIS-01-TWO-SESSIONS"),
                                              ("structural", "STR-01-GE-PREMIER-CENSUS")])
def test_r4_reg_02_inventory_study_keys(tmp_path, cat_name, exam_id):
    exam = next(e for e in catalog.load_file(cat_name)["exams"] if e["id"] == exam_id)
    project = R4Project(tmp_path / "project")
    project.place_exam(exam)
    project.run("prepare", *(["--accept-multi-study", f"{exam['roster_rows'][0][0]}/20261101"]
                             if exam_id == "NAM-11-TWO-STUDY-ZIP" else []))
    conv = e2e.Converted(project, exam, None, [])
    invs = list(_inventories(conv))
    assert invs, "no extraction inventory"
    want = set()
    for s in exam["sessions"]:
        for series in s["series"]:
            study = series.get("uid_study", s.get("uid_study", exam["uid_exam"]))
            want.add(hashlib.sha256(dicom_builder.study_uid(study).encode()).hexdigest())
    got = set()
    for inv in invs:
        got |= set(inv["study_keys"])
    assert got == want, (sorted(got), sorted(want))


# ---------------------------------------------------------------------------- R4-REG-03

def checklist_tests():
    text = open(os.path.join(REPO, "docs", "readme-checklist.md"), encoding="utf-8").read()
    out = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cell = line.rsplit("|", 2)[-2] if line.count("|") >= 3 else ""
        current = None
        for token in re.findall(r"`([^`]+)`", cell):
            if "::" in token:
                current, name = token.split("::", 1)
                out.append((current, name))
            elif token.startswith("test_") and current and not token.endswith(".py"):
                out.append((current, token))
            elif token.endswith(".py"):
                current = token
    return sorted(set(out))


@pytest.fixture(scope="module")
def collected():
    proc = subprocess.run([sys.executable, "-m", "pytest", "-o", "addopts=", "--collect-only", "-q",
                           "-p", "no:cacheprovider",
                           os.path.join(REPO, "tests"), "--ignore", os.path.join(REPO, "tests", "round4")],
                          capture_output=True, text=True, cwd=REPO, timeout=900)
    ids = set()
    for line in proc.stdout.splitlines():
        if "::" in line:
            path, name = line.split("::", 1)
            ids.add((os.path.basename(path), name.split("[")[0]))
    return ids


def test_r4_reg_03_every_checklist_test_is_collected(collected):
    named = checklist_tests()
    assert len(named) > 100, len(named)
    missing = [f"{f}::{n}" for f, n in named if (os.path.basename(f), n) not in collected]
    e2e.record("checklist", {"named": len(named), "missing": missing})
    assert not missing, missing


# ---------------------------------------------------------------------------- R4-REG-04

HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def test_r4_reg_04_records_select_reads_keep_their_form(fake_exam):
    conv = fake_exam("visits", "VIS-01-TWO-SESSIONS")
    index = json.load(open(os.path.join(conv.root, "Reports", ".state", "index.json"), encoding="utf-8"))
    problems = []
    for key, entry in index["sessions"].items():
        if not isinstance(entry.get("content_sha256"), str) or not HEX64.match(entry["content_sha256"]):
            problems.append(f"{key}: content_sha256")
        for name in ("extraction", "conversion"):
            rec = entry.get(name) or {}
            for field, kind in (("receipt", str), ("receipt_signature", list), ("directories", dict),
                                ("inventory", str), ("root", str), ("version", int)):
                if not isinstance(rec.get(field), kind):
                    problems.append(f"{key}: {name}.{field} is {type(rec.get(field)).__name__}")
        conv_rec = entry.get("conversion") or {}
        if conv_rec.get("status") != "converted" or not isinstance(conv_rec.get("receipt_id"), str):
            problems.append(f"{key}: conversion status/receipt_id")
        if conv_rec.get("source_sha256") != entry.get("content_sha256"):
            problems.append(f"{key}: conversion.source_sha256")
    for path, side in _sidecars(conv):
        name = os.path.basename(path)
        if not re.fullmatch(r"[0-9a-f]{20}", str(side.get("source_series_key", ""))):
            problems.append(f"{name}: source_series_key")
        if not isinstance(side.get("SeriesNumber"), int):
            problems.append(f"{name}: SeriesNumber")
        if not isinstance(side.get("ImageType"), list):
            problems.append(f"{name}: ImageType")
        if "EchoTime" in side and not isinstance(side["EchoTime"], (int, float)):
            problems.append(f"{name}: EchoTime")
    for dirpath, _d, files in os.walk(os.path.join(conv.root, "Niftis", "Subjects")):
        if ".inventory.json" in files:
            inv = json.load(open(os.path.join(dirpath, ".inventory.json"), encoding="utf-8"))
            for item in inv["files"]:
                if not (isinstance(item["name"], str) and isinstance(item["size"], int) and HEX64.match(item["sha256"])):
                    problems.append(f"inventory item {item}")
    assert not problems, problems


# ---------------------------------------------------------------------------- R4-REG-05

def test_r4_reg_05_thousand_cached_sessions():
    proc = subprocess.run([sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "-p", "no:cacheprovider",
                           "tests/test_pipeline.py::test_1000_cached_sessions_do_not_read_scan_contents"],
                          capture_output=True, text=True, cwd=REPO, timeout=1800)
    assert proc.returncode == 0, proc.stdout[-2000:]
    assert "1 passed" in proc.stdout, proc.stdout[-500:]
