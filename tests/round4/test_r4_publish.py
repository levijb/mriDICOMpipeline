"""TG-PUB: publication and recovery (plan section 5, TG-PUB; R-PUB-1 to R-PUB-5).

Tier 2 projects (the tier 2 converter). Expected values: README "Scan selection", the runbook,
scan-selection.md 7.5 and the BIDS design 5.3 (P); SHA-256 values are computed here (C).
Crash points: r4lib/crash.py (plan 3.3); a crash test whose marker is missing is an error, not a
result.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from r4lib import crash, e2e, listing

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
SELECTION_KEYS = {"selection_role", "selection_fate", "selection_candidate_id", "selection_profile", "source_nifti"}
LEFTOVER_PREFIXES = (".incoming_", ".previous_", ".staging_")


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _profile(tmp_path, policy):
    path = tmp_path / f"{policy}.json"
    path.write_text(json.dumps({"repeat_policy": policy}), encoding="utf-8")
    return str(path)


def _visit(out, run):
    return os.path.join(str(out), "Subjects", run.rows[0]["subject_id"], run.rows[0]["session"])


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------- R4-PUB-01

def test_r4_pub_01_files_are_verified_copies(fake_exam, tmp_path):
    conv = fake_exam("diffusion", "DIF-01-GE-TENSOR-ADC-EADC")
    out = tmp_path / "Selected"
    run = conv.select(out, "--publish")
    assert run.code == 0
    visit = _visit(out, run)
    manifest = _read(os.path.join(visit, "selection.json"))
    assert manifest["files"]
    problems = []
    session_root = os.path.join(conv.root, "Niftis")
    for item in manifest["files"]:
        published = os.path.join(visit, *item["path"].split("/"))
        source = os.path.join(session_root, *item["source"].split("/"))
        if sha(published) != sha(source) or sha(published) != item["source_sha256"]:
            problems.append(f"{item['path']}: not a byte copy of its source")
        if item["path"].endswith(".nii.gz"):
            sidecar = _read(published[:-7] + ".json")
            src_sidecar = _read(source[:-7] + ".json")
            added = set(sidecar) - set(src_sidecar)
            if added != SELECTION_KEYS:
                problems.append(f"{item['path']}: sidecar adds {sorted(added)}")
            if any(sidecar[k] != v for k, v in src_sidecar.items()):
                problems.append(f"{item['path']}: sidecar changes a source key")
            if sidecar.get("source_nifti") != item["source"]:
                problems.append(f"{item['path']}: source_nifti {sidecar.get('source_nifti')!r}")
    assert not problems, problems


def test_r4_pub_01_every_evaluated_visit_is_published(fake_exam, tmp_path):
    """scan-selection.md 7.5 rule 1: a --publish run writes a folder for each evaluated session regardless of its
    outcomes, and a role still open simply has no files in its folder. HC-02's only role goes to review."""
    conv = fake_exam("hard_cases", "HC-02-DUPLICATE")
    out = tmp_path / "Selected"
    run = conv.select(out, "--publish")
    assert run.role_row("T1")["outcome"] == "review_required"
    assert os.path.exists(os.path.join(_visit(out, run), "selection.json")), \
        "no visit folder: the review-only visit was not published"


# ---------------------------------------------------------------------------- R4-PUB-02

def test_r4_pub_02_folder_without_receipt_is_never_replaced(fake_exam, tmp_path):
    conv = fake_exam("structural", "STR-03-SIEMENS-ESPREE-2D")
    out = tmp_path / "Selected"
    probe = conv.select(tmp_path / "probe")
    visit = _visit(out, probe)
    os.makedirs(visit)
    with open(os.path.join(visit, "mine.txt"), "w", encoding="utf-8") as f:
        f.write("a person's file\n")
    before = listing.list_tree(visit)
    run = conv.select(out, "--publish")
    assert run.code == 2
    assert listing.list_tree(visit) == before


def test_r4_pub_02_added_file_blocks_replacement(tmp_path):
    conv = e2e.build(tmp_path / "b", "hard_cases", "HC-03-REPEAT-CONSISTENT", "fake")
    out = tmp_path / "Selected"
    first = conv.select(out, "--publish", "--profile", _profile(tmp_path, "later"))
    assert first.code == 0
    visit = _visit(out, first)
    with open(os.path.join(visit, "added.txt"), "w", encoding="utf-8") as f:
        f.write("added\n")
    before = listing.list_tree(visit)
    second = conv.select(out, "--publish", "--profile", _profile(tmp_path, "earlier"))
    assert second.code == 2
    assert listing.list_tree(visit) == before


# ---------------------------------------------------------------------------- R4-PUB-03

def test_r4_pub_03_unchanged_kept_changed_replaced(tmp_path):
    conv = e2e.build(tmp_path / "b", "hard_cases", "HC-03-REPEAT-CONSISTENT", "fake")
    out = tmp_path / "Selected"
    first = conv.select(out, "--publish", "--profile", _profile(tmp_path, "later"))
    visit = _visit(out, first)
    receipt = os.path.join(visit, ".pipeline_complete.json")
    rid = _read(receipt)["receipt_id"]
    before = listing.list_tree(visit)
    again = conv.select(out, "--publish", "--profile", _profile(tmp_path, "later"))
    assert again.code == 0
    assert listing.list_tree(visit) == before and _read(receipt)["receipt_id"] == rid
    changed = conv.select(out, "--publish", "--profile", _profile(tmp_path, "earlier"))
    assert changed.code == 0
    assert _read(receipt)["receipt_id"] != rid
    t1 = changed.role_row("T1")
    published = _read(os.path.join(visit, "selection.json"))["roles"]["T1"]["candidate_id"]
    assert published == t1["candidate_id"] and changed.uids_of(published) == [3]


# ---------------------------------------------------------------------------- R4-PUB-04

def _no_leftovers(root):
    found = []
    for dirpath, dirs, _files in os.walk(root):
        found += [os.path.join(dirpath, d) for d in dirs if d.startswith(LEFTOVER_PREFIXES)]
    return found


def _intact(folder):
    inventory = _read(os.path.join(folder, ".inventory.json"))["files"]
    return all(os.path.isfile(os.path.join(folder, *i["name"].split("/"))) and
               sha(os.path.join(folder, *i["name"].split("/"))) == i["sha256"] for i in inventory)


def _visit_folders(out, bids):
    if bids:
        return [os.path.join(str(out), s, v) for s in os.listdir(out) if s.startswith("sub-")
                for v in os.listdir(os.path.join(str(out), s)) if v.startswith("ses-")]
    base = os.path.join(str(out), "Subjects")
    return [os.path.join(base, s, v) for s in os.listdir(base) for v in os.listdir(os.path.join(base, s))
            if not v.startswith(".")]


PUB_STEPS = ["staged", "moved-aside", "swapped", "previous-deleted"]
BIDS_FILE_STEPS = ["marker-written", "participants-written", "sessions-written", "bidsignore-written"]


@pytest.mark.parametrize("layout,step", [("default", s) for s in PUB_STEPS] + [("bids", s) for s in PUB_STEPS]
                         + [("bids", s) for s in BIDS_FILE_STEPS])
def test_r4_pub_04_interrupted_publication_is_finished(tmp_path, layout, step):
    """CUJ 4: a visit replaced because its decision changed (repeat_policy later -> earlier), killed
    after one write step; the next --publish leaves one complete folder, the new content, and no
    leftovers; BIDS dataset files agree with the committed visit."""
    bids = layout == "bids"
    conv = e2e.build(tmp_path / "b", "hard_cases", "HC-03-REPEAT-CONSISTENT", "fake")
    out = tmp_path / ("BIDS" if bids else "Selected")
    extra = ["--bids"] if bids else []
    if step in BIDS_FILE_STEPS[1:]:
        # participants.tsv, the sessions file and .bidsignore are rewritten only when their content
        # changes, which a first publication into an empty root does; it is the run interrupted here.
        pass
    else:
        first = conv.select(out, "--publish", *extra, "--profile", _profile(tmp_path, "later"))
        assert first.code == 0, first.result.stderr[-1500:]
    markers = tmp_path / "markers"
    argv = ["select", "--project-root", conv.root, "--output-root", str(out), "--publish", *extra,
            "--profile", _profile(tmp_path, "earlier")]
    result = crash.run_with_crash(argv, step, str(markers), cwd=conv.root)
    if not crash.fired(str(markers), step):
        pytest.fail(f"ERROR, not a finding: crash point {step} never fired (exit {result.returncode})")
    assert result.returncode == 137
    final = conv.select(out, "--publish", *extra, "--profile", _profile(tmp_path, "earlier"))
    assert final.code == 0, final.result.stderr[-1500:]
    folders = _visit_folders(out, bids)
    assert len(folders) == 1, folders
    assert not _no_leftovers(out), _no_leftovers(out)
    assert _intact(folders[0])
    manifest = _read(os.path.join(folders[0], ".selection.json" if bids else "selection.json"))
    assert final.uids_of(manifest["roles"]["T1"]["candidate_id"]) == [3]
    if bids:
        subject = final.rows[0]["subject_id"]
        session = final.rows[0]["session"]
        participants = open(os.path.join(out, "participants.tsv"), encoding="utf-8").read()
        sessions = open(os.path.join(out, f"sub-{subject}", f"sub-{subject}_sessions.tsv"), encoding="utf-8").read()
        assert f"sub-{subject}" in participants
        assert f"ses-{session}" in sessions
        assert _read(os.path.join(out, ".bids_export.json"))
        assert "mri-dicom-pipeline" in open(os.path.join(out, ".bidsignore"), encoding="utf-8").read()


# ---------------------------------------------------------------------------- R4-PUB-05

def test_r4_pub_05_select_never_writes_to_the_pipeline_folders(fake_exam, tmp_path):
    conv = fake_exam("structural", "STR-04-GE-SESSION-F-CONTRAST")
    roots = {name: os.path.join(conv.root, *name.split("/")) for name in ("DicomExtracted", "Niftis", "Reports/.state")}
    before = {n: listing.list_tree(p) for n, p in roots.items()}
    run = conv.select(tmp_path / "Selected", "--publish")
    assert run.code in (0, 2)
    problems = {}
    for name, path in roots.items():
        d = listing.diff(before[name], listing.list_tree(path), [".mri_dicom_pipeline.lock"])
        if listing.unexplained(d):
            problems[name] = d
    assert not problems, problems
