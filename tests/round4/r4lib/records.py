"""Tier 1 inputs: pipeline records built from the unit tables' facts (phase 2; spec plan 1,
"Named unit functions").

The unit tables in expected/unit/ state candidates by the facts the rules read (5.1 field names,
families, native sizes, gaps, phases). This module turns those facts into the pipeline's own
input types (a 5.1 series record and a selection_identity.Volume), so a tier 1 test can call the
functions its group names: selection_identity.analyze, structural_family, native_sampling,
gradient_stats and dwi_family, selection_rules.load_profile and decide_session, and
bids.plan_visit. Inputs only: every expected value stays in the tables.

How a stated fact becomes a record field (choices of the builder, not expectations):
*   native [row, col, slice]: rows = cols = 100, AcquisitionMatrix [100, 0, 0, 100], phase ROW,
    PixelSpacing [col, row] (so the acquired field of view over the matrix gives back exactly
    row and col, PS3.3 C.8.3.1 and scan-selection.md 5.3), SliceThickness = slice. None: no matrix.
*   gap: slice step = thickness + gap.
*   contrast pre/post: a bolus time after/before the acquisition (5.7 rule 1); unknown: the
    agent tag on every series of the session (5.7 rule 2, exam-wide).
*   family: the parameter sets of the identity table's bases (expected/unit/identity.json),
    with the classifier label of the role.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os

from mri_dicom_pipeline import selection_identity as sid
from mri_dicom_pipeline import selection_rules as rules

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def unit_table(name):
    with open(os.path.join(HERE, "expected", "unit", name), encoding="utf-8") as f:
        return json.load(f)


IDENTITY = unit_table("identity.json")
BASES = IDENTITY["families"]["base"]
NEUTRAL_GR = {"mr_acq": "3D", "scan_seq": ["GR"], "seq_var": ["SP"], "scan_opt": [], "tr_ms": 30.0, "te_ms": 20.0,
              "ti_ms": None}
NEUTRAL_EPI = {"mr_acq": "2D", "scan_seq": ["EP", "SE"], "seq_var": ["SK"], "scan_opt": [], "tr_ms": 6000.0,
               "te_ms": 80.0, "ti_ms": None}
FAMILY_PARAMS = {
    "T1_3D_IRGRE": ("T1", dict(BASES["irgre"])), "T1_3D_GRE": ("T1", dict(BASES["gre"])),
    "T1_2D_SE": ("T1", dict(BASES["t1se"])), "T1_2D_IR": ("T1", dict(BASES["t1ir"])),
    "FLAIR_3D": ("FLAIR", dict(BASES["flair"])),
    "FLAIR_2D": ("FLAIR", dict(BASES["flair"], mr_acq="2D", ti_ms=2500.0, tr_ms=8000.0, te_ms=100.0)),
    "T2_3D_TSE": ("T2", dict(BASES["t23d"])), "T2_2D_TSE": ("T2", dict(BASES["t22d"])),
    "DWI_TENSOR": ("DWI", dict(NEUTRAL_EPI)), "DWI_TRACE": ("DWI", dict(NEUTRAL_EPI)),
    "SWI_3D": ("SWI", dict(NEUTRAL_GR)), "SWI_2D": ("SWI", dict(NEUTRAL_GR, mr_acq="2D", tr_ms=600.0)),
    "QSM_3D": ("QSM", dict(NEUTRAL_GR)), "QSM_2D": ("QSM", dict(NEUTRAL_GR, mr_acq="2D", tr_ms=600.0)),
    "GRE_3D": ("GRE", dict(NEUTRAL_GR)), "GRE_2D": ("GRE", dict(NEUTRAL_GR, mr_acq="2D", tr_ms=600.0)),
}


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def series(key, **facts):
    """A complete 5.1 series record (non-mixed) with neutral defaults, then ``facts``."""
    rec = {"series_key": key, "series_number": 1, "manufacturer": "GE", "model": "R4", "field_t": 3.0,
           "n_instances": 40, "n_stacks": 1, "mixed": False, "image_type": ["ORIGINAL", "PRIMARY", "OTHER"],
           "localizer": False, "mosaic": False, "software": "", "mr_acq": "3D", "scan_seq": ["GR"], "seq_var": [],
           "scan_opt": [], "seq_name": "", "tr_ms": 6.0, "te_ms": 2.4, "ti_ms": None, "flip_deg": 10.0,
           "etl": None, "rows": 100, "cols": 100, "pixel_mm": [1.0, 1.0], "thick_mm": 1.0, "sbs_mm": None,
           "acq_matrix": [100, 0, 0, 100], "phase_dir": "ROW", "pct_phase_fov": 100.0, "orientation": "AX",
           "n_positions": 40, "pos_min": 0.0, "pos_max": 117.0, "step_mm": 3.0, "uniform_spacing": True,
           "extent_mm": 120.0, "for_hash": _hash("for" + key)[:20], "acq_rel_s": 0.0, "series_rel_s": 0.0,
           "bolus_rel_s": None, "contrast_agent": False, "parts": {"mag": 40}, "echoes": [], "desc_tokens": [],
           "desc_phase": None, "multiframe": False, "has_image_type_text": False}
    rec.update(facts)
    return rec


def mixed_series(key, **facts):
    rec = {"series_key": key, "series_number": 1, "manufacturer": "GE", "model": "R4", "field_t": 3.0,
           "n_instances": 80, "n_stacks": 2, "mixed": True}
    rec.update(facts)
    return rec


def volume(rec, folder, *, part="mag", te_ms=None, image_type=None, gradients=None, dims=None, sidecar=None,
           name=None):
    """A Volume of one series; its sidecar carries what dcm2niix would write for it."""
    image_type = list(image_type if image_type is not None else (rec.get("image_type") or
                                                                ["ORIGINAL", "PRIMARY", "OTHER"]))
    if part == "phase" and "P" not in image_type:
        image_type = [t for t in image_type if t != "M"] + ["P"]
    sc = {"SeriesNumber": rec.get("series_number"), "ImageType": image_type,
          "source_series_key": rec.get("series_key")}
    te = te_ms if te_ms is not None else rec.get("te_ms")
    if te is not None:
        sc["EchoTime"] = te / 1000.0
    if sidecar:
        sc.update(sidecar)
    rows, cols, n = rec.get("rows") or 32, rec.get("cols") or 32, rec.get("n_positions") or 40
    if dims is None:
        nvol = gradients["n_volumes"] if gradients else 1
        dims = [4 if nvol > 1 else 3, cols, rows, n, nvol, 1, 1, 1]
    stem = name or f"{rec.get('series_key')}_{part}_{te}"
    relpath = f"Subjects/S900/20261201/{folder}/S900_20261201_{folder}_{stem}.nii.gz"
    return sid.Volume(relpath=relpath, sha256=_hash(relpath), folder=folder, token=_hash(stem)[:16],
                      companions=[], sidecar=sc, dims=dims, gradients=copy.deepcopy(gradients), series=rec,
                      json_sha256=_hash(relpath + ".json"))


def gradients(n_dirs, shells=(1000,), b0=1, mismatch=False):
    """A volume's gradient facts (the 5.2 record shape)."""
    if not shells:
        return {"n_volumes": b0, "b0": b0, "shells": [], "b_max": None, "n_dirs": 0, "mismatch": mismatch}
    n = b0 + n_dirs * len(shells)
    return {"n_volumes": n, "b0": b0, "shells": sorted(shells), "b_max": max(shells), "n_dirs": n_dirs,
            "mismatch": mismatch}


def native_fields(native, slice_mm=None, gap=0.0):
    """Record fields that give native [row, col, slice] (see the module docstring)."""
    if native is None:
        thick = slice_mm if slice_mm is not None else 1.0
        return {"acq_matrix": None, "pixel_mm": [1.0, 1.0], "thick_mm": thick, "step_mm": round(thick + gap, 4)}
    row, col, thick = native
    return {"rows": 100, "cols": 100, "acq_matrix": [100, 0, 0, 100], "phase_dir": "ROW", "pixel_mm": [col, row],
            "thick_mm": thick, "step_mm": round(thick + (gap or 0.0), 4)}


def analyze(series_list, volumes, profile=None):
    profile = profile or rules.load_profile()
    recs = {s["series_key"]: s for s in series_list}
    return sid.analyze(recs, volumes, profile["roles"]["DWI"].get("min_tensor_directions", 6),
                       profile["contrast_unknown"])


def decide(series_list, volumes, profile=None, overrides=None):
    profile = profile or rules.load_profile()
    session = analyze(series_list, volumes, profile)
    decisions, fates, info = rules.decide_session(session, profile, overrides)
    return session, decisions, fates, info


def candidate(key, role, spec, number, acq_s, all_agent=False):
    """(series records, volumes) of one ranking-table candidate."""
    family = spec.get("family") or {"T1": "T1_3D_IRGRE", "T1_POST": "T1_3D_IRGRE", "FLAIR": "FLAIR_3D",
                                    "T2": "T2_2D_TSE", "DWI": "DWI_TENSOR", "SWI": "SWI_3D", "QSM": "QSM_3D",
                                    "GRE": "GRE_3D"}[role]
    label, params = FAMILY_PARAMS[family]
    if "mr_acq" in spec:
        params["mr_acq"] = spec["mr_acq"]
    native = spec.get("native", [1.0, 1.0, 1.0] if role != "DWI" else [2.0, 2.0, 2.0])
    if "native" in spec and spec["native"] is None:
        native = None
    fields = native_fields(native, spec.get("slice"), spec.get("gap", 0.0))
    rec = series(key, series_number=number, acq_rel_s=acq_s, orientation=spec.get("orientation", "AX"),
                 image_type=["ORIGINAL", "PRIMARY", "OTHER"] + list(spec.get("variant", [])),
                 contrast_agent=all_agent, **params, **fields)
    contrast = spec.get("contrast")
    if role == "T1_POST" and contrast is None:
        contrast = "post"
    if contrast == "post":
        rec["bolus_rel_s"] = acq_s - 10.0
    elif contrast == "pre":
        rec["bolus_rel_s"] = acq_s + 10.0
    elif contrast == "unknown":
        rec["contrast_agent"] = True
    grads = None
    if role == "DWI":
        grads = gradients(spec.get("n_dirs", 6 if family == "DWI_TENSOR" else 3), spec.get("shells", [1000]))
    n_echoes = spec.get("n_echoes", 1)
    tes = [rec["te_ms"] + 8.0 * i for i in range(n_echoes)]
    if n_echoes > 1:
        rec["te_ms"] = None
        rec["echoes"] = [{"te_ms": te, "echo_number": i + 1, "n_instances": rec["n_positions"]}
                         for i, te in enumerate(tes)]
    parts = ["mag", "phase"] if spec.get("has_phase") else ["mag"]
    rec["parts"] = {p: rec["n_positions"] * n_echoes for p in parts}
    vols = []
    for te in tes:
        for p in parts:
            vols.append(volume(rec, label, part=p, te_ms=te, gradients=grads,
                               image_type=rec["image_type"] + (["M"] if len(parts) > 1 and p == "mag" else [])))
    return [rec], vols


def directions(n):
    """n distinct unit vectors, none antipodal to another (a Fibonacci half-sphere)."""
    out = []
    golden = math.pi * (3 - math.sqrt(5))
    for i in range(n):
        z = 1 - (i + 0.5) / n
        r = math.sqrt(max(0.0, 1 - z * z))
        out.append((r * math.cos(golden * i), r * math.sin(golden * i), z))
    return out
