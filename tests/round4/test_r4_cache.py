"""TG-CAC: the selection cache and determinism (plan section 5, TG-CAC; R-CAC-1 to R-CAC-5).

Tier 2 projects (the tier 2 converter), and tier 3 for R4-CAC-04. Expected values: README
"Scan selection" and scan-selection.md 7.3, 7.4 and 10.1 case 40 (P). R4-CAC-01 counts file
opens in a child process that runs `mri_dicom_pipeline.cli.main(argv)` with open() wrapped
(plan section 1, "Instrumented in-process runs").
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from r4lib import dicom_builder, e2e, listing
from r4lib.project import R4Project

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

OPEN_LOGGER = r'''
import builtins, io, json, os, sys
log_path = sys.argv[1]
argv = sys.argv[2:]
_open, _os_open = builtins.open, os.open
seen = []
def note(path):
    try:
        seen.append(os.path.abspath(os.fspath(path)))
    except TypeError:
        pass
def hooked_open(file, *a, **k):
    if isinstance(file, (str, bytes, os.PathLike)):
        note(file)
    return _open(file, *a, **k)
def hooked_os_open(path, *a, **k):
    note(path)
    return _os_open(path, *a, **k)
builtins.open = io.open = hooked_open
os.open = hooked_os_open
from mri_dicom_pipeline import cli
code = cli.main(argv)
with _open(log_path, "w", encoding="utf-8") as f:
    json.dump(seen, f)
sys.exit(code if isinstance(code, int) else 0)
'''


def opened_paths(project, out, tmp_path, *args):
    log = tmp_path / "opens.json"
    proc = subprocess.run([sys.executable, "-c", OPEN_LOGGER, str(log), "select", "--project-root", project.root,
                           "--output-root", str(out), *args], capture_output=True, text=True, timeout=600)
    assert proc.returncode in (0, 2), proc.stderr[-2000:]
    return json.loads(log.read_text(encoding="utf-8"))


def test_r4_cac_01_cached_rerun_opens_no_scan_file(tmp_path):
    conv = e2e.build(tmp_path / "b", "diffusion", "DIF-01-GE-TENSOR-ADC-EADC", "fake")
    first = opened_paths(conv.project, tmp_path / "o1", tmp_path)
    # Extracted DICOM objects live below DicomExtracted/Subjects; the root holds only the lock files
    # (the lock owner record is written through a .tmp_ file and renamed, fsutil.atomic_json).
    dicom_root = os.path.normcase(os.path.join(conv.root, "DicomExtracted", "Subjects"))
    assert any(os.path.normcase(p).startswith(dicom_root) for p in first), \
        "the first run must read DICOM (control: the instrumentation sees opens)"
    second = opened_paths(conv.project, tmp_path / "o2", tmp_path)
    bad = [p for p in second if os.path.normcase(p).startswith(dicom_root)
           or p.lower().endswith((".nii", ".nii.gz", ".bval", ".bvec"))]
    assert not bad, [os.path.relpath(p, conv.root) for p in bad]


# ---------------------------------------------------------------------------- R4-CAC-02

def _cache_path(conv):
    paths = e2e.cache_files(conv)
    assert len(paths) == 1, paths
    return paths[0]


def _state(path):
    with open(path, "rb") as f:
        data = f.read()
    return os.stat(path).st_mtime_ns, hashlib.sha256(data).hexdigest()


def _profile(tmp_path, policy):
    path = tmp_path / f"profile_{policy}.json"
    path.write_text(json.dumps({"repeat_policy": policy}), encoding="utf-8")
    return str(path)


@pytest.fixture
def cac_project(tmp_path):
    conv = e2e.build(tmp_path / "b", "hard_cases", "HC-03-REPEAT-CONSISTENT", "fake")
    run = conv.select(tmp_path / "o0")
    assert run.code in (0, 2)
    return conv


def _rewritten(before, after):
    return before[0] != after[0]


def test_r4_cac_02_conversion_receipt_change_rebuilds(cac_project, tmp_path):
    conv = cac_project
    path = _cache_path(conv)
    before = _state(path)
    time.sleep(0.05)
    args, env = e2e.converter_args(tmp_path / "b", conv.exam, "fake")
    assert conv.project.run("convert", *args, "--force-reconvert", env=env).code == 0
    conv.select(tmp_path / "o1")
    assert _rewritten(before, _state(path))


def test_r4_cac_02_cache_version_change_rebuilds(cac_project, tmp_path):
    conv = cac_project
    path = _cache_path(conv)
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    doc["cache_version"] = -1
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    before = _state(path)
    time.sleep(0.05)
    conv.select(tmp_path / "o1")
    after = _state(path)
    assert _rewritten(before, after)
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["cache_version"] != -1


def test_r4_cac_02_audit_rebuilds(cac_project, tmp_path):
    conv = cac_project
    path = _cache_path(conv)
    before = _state(path)
    time.sleep(0.05)
    conv.select(tmp_path / "o1", "--audit")
    assert _rewritten(before, _state(path))


def test_r4_cac_02_profile_change_changes_decisions_not_the_cache(cac_project, tmp_path):
    conv = cac_project
    path = _cache_path(conv)
    before = _state(path)
    review = conv.select(tmp_path / "o1")
    later = conv.select(tmp_path / "o2", "--profile", _profile(tmp_path, "later"))
    assert review.role_row("T1")["outcome"] == "review_required"
    assert later.role_row("T1")["outcome"] in ("selected", "selected_fallback")
    assert _state(path) == before


def test_r4_cac_02_extraction_content_key_rebuilds(cac_project, tmp_path):
    """A cache whose recorded extraction content differs from the session's is rebuilt.

    Deviation from the plan's procedure, recorded in R4_GATES.md: a supported command cannot change an
    accepted session's extraction content (prepare holds a changed canonical ZIP: "Accepted canonical
    ZIP changed ... upload the new export as a new _N session", README), so the key is made to differ
    in the cache file instead, as for cache_version."""
    conv = cac_project
    path = _cache_path(conv)
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    real_key = doc["extraction_content_sha256"]
    doc["extraction_content_sha256"] = "0" * 64
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    before = _state(path)
    time.sleep(0.05)
    conv.select(tmp_path / "o1")
    assert _rewritten(before, _state(path))
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["extraction_content_sha256"] == real_key


def test_r4_cac_02_changed_canonical_zip_is_held(tmp_path):
    """The supported path for new content: a changed accepted ZIP is held by prepare (exit 2) and the
    session, its outputs and the selection cache stay as they were (README "Fast repeated runs")."""
    conv = e2e.build(tmp_path / "b", "structural", "STR-03-SIEMENS-ESPREE-2D", "fake")
    conv.select(tmp_path / "o0")
    path = _cache_path(conv)
    before = _state(path)
    session = conv.exam["sessions"][0]
    sorted_zip = next(os.path.join(d, f) for d, _s, fs in os.walk(os.path.join(conv.root, "DicomUploads"))
                      for f in fs if f.lower().endswith(".zip"))
    with open(sorted_zip, "wb") as f:
        f.write(dicom_builder.session_zip_bytes(conv.exam, session, session["series"][:-1]))
    assert conv.project.run("prepare", "--force-reextract").code == 2
    run = conv.select(tmp_path / "o1")
    assert _state(path) == before
    assert {r["outcome"] for r in run.rows} != {"source_not_ready"}


# ---------------------------------------------------------------------------- R4-CAC-03 / 04

RANDOM_KEYS = ("receipt_id", "run_id")


def _strip(value, extra=()):
    """Drop receipt and run identifiers by name (spec CUJ 5): keys named receipt_id or run_id, or
    ending in _receipt_id / _receipt_ids, which carry the same random identifiers; plus ``extra`` names."""
    if isinstance(value, dict):
        return {k: _strip(v, extra) for k, v in value.items()
                if k not in RANDOM_KEYS and k not in extra and not k.endswith(("_receipt_id", "_receipt_ids"))}
    if isinstance(value, list):
        return [_strip(v, extra) for v in value]
    return value


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def tree(root, extra=(), derived=False):
    """{relative path: comparable form}. JSON files lose the random identifiers (and ``extra`` names).

    With ``derived``, hashes that are computed from files holding an excluded value are checked against
    their own folder instead of across runs: each inventory entry's SHA-256 must match its file (the
    entry's name and size are still compared), and the receipt's selection_sha256 must be the SHA-256 of
    the folder's own manifest. A failed self-check is reported as a difference."""
    out = {}
    for dirpath, _d, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            data = open(full, "rb").read()
            if name.endswith(".json"):
                try:
                    doc = json.loads(data)
                except ValueError:
                    doc = None
                if doc is not None:
                    if derived and name == ".inventory.json":
                        bad = [i["name"] for i in doc["files"]
                               if _sha(os.path.join(dirpath, *i["name"].split("/"))) != i["sha256"]]
                        doc = {"files": [{"name": i["name"], "size": i["size"]} for i in doc["files"]],
                               "self_check_failures": bad}
                    if derived and name == ".pipeline_complete.json" and "selection_sha256" in doc:
                        manifest = next((os.path.join(dirpath, m) for m in ("selection.json", ".selection.json")
                                         if os.path.exists(os.path.join(dirpath, m))), None)
                        ok = manifest is not None and _sha(manifest) == doc.pop("selection_sha256")
                        doc["selection_sha256_self_check"] = ok
                    out[rel] = json.dumps(_strip(doc, extra), sort_keys=True)
                    continue
            out[rel] = hashlib.sha256(data).hexdigest()
    return out


def tree_differences(a, b, extra=(), derived=False):
    ta, tb = tree(a, extra, derived), tree(b, extra, derived)
    return sorted(set(ta) ^ set(tb)) + sorted(k for k in set(ta) & set(tb) if ta[k] != tb[k])


def test_r4_cac_03_two_runs_give_the_same_bytes(fake_exam, tmp_path):
    conv = fake_exam("structural", "STR-04-GE-SESSION-F-CONTRAST")
    a = conv.select(tmp_path / "a", "--publish")
    b = conv.select(tmp_path / "b", "--publish")
    assert a.code == b.code
    diffs = tree_differences(tmp_path / "a", tmp_path / "b")
    e2e.record("determinism", {"test": "R4-CAC-03", "differences": diffs})
    assert not diffs, diffs
    again = conv.select(tmp_path / "a", "--publish")
    assert again.code == a.code
    assert not tree_differences(tmp_path / "a", tmp_path / "b")


def _build_ordered(base, cat_name, exam_id, converter, reverse):
    from r4lib import catalog

    exam = next(e for e in catalog.load_file(cat_name)["exams"] if e["id"] == exam_id)
    if reverse:
        exam = json.loads(json.dumps(exam))
        for s in exam["sessions"]:
            s["member_order"] = "reverse"
    project = R4Project(os.path.join(str(base), "project"))
    project.place_exam(exam)
    assert project.run("prepare").code == 0
    args, env = e2e.converter_args(base, exam, converter)
    assert project.run("convert", *args, env=env).code == 0
    return e2e.Converted(project, exam, converter, [])


@pytest.mark.parametrize("converter", ["fake", pytest.param("real", marks=pytest.mark.r4real)])
def test_r4_cac_04_member_order_does_not_change_outputs(tmp_path, converter):
    if converter == "real" and not os.environ.get("MRI_PIPELINE_DCM2NIIX"):
        pytest.skip("set MRI_PIPELINE_DCM2NIIX")
    a = _build_ordered(tmp_path / "a", "structural", "STR-04-GE-SESSION-F-CONTRAST", converter, False)
    b = _build_ordered(tmp_path / "b", "structural", "STR-04-GE-SESSION-F-CONTRAST", converter, True)
    ra = a.select(tmp_path / "oa", "--publish")
    rb = b.select(tmp_path / "ob", "--publish")
    assert ra.code == rb.code
    # Owner decision on R4-F-014 (2026-11-25): source_archive_sha256, the hash of the input ZIP, is excluded
    # by name, because reordering the ZIP's members changes it by construction. The hashes computed from
    # files that hold it are self-checked instead (tree(derived=True)).
    diffs = tree_differences(tmp_path / "oa", tmp_path / "ob", extra=("source_archive_sha256",), derived=True)
    e2e.record("determinism", {"test": "R4-CAC-04", "converter": converter, "differences": diffs})
    assert not diffs, diffs


# ---------------------------------------------------------------------------- R4-CAC-05

def test_r4_cac_05_dry_run_writes_nothing(fake_exam, tmp_path):
    conv = fake_exam("structural", "STR-03-SIEMENS-ESPREE-2D")
    before = listing.list_tree(conv.root)
    out = tmp_path / "never"
    run = conv.select(out, "--dry-run", "--publish")
    assert run.code in (0, 2)
    after = listing.list_tree(conv.root)
    d = listing.diff(before, after)
    assert not listing.unexplained(d), d
    assert not out.exists()
