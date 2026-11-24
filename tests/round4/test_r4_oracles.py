"""TG-OR: oracle self-checks (plan section 5, TG-OR; oracle rules 5 and 6).

Every expected value here comes from the hand-case files in expected/hand_cases/ or from the
catalog in expected/catalog/, never from the pipeline.
"""

from __future__ import annotations

import ast
import json
import math
import os

import pytest

from r4lib import catalog, dicom_builder, facts as facts_cmp
from oracles import geometry, gradients, objective_checks

HERE = os.path.dirname(os.path.abspath(__file__))
HAND = os.path.join(HERE, "expected", "hand_cases")


def _load(name):
    with open(os.path.join(HAND, name), encoding="utf-8") as f:
        return json.load(f)


def _close(a, b, tol):
    return all(abs(float(x) - float(y)) <= tol for x, y in zip(a, b)) and len(a) == len(b)


# ---------------------------------------------------------------------------- independence

def _imports(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_oracles_import_no_pipeline_module():
    """Oracle rule 3: oracle code (and the header reader) never imports a pipeline module."""
    from conftest import header_reader_path

    paths = [os.path.join(HERE, "oracles", n) for n in os.listdir(os.path.join(HERE, "oracles")) if n.endswith(".py")]
    paths.append(header_reader_path())
    for path in paths:
        if os.path.getsize(path) == 0:
            continue
        bad = {n for n in _imports(path) if n == "mri_dicom_pipeline" or n.startswith("mri_dicom_pipeline.")}
        assert not bad, f"{path} imports {bad}"


# ---------------------------------------------------------------------------- R4-OR-01

def test_r4_or_01_geometry_hand_cases():
    cases = _load("geometry.json")
    tol = cases["tolerance_mm"]
    for case in cases["pixel_to_patient"]:
        p = geometry.pixel_to_patient(case["ipp"], case["iop"], case["pixel_spacing"], case["row"], case["col"])
        assert _close(p, case["expected_lps"], tol), case["id"]
        assert _close(geometry.lps_to_ras(p), case["expected_ras"], tol), case["id"]
        assert _close(geometry.slice_normal(case["iop"]), case["expected_normal"], 1e-9), case["id"]
        assert geometry.orientation_label(case["iop"]) == case["expected_orientation"], case["id"]
    for case in cases["stacks"]:
        got = geometry.stack_facts(case["ipps"], case["iop"], case["thick_mm"])
        for key, value in case["expected"].items():
            if isinstance(value, bool):
                assert got[key] is value, (case["id"], key)
            else:
                assert abs(got[key] - value) <= tol, (case["id"], key, got[key])
    for case in cases["nifti_axes"]:
        got = geometry.dicom_axes_in_nifti(case["affine"], case["iop"])
        for key in ("col_index_axis", "row_index_axis", "slice_axis"):
            assert got[key] == case["expected"][key], (case["id"], key)
        assert geometry.nifti_slice_count(case["shape"], case["affine"], case["iop"]) == case["expected"]["slice_count"]


def test_r4_or_01_native_voxel_hand_cases():
    cases = _load("native_voxel.json")
    tol = cases["tolerance_mm"]
    for case in cases["cases"]:
        got = geometry.native_voxel(case["rows"], case["cols"], case["pixel_spacing"], case["acq_matrix"],
                                    case["phase_dir"], case["thick_mm"])
        if case.get("expected_both"):
            for side in ("if_row", "if_col"):
                for axis in ("row", "col"):
                    assert abs(got[side][axis] - case["expected_both"][side][axis]) <= tol, (case["id"], side, axis)
            continue
        if case["expected"] is None:
            assert got is None, case["id"]
            continue
        exp = case["expected"]
        assert abs(got["row"] - exp["row"]) <= tol and abs(got["col"] - exp["col"]) <= tol, case["id"]
        assert abs(geometry.native_max(got) - exp["max"]) <= tol, case["id"]
        assert geometry.interpolated_grid(case["pixel_spacing"], got) is exp["interpolated_grid"], case["id"]


def test_r4_or_01_header_reader_native_sampling(header_reader):
    """The header reader's own 5.3 native sampling reproduces the same hand cases."""
    cases = _load("native_voxel.json")
    tol = cases["tolerance_mm"]
    for case in cases["cases"]:
        out, _ = header_reader.native_sampling(case["rows"], case["cols"], case["pixel_spacing"],
                                               case["acq_matrix"], case["phase_dir"], case["thick_mm"], None)
        if case.get("expected_both"):
            assert abs(out["native_row_if_row"] - case["expected_both"]["if_row"]["row"]) <= tol
            assert abs(out["native_col_if_row"] - case["expected_both"]["if_row"]["col"]) <= tol
            assert abs(out["native_row_if_col"] - case["expected_both"]["if_col"]["row"]) <= tol
            assert abs(out["native_col_if_col"] - case["expected_both"]["if_col"]["col"]) <= tol
            assert out["native_row_mm"] is None and out["native_inplane"] == "both_assignments"
            continue
        if case["expected"] is None:
            assert out["native_row_mm"] is None and out["native_inplane"] == "unknown", case["id"]
            continue
        exp = case["expected"]
        assert abs(out["native_row_mm"] - exp["row"]) <= tol and abs(out["native_col_mm"] - exp["col"]) <= tol, case["id"]
        assert abs(out["native_max_mm"] - exp["max"]) <= tol, case["id"]
        assert out["interpolated_grid"] is exp["interpolated_grid"], case["id"]


def test_r4_or_01_header_reader_orientation(header_reader):
    for case in _load("geometry.json")["pixel_to_patient"]:
        assert header_reader.orientation_label(case["iop"]) == case["expected_orientation"], case["id"]


# ---------------------------------------------------------------------------- R4-OR-02

def test_r4_or_02_gradient_hand_cases():
    for case in _load("gradients.json")["cases"]:
        got = gradients.gradient_stats(case["bvals"], [tuple(v) for v in case["bvecs"]], case["dim4"])
        for key, value in case["expected"].items():
            assert got[key] == value, (case["id"], key, got[key])


def test_r4_or_02_gradient_file_round_trip(tmp_path):
    """The .bval/.bvec readers parse the FSL text layout (one row of b-values; three rows of
    vector components)."""
    case = _load("gradients.json")["cases"][0]
    (tmp_path / "x.bval").write_text(" ".join(str(b) for b in case["bvals"]) + "\n", encoding="ascii")
    rows = [" ".join(str(v[i]) for v in case["bvecs"]) for i in range(3)]
    (tmp_path / "x.bvec").write_text("\n".join(rows) + "\n", encoding="ascii")
    got = gradients.stats_from_files(tmp_path / "x.bval", tmp_path / "x.bvec", case["dim4"])
    assert got["n_dirs"] == case["expected"]["n_dirs"] and got["b0"] == case["expected"]["b0"]


# ---------------------------------------------------------------------------- R4-OR-04

def _oc_cases():
    data = _load("objective_checks.json")
    return [pytest.param(data, c, id=c["id"]) for c in data["cases"]]


@pytest.mark.parametrize("data,case", _oc_cases())
def test_r4_or_04_objective_checks_flag_planted_choices(data, case):
    import copy

    records = copy.deepcopy(data["base_records"])
    for key, spec in case.get("add", {}).items():
        rec = copy.deepcopy(records[spec["from"]])
        rec.update({k: v for k, v in spec.items() if k != "from"})
        records[key] = rec
    for key, changes in case.get("set", {}).items():
        records[key].update(changes)
    members = [records[k] for k in case["members"]]
    flags = objective_checks.check_choice(case["role"], members, list(records.values()),
                                          volume=case.get("volume"), extent_mm=case.get("extent_mm"),
                                          chosen_by_override=case.get("chosen_by_override", False))
    assert sorted({f["category"] for f in flags}) == sorted(case["expected_categories"]), (case["id"], flags)


def test_r4_or_04_family_bands_at_the_5_4_bounds():
    """The objective checks' own 5.4 family code at the numeric bounds of T1_3D_IRGRE (TI 300 and
    1300, TE 10, TR below 3000), written from the 5.4 table text."""
    base = {"mr_acq": "3D", "scan_seq": ["GR"], "seq_var": ["SS"], "scan_opt": [], "tr_ms": 6.0, "te_ms": 2.4, "ti_ms": 450.0}
    def fam(**kw):
        return objective_checks.families({**base, **kw})
    assert "T1_3D_IRGRE" in fam(ti_ms=300.0) and "T1_3D_IRGRE" not in fam(ti_ms=299.9)
    assert "T1_3D_IRGRE" in fam(ti_ms=1300.0) and "T1_3D_IRGRE" not in fam(ti_ms=1300.1)
    assert "T1_3D_IRGRE" in fam(te_ms=10.0) and "T1_3D_IRGRE" not in fam(te_ms=10.1)
    assert "T1_3D_IRGRE" in fam(tr_ms=2999.9) and "T1_3D_IRGRE" not in fam(tr_ms=3000.0)
    # Rules revision 2: TI without IR still prepares; RM counts as gradient echo.
    assert "T1_3D_IRGRE" in fam(scan_seq=["RM"])
    # Not prepared, single echo, short TR: T1_3D_GRE (TI 0 is no inversion).
    assert fam(ti_ms=0.0) == ["T1_3D_GRE"]
    # Philips TFE: GR, MP, no TI, TR one shot's.
    assert "T1_3D_IRGRE" in fam(ti_ms=None, seq_var=["MP"], tr_ms=8.0)


# ---------------------------------------------------------------------------- pixel sampler (R4-OR-06, R4-OR-07)

def _ps_stack(tmp_path, volumes=1, ge_pepolar_mode=None):
    """Build the pixel_sampler.json stack as DICOM; returns (paths, data[vol][k] arrays). With
    ``ge_pepolar_mode`` the series is marked as GE epi_pepolar with that UserData12 mode."""
    import numpy as np
    import pydicom

    spec = _load("pixel_sampler.json")["stack"]
    exam = {"uid_exam": 9900}
    session = {"study_date": "20260725"}
    series = {"uid_series": 1, "slices": spec["slices"], "volumes": volumes, "echoes": [10.0], "parts": ["mag"],
              "geometry": {"iop": spec["iop"], "first_position": spec["first_position"], "step_mm": spec["step_mm"],
                           "pixel_spacing": spec["pixel_spacing"], "rows": spec["rows"], "cols": spec["cols"]},
              "tags": {"SeriesNumber": 1, "Manufacturer": "R4", "MRAcquisitionType": "2D", "AcquisitionTime": "090000"}}
    if ge_pepolar_mode is not None:
        series["tags"]["Manufacturer"] = "GE MEDICAL SYSTEMS"
        series["private_tags"] = [
            {"tag": "0019,109C", "creator": "GEMS_ACQU_01", "vr": "LO", "value": "epi_pepolar"},
            {"tag": "0019,10B3", "creator": "GEMS_ACQU_01", "vr": "DS", "value": str(ge_pepolar_mode)}]
    paths = dicom_builder.write_series(str(tmp_path / "dicom"), exam, session, series)
    data = [[None] * spec["slices"] for _ in range(volumes)]
    for path in paths:
        ds = pydicom.dcmread(path)
        inst = int(ds.InstanceNumber) - 1
        data[inst // spec["slices"]][inst % spec["slices"]] = ds.pixel_array.astype(np.int16)
    return paths, data


def _ps_nifti(tmp_path, data, variant):
    import nibabel as nib
    import numpy as np

    affine = np.array(_load("pixel_sampler.json")["affine"], dtype=float)
    vols = [np.stack([sl.T for sl in vol], axis=2) for vol in data]  # nifti[i, j, k] = dicom_k[j, i]
    arr = vols[0] if len(vols) == 1 else np.stack(vols, axis=3)
    if variant.startswith("flip axis"):
        arr = np.flip(arr, axis=int(variant[-1])).copy()
    if variant.startswith("affine shifted"):
        affine = affine.copy()
        affine[0, 3] += 0.5 * affine[0, 0]
    if variant.startswith("4D"):
        arr = arr[..., ::-1].copy()
    img = nib.Nifti1Image(arr.astype(np.int16), affine)
    if variant.startswith("scaled"):
        img = nib.Nifti1Image(arr.astype(np.int16), affine)
        img.header.set_slope_inter(2.0, 10.0)
    path = tmp_path / f"img_{variant.replace(' ', '_').replace(':', '').replace(',', '')}.nii"
    nib.save(img, str(path))
    return str(path)


def test_r4_or_07_sampler_hand_point():
    from oracles import pixel_sampler
    import numpy as np

    hand = _load("pixel_sampler.json")
    spec, pt = hand["stack"], hand["point"]
    ipp = [spec["first_position"][0], spec["first_position"][1], spec["first_position"][2] + pt["k"] * spec["step_mm"]]
    ras = pixel_sampler.world_ras(ipp, spec["iop"], spec["pixel_spacing"], pt["r"], pt["c"])
    assert np.allclose(ras, pt["expected_ras"])
    vox = np.linalg.inv(np.array(hand["affine"], dtype=float)) @ np.append(ras, 1.0)
    assert np.allclose(vox[:3], pt["expected_voxel"])


@pytest.mark.parametrize("case", _load("pixel_sampler.json")["cases"], ids=lambda c: c["id"])
def test_r4_or_07_sampler_hand_cases(tmp_path, case):
    from oracles import pixel_sampler

    volumes = 2 if case["nifti"].startswith("4D") else 1
    paths, data = _ps_stack(tmp_path, volumes, case.get("ge_pepolar_mode"))
    nifti = _ps_nifti(tmp_path, data, case["nifti"])
    result = pixel_sampler.sample_series(paths, [{"path": nifti, "echo_ms": None}], "hand-" + case["id"])
    assert result["status"] == case["expected_status"], (case["id"], result)


R4_OR_07_SERIES = [("structural", "STR-01-GE-PREMIER-CENSUS", 3), ("geometry", "GEO-04-OBLIQUE-30", 3), ("structural", "STR-03-SIEMENS-ESPREE-2D", 3)]


@pytest.mark.r4real
@pytest.mark.parametrize("cat_name,exam_id,uid_series", R4_OR_07_SERIES)
def test_r4_or_07_sampler_flip_self_test(tmp_path, r4_dcm2niix, cat_name, exam_id, uid_series):
    """Plan 3.6 self-test: on a real dcm2niix conversion (-l o) of a catalog series, the unflipped
    image matches and copies flipped along each axis, header and affine kept, mismatch. The
    converter is run directly; no pipeline command runs."""
    import glob
    import subprocess

    import nibabel as nib
    import numpy as np
    from oracles import pixel_sampler

    data = catalog.load_file(cat_name)
    exam = next(e for e in data["exams"] if e["id"] == exam_id)
    session = exam["sessions"][0]
    series = next(s for s in session["series"] if s["uid_series"] == uid_series)
    paths = dicom_builder.write_series(str(tmp_path / "dicom"), exam, session, series)
    out = tmp_path / "out"
    out.mkdir()
    subprocess.run([r4_dcm2niix, "-l", "o", "-z", "n", "-b", "y", "-f", "%s", "-o", str(out), str(tmp_path / "dicom")],
                   check=True, capture_output=True)
    niftis = sorted(glob.glob(str(out / "*.nii")))
    assert len(niftis) == 1, niftis
    key = "or07-" + exam_id
    base = pixel_sampler.sample_series(paths, [{"path": niftis[0], "echo_ms": None}], key)
    assert base["status"] == "matched", base
    img = nib.load(niftis[0])
    raw = np.asanyarray(img.dataobj.get_unscaled())
    for axis in range(3):
        flipped = nib.Nifti1Image(np.flip(raw, axis=axis).copy(), img.affine, img.header)
        path = tmp_path / f"flip{axis}.nii"
        nib.save(flipped, str(path))
        got = pixel_sampler.sample_series(paths, [{"path": str(path), "echo_ms": None}], key)
        assert got["status"] == "mismatched", (axis, got)


@pytest.fixture(scope="module")
def calibration_report(header_reader):
    """R4-OR-05/06 over the D-13 datasets (owner's choice, 2026-11-24). Skips when none is
    present (CI's calibration job fetches them at the pinned commits). With R4_EVIDENCE_DIR set,
    the coded report is written there as calibration.json."""
    from oracles import pixel_sampler
    from r4lib import calibration

    if not any(calibration.dataset_root(e) for e in calibration.spec()["datasets"]):
        pytest.skip("D-13 vendor datasets not present (R4_VENDOR_DIR or D:\\round4\\vendor_datasets)")
    report = calibration.run(header_reader, pixel_sampler)
    evidence = os.environ.get("R4_EVIDENCE_DIR")
    if evidence:
        with open(os.path.join(evidence, "calibration.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(report, f, indent=1, default=str)
    return report


@pytest.mark.r4calib
def test_r4_or_05_header_reader_on_vendor_datasets(calibration_report):
    """Every compared field equal, apart from the differences the owner accepted by name at G1."""
    from r4lib import calibration

    assert not calibration.reader_mismatches(calibration_report)
    accepted = calibration.accepted_reader_differences()
    seen = {(item["repo"], ref["stem"], field) for item in calibration_report for ref in item["reader"]
            if ref["status"] == "compared" for field, res in ref["results"].items() if res == "mismatch"}
    assert seen == accepted, "an accepted difference no longer occurs, or a new one appeared"


@pytest.mark.r4calib
def test_r4_or_06_sampler_on_vendor_reference_conversions(calibration_report):
    """Every series matched, under-sampled or not compared, except the named expectation (polar
    series 7: 'known GE row reversal', owner decision at G1)."""
    from r4lib import calibration

    assert not calibration.sampler_failures(calibration_report)


# ---------------------------------------------------------------------------- R4-OR-08 listing

def test_r4_or_08_listing_reports_exactly_the_planted_changes(tmp_path):
    import time

    from r4lib import listing

    root = tmp_path / "proj"
    for rel in ("Reports/pipeline.log", "Reports/run_history.csv", "Niftis/Subjects/S1/20260725/T1/a.nii.gz",
                "DicomExtracted/Subjects/S1/20260725/dicom/0001", "keep.txt", "edit.txt", "gone.txt"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    before = listing.list_tree(str(root))
    assert set(before["session_dirs"]) == {"DicomExtracted/Subjects/S1/20260725", "Niftis/Subjects/S1/20260725"}
    time.sleep(0.05)
    (root / "edit.txt").write_text("changed", encoding="utf-8")
    (root / "gone.txt").unlink()
    (root / "added.txt").write_text("new", encoding="utf-8")
    (root / "Reports" / "pipeline.log").write_text("more log", encoding="utf-8")       # allowed
    (root / "Reports" / ".selection_cache" / "S1").mkdir(parents=True)
    (root / "Reports" / ".selection_cache" / "S1" / "20260725.json").write_text("{}", encoding="utf-8")  # allowed
    (root / "Niftis" / "Subjects" / "S1" / "20260725" / "extra.txt").write_text("x", encoding="utf-8")   # session dir time
    (root / "Reports" / ".mri_dicom_pipeline.lock.owner").write_text("pid", encoding="utf-8")            # forbidden
    after = listing.list_tree(str(root))
    allow = json.load(open(os.path.join(HERE, "expected", "allowlist_ops1.json"), encoding="utf-8"))
    d = listing.diff(before, after, allow["allow"], allow["forbidden_after_normal_end"])
    assert d["added"] == ["Niftis/Subjects/S1/20260725/extra.txt", "Reports/.mri_dicom_pipeline.lock.owner", "added.txt"]
    assert d["removed"] == ["gone.txt"]
    assert d["changed"] == ["edit.txt"]
    assert d["session_dirs_changed"] == ["Niftis/Subjects/S1/20260725"]
    assert d["forbidden_present"] == ["Reports/.mri_dicom_pipeline.lock.owner"]
    assert sorted(d["allowed"]) == ["Reports/.selection_cache/S1/20260725.json", "Reports/pipeline.log"]
    coded = listing.coded(d, lambda p: p.replace("S1/20260725", "V001"))
    assert "Niftis/Subjects/V001" in coded["session_dirs_changed"]


# ---------------------------------------------------------------------------- R4-OR-09 sentinels

def test_r4_or_09_sentinel_scanner_finds_planted_values(tmp_path):
    import gzip

    import nibabel as nib
    import numpy as np

    from r4lib import sentinels

    s_name = dicom_builder.sentinel_value("PatientName")
    s_acc = dicom_builder.sentinel_value("AccessionNumber")
    uid = dicom_builder.series_uid(42, 7)
    (tmp_path / "a.json").write_text(json.dumps({"x": s_name}), encoding="utf-8")
    (tmp_path / "b.csv").write_text(f"k,v\n1,{uid}\n", encoding="utf-8")
    (tmp_path / "c.tsv").write_text(f"participant_id\t{s_acc}\n", encoding="utf-8")
    (tmp_path / "d.log").write_text("line R4SENT_FOLDER and R4SENT_ZIP\n", encoding="utf-8")
    img = nib.Nifti1Image(np.zeros((2, 2, 2), dtype=np.int16), np.eye(4))
    img.header["descrip"] = dicom_builder.sentinel_value("StudyDescription")[:79].encode()
    nib.save(img, str(tmp_path / "e.nii.gz"))
    (tmp_path / "clean.txt").write_text("nothing here", encoding="utf-8")
    hits = sentinels.scan_tree(str(tmp_path), sentinels.FIELD_SENTINELS + sentinels.PATH_SENTINELS + (sentinels.UID_ROOT,))
    assert hits["a.json"] == [s_name]
    assert hits["b.csv"] == [sentinels.UID_ROOT]
    assert hits["c.tsv"] == [s_acc]
    assert hits["d.log"] == ["R4SENT_FOLDER", "R4SENT_ZIP"]
    assert hits["e.nii.gz"] == [dicom_builder.sentinel_value("StudyDescription")]
    assert "clean.txt" not in hits
    assert "R4SENT_FOLDER" not in sentinels.needles_for_prv01() and sentinels.UID_ROOT in sentinels.needles_for_prv01()


def test_sh_sentinels_fit_their_value_representation():
    from pydicom.datadict import dictionary_VR, tag_for_keyword

    for keyword in dicom_builder.SENTINEL_FIELDS:
        value = dicom_builder.sentinel_value(keyword)
        if dictionary_VR(tag_for_keyword(keyword)) == "SH":
            assert len(value) <= 16, keyword
        if keyword != "PatientBirthDate":
            assert value.startswith("R4SENT_"), keyword


# ---------------------------------------------------------------------------- R4-OR-10 crash points

@pytest.fixture(scope="module")
def converted_hc03(tmp_path_factory):
    """HC-03 prepared and converted with the real converter (module scope: converted once)."""
    dcm2niix = os.environ.get("MRI_PIPELINE_DCM2NIIX")
    if not dcm2niix:
        pytest.skip("set MRI_PIPELINE_DCM2NIIX to run R4-OR-10's selection crash points")
    from r4lib.project import R4Project, load_exam

    base = tmp_path_factory.mktemp("or10")
    project = R4Project(base / "project")
    project.place_exam(load_exam("hard_cases", "HC-03-REPEAT-CONSISTENT"))
    assert project.run("prepare").code == 0
    assert project.run("convert", "--dcm2niix-bin", dcm2niix).code == 0
    return project, base


def _crash(project, step, marker_dir, argv):
    from r4lib import crash

    result = crash.run_with_crash(argv, step, str(marker_dir), cwd=project.root)
    if not crash.fired(str(marker_dir), step):
        pytest.fail(f"ERROR, not a finding: crash point {step} never fired (exit {result.returncode}); the injection did not happen")
    assert result.returncode == 137, (step, result.returncode)


@pytest.mark.parametrize("step", ["zip-renamed", "rename-logged", "naming-cache-saved"])
def test_r4_or_10_naming_crash_points_fire(tmp_path, step):
    from r4lib.project import R4Project, load_exam

    project = R4Project(tmp_path / "project")
    project.place_exam(load_exam("naming", "NAM-02-LISTED-DATE"))
    _crash(project, step, tmp_path / "markers", ["prepare", "--project-root", project.root])


def _profile(tmp_path, policy):
    """HC-03's repeat pair is decided by the repeat policy (scan-selection.md 6.4 step 4, 6.7):
    'later' selects series 7 and 'earlier' series 3, so a visit has selected content and a
    change of policy changes its decision."""
    path = tmp_path / f"{policy}.json"
    path.write_text(json.dumps({"repeat_policy": policy}), encoding="utf-8")
    return str(path)


@pytest.mark.r4real
@pytest.mark.parametrize("step", ["staged", "swapped"])
def test_r4_or_10_publication_crash_points_fire(converted_hc03, tmp_path, step):
    project, _ = converted_hc03
    out = tmp_path / "Selected"
    _crash(project, step, tmp_path / "markers", ["select", "--project-root", project.root, "--output-root", str(out),
                                                 "--publish", "--profile", _profile(tmp_path, "later")])


@pytest.mark.r4real
@pytest.mark.parametrize("step", ["moved-aside", "previous-deleted"])
def test_r4_or_10_replacement_crash_points_fire(converted_hc03, tmp_path, step):
    """A complete first publication (repeat_policy later), then a republication whose decision
    changes (repeat_policy earlier), so the visit folder is replaced."""
    project, _ = converted_hc03
    out = tmp_path / "Selected"
    first = project.run("select", "--output-root", str(out), "--publish", "--profile", _profile(tmp_path, "later"))
    assert first.code in (0, 2), first.code
    _crash(project, step, tmp_path / "markers", ["select", "--project-root", project.root, "--output-root", str(out),
                                                 "--publish", "--profile", _profile(tmp_path, "earlier")])


@pytest.mark.r4real
@pytest.mark.parametrize("step", ["marker-written", "participants-written", "sessions-written", "bidsignore-written"])
def test_r4_or_10_bids_crash_points_fire(converted_hc03, tmp_path, step):
    project, _ = converted_hc03
    out = tmp_path / "BIDS"
    out.mkdir()
    _crash(project, step, tmp_path / "markers", ["select", "--project-root", project.root, "--output-root", str(out),
                                                 "--bids", "--publish", "--profile", _profile(tmp_path, "later")])


# ---------------------------------------------------------------------------- R4-PRV-06 (reader part, run before G1)

def test_header_reader_output_holds_no_sentinel(tmp_path, header_reader, capsys):
    """R4-PRV-06 for the header reader, checked before it is frozen: on the sentinel exam its
    records, its JSON file and its console output hold no sentinel, no UID root and no planted
    birth date or time of day."""
    from r4lib import sentinels

    exam = next(e for e in catalog.load_file("hard_cases")["exams"] if e["id"] == "HC-10-SENTINELS")
    session = exam["sessions"][0]
    for series in session["series"]:
        dicom_builder.write_series(str(tmp_path / "dicom"), exam, session, series, sentinels=True)
    out_json = tmp_path / "facts.json"
    assert header_reader.main([str(tmp_path / "dicom"), "--json", str(out_json)]) == 0
    printed = capsys.readouterr().out
    text = out_json.read_text(encoding="utf-8") + printed
    forbidden = ("R4SENT_", sentinels.UID_ROOT, sentinels.DATE_SENTINEL, "101010", "36610")
    assert not [f for f in forbidden if f in text]


# ---------------------------------------------------------------------------- bids_rules (oracle rule 5)

UNIT = os.path.join(HERE, "expected", "unit")


def _unit(name):
    with open(os.path.join(UNIT, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("case", _load("bids_rules.json")["cases"], ids=lambda c: c["id"])
def test_bids_rules_hand_cases(case):
    from oracles import bids_rules

    problems, parsed = bids_rules.check_path(case["path"])
    for kind in case["problems"]:
        assert any(p.startswith(kind) for p in problems), (case["id"], problems)
    if not case["problems"]:
        assert problems == [], (case["id"], problems)
        assert parsed["status"] == case["status"], case["id"]


def test_bids_rules_subject_labels():
    from oracles import bids_rules

    labels = _load("bids_rules.json")["subject_labels"]
    assert all(bids_rules.valid_subject_label(x) for x in labels["valid"])
    assert not any(bids_rules.valid_subject_label(x) for x in labels["invalid"])


def test_bids_rules_agree_with_the_bundled_schema():
    """T-class cross-check: the hand-written tables against bidsschematools' schema (BIDS 1.11.x);
    allowed and required entities must be equal for every suffix of the table."""
    schema_mod = pytest.importorskip("bidsschematools.schema")
    from oracles import bids_rules

    s = schema_mod.load_schema()
    names = {k: s["objects"]["entities"][k]["name"] for k in s["rules"]["entities"]}
    order = [names[k] for k in s["rules"]["entities"]]
    mine = [e for e in bids_rules.ENTITY_ORDER if e in order]
    assert mine == [e for e in order if e in mine], "entity order differs from the schema"
    raw = s["rules"]["files"]["raw"]
    found = {}
    for group in ("anat", "dwi", "fmap"):
        for rule in raw[group].values():
            for suffix in rule["suffixes"]:
                found[suffix] = ({names[k] for k in rule["entities"]}, {names[k] for k, v in rule["entities"].items() if v == "required"}, group)
    for suffix, (datatype, allowed, required) in bids_rules.SUFFIXES.items():
        s_allowed, s_required, s_group = found[suffix]
        assert datatype == s_group, suffix
        assert allowed == s_allowed, (suffix, allowed ^ s_allowed)
        assert required == s_required, (suffix, required, s_required)


def _planned_paths():
    out = []
    plan = _unit("bids_plan.json")
    for case in plan["cases"]:
        out.extend(case.get("files", []))
        if "files_pattern" in case:
            base = case["files_pattern"]
            for e in range(1, 5):
                for part in ("mag", "phase"):
                    out.append(base.replace("{1..4}", str(e)).replace("{mag,phase}", part))
    for name, data in catalog.load_all().items():
        for exam in data["exams"]:
            for item in exam["expected"].values():
                outcome = item["outcome"]
                if isinstance(outcome, dict):
                    for key in ("fmap", "files", "exported_anat"):
                        value = outcome.get(key)
                        if isinstance(value, str):
                            sub = exam["roster_rows"][0][0]
                            label = exam["sessions"][0]["label"]
                            out.append(f"sub-{sub}/ses-{label}/{value}" if not value.startswith("sub-") else value)
                        elif isinstance(value, list):
                            sub = exam["roster_rows"][0][0]
                            label = exam["sessions"][0]["label"]
                            out.extend(f"sub-{sub}/ses-{label}/{v}" for v in value)
    return out


def test_r4_bpl_01_table_paths_pass_bids_rules():
    from oracles import bids_rules

    bad = {}
    for path in _planned_paths():
        problems, _ = bids_rules.check_path(path)
        if problems:
            bad[path] = problems
    assert not bad, bad
    for sid in _unit("bids_plan.json")["subject_ids"]["refused"]:
        assert not bids_rules.valid_subject_label(sid)


# ---------------------------------------------------------------------------- tier 1 tables: internal consistency

def test_identity_family_table_agrees_with_the_objective_checks_family_code():
    """Two artifacts written from the same 5.4 text (the hand table and objective_checks.families)
    must agree on every case; a disagreement means one of them misreads 5.4."""
    table = _unit("identity.json")["families"]
    for case in table["cases"]:
        rec = dict(table["base"][case["base"]])
        rec.update(case.get("set", {}))
        got = objective_checks.families(rec, case.get("volume_te_ms"))
        want = [case["family"]] if case["family"] else []
        assert got == want, (case["id"], got, want)


def test_unit_tables_are_well_formed():
    for name in ("identity.json", "ranking.json", "linking_contrast.json", "bids_plan.json"):
        data = _unit(name)
        for section in data.values():
            if isinstance(section, dict) and "cases" in section:
                ids = [c["id"] for c in section["cases"]]
                assert len(ids) == len(set(ids)), (name, "duplicate ids")
                assert section.get("source") or data.get("source"), (name, "no source")


# ---------------------------------------------------------------------------- R4-OR-03

def _catalogs():
    return catalog.load_all()


def test_catalog_files_pass_the_schema_check():
    cats = _catalogs()
    assert cats, "no catalog files"
    for name, data in cats.items():
        assert data["exams"], name


def _session_cases():
    out = []
    for name, data in _catalogs().items():
        for exam in data["exams"]:
            for session in exam["sessions"]:
                if any("facts" in s for s in session["series"]):
                    out.append(pytest.param(name, exam["id"], session["label"], id=f"{exam['id']}-{session['label']}"))
    return out


@pytest.mark.parametrize("cat_name,exam_id,label", _session_cases())
def test_r4_or_03_header_reader_reproduces_catalog_facts(tmp_path, header_reader, cat_name, exam_id, label):
    data = catalog.load_file(cat_name)
    exam = next(e for e in data["exams"] if e["id"] == exam_id)
    session = next(s for s in exam["sessions"] if s["label"] == label)
    dicom_dir = tmp_path / "dicom"
    for series in session["series"]:
        dicom_builder.write_series(str(dicom_dir), exam, session, series, sentinels=bool(series.get("sentinels")))
    result = header_reader.read_session(str(dicom_dir))
    assert result["counts"].get("unreadable", 0) == 0
    problems = {}
    for series in session["series"]:
        if "facts" not in series:
            continue
        key = header_reader.series_key_of(dicom_builder.series_uid(exam["uid_exam"], series["uid_series"]))
        assert key in result["series"], f"series {series['uid_series']} not read"
        expected = catalog.expected_facts(exam, series)
        diffs = facts_cmp.compare(expected, result["series"][key])
        if diffs:
            problems[series["uid_series"]] = diffs
    assert not problems, json.dumps(problems, indent=1, default=str)
