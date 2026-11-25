"""TG-CLN: the Uploads/ empty-folder cleanup (plan section 5, TG-CLN; R-NAM-10; D-14 and OQ-13,
P).

Each layout of catalog cleanup.json is built in a temporary project; `prepare` runs (with --dry-run
for layout I); the upload tree is listed before and after. Junctions (Windows) are made with
`cmd /c mklink /J`, symbolic links (Linux) with os.symlink; hidden attributes with `attrib +h`.
Left untested, as the plan says: a process killed after the ZIP moved but before the cleanup ran
(the R-NAM-10 known limit).
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from r4lib import catalog
from r4lib.project import R4Project

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
CLEANUP = {e["id"]: e for e in catalog.load_file("cleanup")["exams"]}


def _listing(root):
    out = []
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
        out += [f"{rel}/{d}/" for d in dirs] + [f"{rel}/{f}" for f in files]
    return sorted(out)


def _link(link, target):
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", link, target], check=True, capture_output=True)
    else:
        os.symlink(target, link, target_is_directory=True)


def build(tmp_path, exam, hidden_attribute=False):
    project = R4Project(tmp_path / "project")
    project.write_roster(exam["roster_rows"])
    uploads = os.path.join(project.root, "DicomUploads")
    sessions = {s.get("key", s["label"]): s for s in exam["sessions"]}
    target_root = None
    for item in exam["layout"]:
        if "zip" in item:
            project.place(exam, sessions[item["zip"]])
        elif "file" in item:
            rel = item["file"]
            if hidden_attribute and os.path.basename(rel) == ".hidden":
                rel = rel[: -len(".hidden")] + "hidden.txt"
            path = os.path.join(uploads, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("kept\n")
            if hidden_attribute and rel.endswith("hidden.txt"):
                subprocess.run(["attrib", "+h", path], check=True, capture_output=True)
        elif "dir" in item:
            os.makedirs(os.path.join(uploads, *item["dir"].split("/")), exist_ok=True)
        elif "link" in item:
            target_root = str(tmp_path)
            target = os.path.join(target_root, *item["target"].split("/"))
            for rel in item.get("target_contents", []):
                os.makedirs(os.path.join(target_root, *rel.strip("/").split("/")), exist_ok=True)
            _link(os.path.join(uploads, *item["link"].split("/")), target)
    return project, uploads, target_root


def _resolve(rel, uploads, tmp_path):
    if rel.startswith("<tmp>/"):
        return os.path.join(str(tmp_path), *rel[len("<tmp>/"):].split("/"))
    return os.path.join(uploads, *rel.split("/"))


@pytest.mark.parametrize("exam_id", sorted(CLEANUP))
def test_r4_cln_layouts(tmp_path, exam_id):
    exam = CLEANUP[exam_id]
    project, uploads, target_root = build(tmp_path, exam)
    before = _listing(uploads)
    target_before = _listing(os.path.join(target_root, "outside")) if target_root else None
    run = exam.get("run", ["prepare"])
    result = project.run(run[0], *run[1:])
    want = exam["expected"]["R-NAM-10"]["outcome"]
    problems = []
    for rel in want.get("after_absent", []):
        if os.path.lexists(_resolve(rel, uploads, tmp_path)):
            problems.append(f"{rel} still present")
    for rel in want.get("after_present", []):
        if not os.path.lexists(_resolve(rel, uploads, tmp_path)):
            problems.append(f"{rel} removed")
    if want.get("target_listing_unchanged") and _listing(os.path.join(target_root, "outside")) != target_before:
        problems.append("the link target changed")
    if exam_id == "CLN-09-I" and _listing(uploads) != before:
        problems.append("the dry run changed Uploads/")
    record = {"exam": exam_id, "exit": result.code, "before": before, "after": _listing(uploads), "problems": problems}
    path = os.environ.get("R4_EVIDENCE_DIR")
    if path:
        with open(os.path.join(path, "cleanup_listings.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    assert os.path.isdir(os.path.join(uploads, "Uploads")), "Uploads/ itself was removed"
    assert not problems, (problems, result.code, result.stderr[-800:])


@pytest.mark.skipif(os.name != "nt", reason="the hidden attribute is a Windows file attribute")
def test_r4_cln_07_hidden_attribute_windows(tmp_path):
    """Layout G's Windows variant: a file with the hidden attribute set keeps its folder."""
    exam = CLEANUP["CLN-07-G"]
    project, uploads, _ = build(tmp_path, exam, hidden_attribute=True)
    project.run("prepare")
    for rel in exam["expected"]["R-NAM-10"]["outcome"]["after_present"]:
        assert os.path.lexists(os.path.join(uploads, *rel.split("/"))), rel
    assert os.path.exists(os.path.join(uploads, "Uploads", "b1", "S808", "hidden.txt"))
