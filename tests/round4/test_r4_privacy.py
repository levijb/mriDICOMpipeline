"""TG-PRV: privacy and security (plan section 5, TG-PRV; R-PRV-1 to R-PRV-6).

The sentinel exam (HC-10, every identifying field set to an R4SENT_ value, plan 3.2) is uploaded
unnamed below Uploads/R4SENT_FOLDER_b1/S210/R4SENT_ZIP_x.zip, then processed by prepare and convert,
followed by `select --publish` and then `select --bids --publish`, with the tier 2 converter and (r4real) the real
dcm2niix. Expected values: README "Privacy" and plan 3.2's exceptions (P); the scan itself is the
testing agent's (C).
"""

from __future__ import annotations

import csv
import json
import os
import re

import pytest

from r4lib import catalog, e2e, listing, sentinels
from r4lib.project import R4Project

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
UPLOAD = "Uploads/R4SENT_FOLDER_b1/S210/R4SENT_ZIP_x.zip"
README = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "README.md")


def _sentinel_exam():
    exam = json.loads(json.dumps(next(e for e in catalog.load_file("hard_cases")["exams"] if e["id"] == "HC-10-SENTINELS")))
    session = exam["sessions"][0]
    session.pop("zip_name", None)
    session["upload_path"] = UPLOAD
    return exam


def run_sentinel_project(base, converter):
    exam = _sentinel_exam()
    project = R4Project(os.path.join(str(base), "project"))
    project.place_exam(exam)
    steps = {"prepare": project.run("prepare").code}
    args, env = e2e.converter_args(base, exam, converter)
    steps["convert"] = project.run("convert", *args, env=env).code
    selected, bids_root = os.path.join(str(base), "Selected"), os.path.join(str(base), "BIDS")
    steps["select"] = project.run("select", "--output-root", selected, "--publish", "--extras").code
    steps["bids"] = project.run("select", "--output-root", bids_root, "--bids", "--publish").code
    return project, selected, bids_root, steps


@pytest.fixture(scope="module", params=["fake", pytest.param("real", marks=pytest.mark.r4real)])
def sentinel_run(request, tmp_path_factory):
    if request.param == "real" and not os.environ.get("MRI_PIPELINE_DCM2NIIX"):
        pytest.skip("set MRI_PIPELINE_DCM2NIIX")
    base = tmp_path_factory.mktemp("prv-" + request.param)
    return run_sentinel_project(base, request.param)


def _new_feature_files(project, selected, bids_root):
    """Every file the new features write (R-PRV-1), except NIfTI images (README: NIfTI header text)."""
    out = {}
    for root, tag in ((selected, "Selected"), (bids_root, "BIDS")):
        for dirpath, _d, files in os.walk(root):
            for f in files:
                if not f.endswith((".nii.gz", ".nii")):
                    out[f"{tag}/" + os.path.relpath(os.path.join(dirpath, f), root).replace(os.sep, "/")] = \
                        os.path.join(dirpath, f)
    reports = os.path.join(project.root, "Reports")
    for dirpath, _d, files in os.walk(os.path.join(reports, ".selection_cache")):
        for f in files:
            out["Reports/.selection_cache/" + f] = os.path.join(dirpath, f)
    for name in os.listdir(reports):
        # latest_convert.log belongs to convert (rounds 1-3), a README-listed channel; its content is
        # reported by finding R4-F-017, not counted here.
        if name == "latest_convert.log":
            continue
        if name in (".naming_cache.json", "renamed_uploads.csv") or name.endswith(".log") or ".log." in name:
            out["Reports/" + name] = os.path.join(reports, name)
    return out


def test_r4_prv_01_no_sentinel_in_new_outputs(sentinel_run):
    project, selected, bids_root, steps = sentinel_run
    assert steps["prepare"] == 0 and steps["convert"] == 0, steps
    files = _new_feature_files(project, selected, bids_root)
    assert any(k.startswith("Selected/") for k in files) and any(k.startswith("BIDS/") for k in files)
    hits = {}
    for rel, path in files.items():
        found = sentinels.scan_file(path, sentinels.needles_for_prv01())
        if found:
            hits[rel] = found
    e2e.record("sentinel_scan", {"test": "R4-PRV-01", "files": len(files), "hits": hits})
    assert not hits, hits


ALLOWED_PATH_CHANNELS = ("Reports/renamed_uploads.csv", "Reports/issues.csv", "Reports/.state/")


def test_r4_prv_02a_upload_names_only_in_listed_channels(sentinel_run):
    project, selected, bids_root, _steps = sentinel_run
    needles = ["R4SENT_FOLDER", "R4SENT_ZIP"]
    hits = {}
    for root, tag in ((project.root, ""), (selected, "Selected/"), (bids_root, "BIDS/")):
        for rel, found in sentinels.scan_tree(root, needles).items():
            if tag == "" and rel.startswith(("DicomUploads/", "Selected/", "BIDS/")):
                continue
            hits[tag + rel] = found
    unlisted = {k: v for k, v in hits.items()
                if not (k.startswith(ALLOWED_PATH_CHANNELS) or re.match(r"Reports/[^/]*\.log(\.\d+)?$", k))}
    e2e.record("sentinel_scan", {"test": "R4-PRV-02a", "hits": hits, "unlisted": unlisted})
    assert "Reports/renamed_uploads.csv" in hits, hits
    assert not unlisted, unlisted
    text = open(README, encoding="utf-8").read()
    privacy = text[text.index("## Privacy"):text.index("## Where to look")]
    for channel in ("renamed_uploads.csv", "issues.csv", "state files", "logs"):
        assert channel in privacy, channel


def test_r4_prv_02b_dataset_name_is_listed_or_reported(sentinel_run):
    """dataset_description.json Name carries the project folder's name, which the user did not give
    to the export: listed in the README privacy section, or a finding."""
    _project, _selected, bids_root, _steps = sentinel_run
    desc = json.load(open(os.path.join(bids_root, "dataset_description.json"), encoding="utf-8"))
    name = desc.get("Name", "")
    text = open(README, encoding="utf-8").read()
    privacy = text[text.index("## Privacy"):text.index("## Where to look")]
    e2e.record("sentinel_scan", {"test": "R4-PRV-02b", "name_is_project_folder": name == "project"})
    if name == "project":
        assert "dataset_description" in privacy or "project folder" in privacy.lower(), \
            "dataset_description.json Name holds the project folder name; the README privacy section does not list it"


# ---------------------------------------------------------------------------- R4-PRV-03

def _fresh(tmp_path):
    return e2e.build(tmp_path / "b", "structural", "STR-03-SIEMENS-ESPREE-2D", "fake")


def _first(root, suffix):
    return next(os.path.join(d, f) for d, _s, fs in os.walk(root) for f in sorted(fs) if f.endswith(suffix)
                and not f.startswith("."))


@pytest.mark.parametrize("damage", ["unparseable_sidecar", "truncated_nifti", "unreadable_dicom"])
def test_r4_prv_03_malformed_session_is_never_used(tmp_path, damage):
    conv = _fresh(tmp_path)
    niftis = os.path.join(conv.root, "Niftis", "Subjects")
    if damage == "unparseable_sidecar":
        path = _first(niftis, ".json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
    elif damage == "truncated_nifti":
        path = _first(niftis, ".nii.gz")
        data = open(path, "rb").read()
        with open(path, "wb") as f:
            f.write(data[:200])
    else:
        dicom = os.path.join(conv.root, "DicomExtracted", "Subjects")
        path = next(os.path.join(d, f) for d, _s, fs in os.walk(dicom) for f in sorted(fs)
                    if not f.startswith(".") and not f.endswith(".json"))
        with open(path, "r+b") as f:
            f.seek(0)
            f.write(b"\0" * 64 + b"NOT DICOM" * 16)
    run = conv.select(tmp_path / "o")
    outcomes = {r["outcome"] for r in run.rows}
    reasons = {r["reasons"] for r in run.rows}
    e2e.record("malformed", {"damage": damage, "outcomes": sorted(outcomes), "reasons": sorted(reasons)})
    assert outcomes == {"source_not_ready"}, (outcomes, reasons)
    assert all(r.startswith("metadata_read_failed") for r in reasons), reasons


# ---------------------------------------------------------------------------- R4-PRV-04

CI = os.path.join(os.path.dirname(README), ".github", "workflows", "ci.yml")


def test_r4_prv_04_ci_downloads_are_checksum_verified():
    text = open(CI, encoding="utf-8").read()
    assert "digest != '${{ matrix.sha256 }}'" in text, "dcm2niix download not verified"
    assert "entry['archive_sha256']" in text, "vendor dataset download not verified"
    uses = sorted(set(re.findall(r"uses:\s*([^\s]+)", text)))
    pinned = {u: bool(re.search(r"@[0-9a-f]{40}$", u)) for u in uses}
    e2e.record("ci_actions", {"test": "R4-PRV-04", "actions": pinned})


@pytest.mark.r4slow
def test_r4_prv_04_pip_audit(tmp_path):
    pytest.importorskip("pip_audit")
    import subprocess
    import sys

    out = tmp_path / "audit.json"
    # pip-audit verifies TLS with certifi's bundle; pip verifies with the operating system's store
    # (truststore). On this machine only the latter verifies pypi.org, so the store pip uses is injected.
    code = 'import sys\ntry:\n    import pip._vendor.truststore as ts\n    ts.inject_into_ssl()\nexcept Exception:\n    pass\nfrom pip_audit._cli import audit\n'
    code += f"sys.argv = ['pip-audit', '--format', 'json', '--output', {str(out)!r}]\naudit()\n"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=900)
    report = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    deps = report.get("dependencies", [])
    vulns = {d["name"]: [v["id"] for v in d.get("vulns", [])] for d in deps if d.get("vulns")}
    e2e.record("pip_audit", {"exit": proc.returncode, "packages": len(deps), "vulnerable": vulns,
                             "skipped": [d["name"] for d in deps if d.get("skip_reason")]})
    assert deps, proc.stderr[-1500:]
    assert not vulns, vulns


# ---------------------------------------------------------------------------- R4-PRV-05

@pytest.fixture
def contained(fake_exam, tmp_path):
    conv = fake_exam("structural", "STR-03-SIEMENS-ESPREE-2D")
    parent = tmp_path / "outer"
    out = parent / "Selected"
    parent.mkdir()
    return conv, parent, out


def _check_refused(conv, parent, out, *args):
    before = listing.list_tree(str(parent))
    run = conv.select(out, "--publish", *args)
    after = listing.list_tree(str(parent))
    d = listing.diff(before, after, ["Selected/.mri_dicom_pipeline.lock"])
    return run.code, listing.unexplained(d)


def test_r4_prv_05_subject_list_traversal(contained, tmp_path):
    conv, parent, out = contained
    subjects = tmp_path / "subjects.csv"
    subjects.write_text("subject_id\n..\\..\\x\n", encoding="utf-8")
    code, changed = _check_refused(conv, parent, out, "--subjects", str(subjects))
    assert code == 2 and not changed, (code, changed)


def test_r4_prv_05_override_with_path_like_candidate(contained, tmp_path):
    conv, parent, out = contained
    ov = tmp_path / "ov.csv"
    ov.write_text("subject_id,session,role,action,candidate_id\nS103,20260802,T1,choose,../../../x\n", encoding="utf-8")
    code, changed = _check_refused(conv, parent, out, "--overrides", str(ov))
    assert code == 2 and not changed, (code, changed)


def test_r4_prv_05_profile_with_traversal_name(contained, tmp_path):
    conv, parent, out = contained
    profile = tmp_path / "p.json"
    profile.write_text(json.dumps({"name": "../../x"}), encoding="utf-8")
    code, changed = _check_refused(conv, parent, out, "--profile", str(profile))
    assert code == 2 and not changed, (code, changed)


# ---------------------------------------------------------------------------- R4-PRV-06

def test_r4_prv_06_pixel_sampler_output_holds_no_sentinel(sentinel_run, capsys):
    import pydicom

    from oracles import pixel_sampler

    project, _selected, _bids, _steps = sentinel_run
    exam = _sentinel_exam()
    dicom = os.path.join(project.root, "DicomExtracted", "Subjects")
    by_series = {}
    for dirpath, _d, files in os.walk(dicom):
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                ds = pydicom.dcmread(p, stop_before_pixels=True, specific_tags=["SeriesInstanceUID"])
            except Exception:  # noqa: BLE001 - receipts and inventories
                continue
            by_series.setdefault(str(ds.get("SeriesInstanceUID", "")), []).append(p)
    niftis = {}
    for dirpath, _d, files in os.walk(os.path.join(project.root, "Niftis", "Subjects")):
        for f in files:
            if f.endswith(".json") and not f.startswith("."):
                side = json.load(open(os.path.join(dirpath, f), encoding="utf-8"))
                niftis[side.get("source_series_key")] = os.path.join(dirpath, f[:-5] + ".nii.gz")
    text = ""
    for series in exam["sessions"][0]["series"]:
        uid = e2e.dicom_builder.series_uid(exam["uid_exam"], series["uid_series"])
        key = e2e.series_key(exam, series["uid_series"])
        if key in niftis and uid in by_series:
            result = pixel_sampler.sample_series(sorted(by_series[uid]), [{"path": niftis[key], "echo_ms": None}],
                                                 "prv06-" + str(series["uid_series"]))
            text += json.dumps(result, default=str)
    text += capsys.readouterr().out
    assert text
    forbidden = ("R4SENT_", sentinels.UID_ROOT, sentinels.DATE_SENTINEL)
    assert not [f for f in forbidden if f in text]
