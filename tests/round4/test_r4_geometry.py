"""TG-GEO: the pipeline's own geometry (plan section 5, TG-GEO; R-GEO-2 to R-GEO-4, R-GEO-1 on
synthetic data; D-2).

Catalog geometry.json variants and the diffusion field-map pairs, converted with the real dcm2niix.
Expected values: independent PS3.3 calculations in oracles/geometry.py from the catalog's
geometry (C), NiBabel's reading of the converted images (T), and the BIDS design 4.3 rule (P).
Evidence: geometry_comparison.csv in R4_EVIDENCE_DIR.
"""

from __future__ import annotations

import csv
import json
import os

import pytest

from oracles import geometry
from r4lib import catalog, e2e

pytestmark = [pytest.mark.r4real, pytest.mark.filterwarnings("ignore::DeprecationWarning")]
GEO = [e["id"] for e in catalog.load_file("geometry")["exams"]]


def _oracle(series):
    g = series["geometry"]
    normal = geometry.slice_normal(g["iop"])
    n = int(series["slices"])
    ipps = [[g["first_position"][i] + k * g["step_mm"] * normal[i] for i in range(3)] for k in range(n)]
    facts = geometry.stack_facts(ipps, g["iop"], series["tags"]["SliceThickness"])
    native = geometry.native_voxel(g["rows"], g["cols"], g["pixel_spacing"], series["tags"].get("AcquisitionMatrix"),
                                   series["tags"].get("InPlanePhaseEncodingDirection"),
                                   series["tags"]["SliceThickness"])
    return {"orientation": geometry.orientation_label(g["iop"]), "facts": facts, "native": native, "n": n}


def _row(run, uid):
    rows = e2e.rows_for(run, str(uid))
    assert len(rows) == 1, rows
    return rows[0]


def _write(rows):
    path = e2e.evidence_dir()
    if not path:
        return
    target = os.path.join(path, "geometry_comparison.csv")
    new = not os.path.exists(target)
    with open(target, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["exam", "value", "pipeline", "oracle", "result"])
        w.writerows(rows)


@pytest.mark.parametrize("exam_id", GEO)
def test_r4_geo_01_slice_count_after_rotation(real_exam, exam_id):
    import nibabel as nib

    conv = real_exam("geometry", exam_id)
    run = conv.standard()[0]
    series = e2e.all_series(conv.exam)[0]
    row = _row(run, series["uid_series"])
    img = nib.load(os.path.join(conv.root, "Niftis", *row["source"].split("/")))
    oracle_n = geometry.nifti_slice_count(img.shape, img.affine, series["geometry"]["iop"])
    want = conv.exam["expected"]["R-GEO-2"]["outcome"][str(series["uid_series"])]
    _write([(exam_id, "n_slices", row["n_slices"], oracle_n, int(row["n_slices"]) == oracle_n == want["n_slices"])])
    assert oracle_n == want["n_slices"], (oracle_n, img.shape)
    assert int(row["n_slices"]) == oracle_n
    assert ("slice_count_mismatch" in row["exclusion_reasons"]) is want["slice_count_mismatch"]


@pytest.mark.parametrize("exam_id", GEO)
def test_r4_geo_03_orientation_step_extent_native(real_exam, exam_id):
    conv = real_exam("geometry", exam_id)
    run = conv.standard()[0]
    series = e2e.all_series(conv.exam)[0]
    o = _oracle(series)
    rec = e2e.cache_series(conv, conv.exam, series["uid_series"])
    row = _row(run, series["uid_series"])
    want = conv.exam["expected"]["R-GEO-4"]["outcome"][str(series["uid_series"])]
    native = [o["native"]["row"], o["native"]["col"], series["tags"]["SliceThickness"]]
    got_native = [float(x) for x in row["native_mm"].split("x")]
    checks = [("orientation", rec["orientation"], o["orientation"], rec["orientation"] == o["orientation"]),
              ("step_mm", rec["step_mm"], o["facts"]["step_mm"], abs(rec["step_mm"] - o["facts"]["step_mm"]) <= 0.001),
              ("extent_mm", rec["extent_mm"], o["facts"]["extent_mm"],
               abs(rec["extent_mm"] - o["facts"]["extent_mm"]) <= 0.01),
              ("native_mm", got_native, native, all(abs(a - b) <= 0.0051 for a, b in zip(got_native, native)))]
    if "pos_min" in want:
        checks.append(("pos_min", rec["pos_min"], o["facts"]["pos_min"], abs(rec["pos_min"] - o["facts"]["pos_min"]) <= 0.01))
    _write([(exam_id, *c) for c in checks])
    assert o["orientation"] == want["orientation"] and abs(o["facts"]["extent_mm"] - want["extent_mm"]) <= 0.01, \
        "oracle disagrees with the catalog"
    assert all(c[3] for c in checks), [c for c in checks if not c[3]]


FIELD_MAP_PAIRS = [("diffusion", "DIF-03-REVERSE-PE-B0"), ("diffusion", "DIF-07-PARTNER-SHIFTED-GRID"),
                   ("diffusion", "DIF-14-PARTNER-AXIS-ORDER")]


@pytest.mark.parametrize("cat_name,exam_id", FIELD_MAP_PAIRS)
def test_r4_geo_02_field_map_grid_decision(real_exam, cat_name, exam_id):
    import nibabel as nib

    conv = real_exam(cat_name, exam_id)
    run, root = conv.bids()
    dwi, b0 = _row(run, 5), _row(run, 7)
    a = nib.load(os.path.join(conv.root, "Niftis", *dwi["source"].split("/")))
    b = nib.load(os.path.join(conv.root, "Niftis", *b0["source"].split("/")))
    same, detail = geometry.same_grid(a.shape[:3], a.affine, b.shape[:3], b.affine)
    any_order = geometry.same_physical_grid_any_order(a.shape[:3], a.affine, b.shape[:3], b.affine)
    any_order = any_order[0] if isinstance(any_order, tuple) else any_order
    exported = b0["export_status"] not in ("", "omitted")
    e2e.record("geometry_fieldmap", {"exam": exam_id, "pipeline_exports_fmap": exported, "oracle_same_grid": same,
                                     "oracle_same_physical_grid_any_order": any_order, "oracle_detail": detail,
                                     "export_reason": b0["export_reason"]})
    _write([(exam_id, "fmap_grid", exported, same, exported == same)])
    assert exported == same, (exported, same, any_order, b0["export_reason"])
    if not same:
        assert b0["export_reason"] == "grid_mismatch", b0["export_reason"]


@pytest.mark.parametrize("exam_id", GEO)
def test_r4_geo_04_pixel_sampler_report(real_exam, exam_id):
    """Report only (R-GEO-1 on synthetic data): never gates; any mismatch is reported to the owner as a finding."""
    import pydicom

    from oracles import pixel_sampler

    conv = real_exam("geometry", exam_id)
    run = conv.standard()[0]
    series = e2e.all_series(conv.exam)[0]
    row = _row(run, series["uid_series"])
    uid = e2e.dicom_builder.series_uid(conv.exam["uid_exam"], series["uid_series"])
    paths = []
    for dirpath, _d, files in os.walk(os.path.join(conv.root, "DicomExtracted", "Subjects")):
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                ds = pydicom.dcmread(p, stop_before_pixels=True, specific_tags=["SeriesInstanceUID"])
            except Exception:  # noqa: BLE001 - receipts and inventories are not DICOM
                continue
            if str(ds.get("SeriesInstanceUID", "")) == uid:
                paths.append(p)
    nifti = os.path.join(conv.root, "Niftis", *row["source"].split("/"))
    result = pixel_sampler.sample_series(sorted(paths), [{"path": nifti, "echo_ms": None}], "geo-" + exam_id)
    e2e.record("pixel_sampler_synthetic", {"exam": exam_id, "status": result["status"]})
    assert result["status"] in ("matched", "under-sampled"), result
