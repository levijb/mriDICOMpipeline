"""TG-IDN: acquisition identity (plan section 5, TG-IDN; R-IDN-1 to R-IDN-7).

Tier 1 calls the unit functions the group names (selection_identity.analyze, native_sampling,
gradient_stats, dwi_family) on records built from expected/unit/identity.json and the hand-case
files. Tier 3 runs the catalog exams end to end with the real dcm2niix and compares the reports
with each exam's expected block. Expected values come from those files only.
"""

from __future__ import annotations

import json
import os

import pytest

from oracles import geometry, gradients as grad_oracle
from r4lib import e2e, records

HERE = os.path.dirname(os.path.abspath(__file__))
IDENTITY = records.unit_table("identity.json")


def _hand(name):
    with open(os.path.join(HERE, "expected", "hand_cases", name), encoding="utf-8") as f:
        return json.load(f)


def _params(case):
    if "params_set" in case:
        p = dict(IDENTITY["families"]["base"][case["params_set"]["base"]])
        p.update(case["params_set"]["set"])
        return p
    if "params" in case:
        return dict(IDENTITY["families"]["base"][case["params"]])
    return {}


def _one_volume(params, label, image_type, has_bval=False, echoes=None, volume_te=None):
    p = {k: v for k, v in params.items() if k != "echoes"}
    rec = records.series("S1", series_number=3, image_type=list(image_type), **p)
    if echoes:
        rec["echoes"] = [dict(e, n_instances=40) for e in echoes]
    grads = records.gradients(6) if has_bval else None
    v = records.volume(rec, label, image_type=image_type, gradients=grads, te_ms=volume_te)
    session = records.analyze([rec], [v])
    return session.volumes[0]


# ---------------------------------------------------------------------------- R4-IDN-01 (tier 1)

@pytest.mark.parametrize("case", IDENTITY["roles"]["cases"], ids=lambda c: c["id"])
def test_r4_idn_01_role_rule_order(case):
    params = _params(case)
    if case["label"] in ("ADC", "DWI") and not params:
        params = dict(records.NEUTRAL_EPI)
    v = _one_volume(params, case["label"], case["image_type"], case["has_bval"])
    assert (v.role, v.role_rule) == (case["role"], case["rule"]), (case["id"], v.role, v.role_rule)
    assert sorted(v.warnings) == sorted(case["warnings"]), (case["id"], v.warnings)
    if "family" in case:
        assert v.family == case["family"], (case["id"], v.family)


# ---------------------------------------------------------------------------- R4-IDN-02 (tier 1)

@pytest.mark.parametrize("case", IDENTITY["families"]["cases"], ids=lambda c: c["id"])
def test_r4_idn_02_family_bounds(case):
    """Label OTHER, so a volume whose parameters name no family has no role (5.4 rule 6) and an
    empty family; a family gives the structural role (rule 4)."""
    params = dict(IDENTITY["families"]["base"][case["base"]])
    params.update(case.get("set", {}))
    echoes = params.pop("echoes", None)
    v = _one_volume(params, "OTHER", ["ORIGINAL", "PRIMARY", "OTHER"], echoes=echoes,
                    volume_te=case.get("volume_te_ms"))
    assert (v.family or None) == case["family"], (case["id"], v.family)


# ---------------------------------------------------------------------------- R4-IDN-03 (tier 1)

def _neutral_for(label):
    if label in ("T1", "T1_POST"):
        return dict(IDENTITY["families"]["base"]["irgre"])
    if label in ("ADC", "EADC", "DWI"):
        return dict(records.NEUTRAL_EPI)
    return dict(records.NEUTRAL_GR)


@pytest.mark.parametrize("case", IDENTITY["derived_kinds"]["cases"], ids=lambda c: c["id"])
def test_r4_idn_03_derived_kinds(case):
    v = _one_volume(_neutral_for(case["role"]), case["role"], case["image_type"])
    assert v.role == case["role"], (case["id"], v.role)
    assert (v.kind != "native") is case["derived"], (case["id"], v.kind)
    assert v.kind == case["kind"], (case["id"], v.kind)
    assert sorted(v.warnings) == sorted(case["warnings"]), (case["id"], v.warnings)


# ---------------------------------------------------------------------------- R4-IDN-05 (tier 1)

@pytest.mark.parametrize("case", _hand("native_voxel.json")["cases"], ids=lambda c: c["id"])
def test_r4_idn_05_native_sampling_against_the_geometry_oracle(case):
    from mri_dicom_pipeline import selection_identity as sid

    rec = {"rows": case["rows"], "cols": case["cols"], "pixel_mm": case["pixel_spacing"],
           "acq_matrix": case["acq_matrix"], "phase_dir": case["phase_dir"], "thick_mm": case["thick_mm"]}
    got = sid.native_sampling(rec)
    want = geometry.native_voxel(case["rows"], case["cols"], case["pixel_spacing"], case["acq_matrix"],
                                 case["phase_dir"], case["thick_mm"])
    if want is None:
        assert got["inplane"] == [] and got["max"] is None, (case["id"], got)
        return
    if "if_row" in want:
        pairs = [(want["if_row"]["row"], want["if_row"]["col"]), (want["if_col"]["row"], want["if_col"]["col"])]
        assert len(got["inplane"]) == 2, (case["id"], got)
        for g, w in zip(got["inplane"], pairs):
            assert abs(g[0] - w[0]) <= 1e-3 and abs(g[1] - w[1]) <= 1e-3, (case["id"], got, pairs)
        return
    assert len(got["inplane"]) == 1, (case["id"], got)
    row, col = got["inplane"][0]
    assert abs(row - want["row"]) <= 1e-3 and abs(col - want["col"]) <= 1e-3, (case["id"], got, want)
    assert abs(got["max"] - geometry.native_max(want)) <= 1e-3, (case["id"], got)
    assert got["interpolated"] is geometry.interpolated_grid(case["pixel_spacing"], want), case["id"]


# ---------------------------------------------------------------------------- R4-IDN-07 (tier 1)

@pytest.mark.parametrize("case", _hand("gradients.json")["cases"], ids=lambda c: c["id"])
def test_r4_idn_07_gradient_statistics_against_the_oracle(case):
    from mri_dicom_pipeline import selection_identity as sid

    vectors = [tuple(v) for v in case["bvecs"]]
    rows = [[v[i] for v in vectors] for i in range(3)] if vectors else [[], [], []]
    got = sid.gradient_stats(case["bvals"], rows, case["dim4"])
    want = grad_oracle.gradient_stats(case["bvals"], vectors, case["dim4"])
    for key in ("n_volumes", "b0", "shells", "b_max", "n_dirs", "mismatch"):
        if key in want:
            assert got[key] == want[key], (case["id"], key, got[key], want[key])
    if "family" in case["expected"]:
        assert sid.dwi_family(got, 6) == case["expected"]["family"], case["id"]


# ---------------------------------------------------------------------------- tier 3: the catalog end to end

KEYS = ("R-IDN-1", "R-IDN-2", "R-IDN-3", "R-IDN-4", "R-IDN-5", "R-IDN-6", "R-IDN-7")


@pytest.mark.r4real
@pytest.mark.parametrize("cat_name,exam_id,req", e2e.exam_cases(set(KEYS)),
                         ids=lambda x: str(x))
def test_r4_idn_catalog_end_to_end(real_exam, cat_name, exam_id, req):
    """R4-IDN-01 to 07 on every catalog exam that states them; R4-IDN-06 is the PACS copy exam
    (HC-01) and the two-orientation series (HC-09): no volume of a mixed series is chosen."""
    conv = real_exam(cat_name, exam_id)
    problems = conv.check(req)
    assert not problems, json.dumps(problems, indent=1)
