"""TG-RNK: gates, tiers and ranking (plan section 5, TG-RNK; R-RNK-1 to R-RNK-7).

Tier 1: expected/unit/ranking.json (R4-RNK-02 tiers, R4-RNK-03 components, R4-RNK-04 options)
through selection_identity.analyze and selection_rules.load_profile, decide_session and
session_requirement (the plan names no unit function for TG-RNK; these are the module's entry
points for one session). Tier 3: the catalog's R-RNK keys end to end; R4-RNK-04's variants; and
R4-RNK-05, the default 110 mm coverage gate at 109.9 and 110.0 mm with the real converter.
"""

from __future__ import annotations

import copy
import json
import os

import pytest

from oracles import geometry
from r4lib import catalog, dicom_builder, e2e, records
from r4lib.project import R4Project

TABLE = records.unit_table("ranking.json")


def _acq_of(session, rec):
    return next(v for v in session.volumes if v.series is rec).acq


# ---------------------------------------------------------------------------- R4-RNK-02

@pytest.mark.parametrize("case", TABLE["tiers"]["cases"], ids=lambda c: c["id"])
def test_r4_rnk_02_tier_assignment(case):
    recs, vols = records.candidate("A", case["role"], {"family": case["family"], "native": case["native"],
                                                        "gap": case["gap"]}, 3, 0.0)
    session, decisions, _fates, info = records.decide(recs, vols)
    acq = _acq_of(session, recs[0])
    tier = info[acq.id]["tier"]
    assert tier is not None and tier[1]["name"] == case["tier"], (case["id"], tier, info[acq.id]["reasons"])
    if "outcome_if_winner" in case:
        assert decisions[case["role"]]["outcome"] == case["outcome_if_winner"], (case["id"], decisions[case["role"]])


# ---------------------------------------------------------------------------- R4-RNK-03

@pytest.mark.parametrize("case", TABLE["components"]["cases"], ids=lambda c: c["id"])
def test_r4_rnk_03_ranking_components(case):
    unknown = any(case[k].get("contrast") == "unknown" for k in ("a", "b"))
    ra, va = records.candidate("A", case["role"], case["a"], 3, 0.0, all_agent=unknown)
    rb, vb = records.candidate("B", case["role"], case["b"], 5, 600.0, all_agent=unknown)
    session, decisions, _fates, info = records.decide(ra + rb, va + vb)
    d = decisions[case["role"]]
    a, b = _acq_of(session, ra[0]), _acq_of(session, rb[0])
    if "tier_both" in case:
        names = [info[x.id]["tier"][1]["name"] if info[x.id]["tier"] else None for x in (a, b)]
        assert names == [case["tier_both"]] * 2, (case["id"], names)
    if case.get("outcome"):
        assert d["outcome"] == case["outcome"], (case["id"], d["outcome"], d["reasons"], d["decided_by"])
        assert case["reason"] in d["reasons"], (case["id"], d["reasons"])
        return
    winner = {"a": a, "b": b}[case["winner"]]
    assert d["winner"] is winner, (case["id"], d["outcome"], d["reasons"], d["decided_by"],
                                   info[a.id]["reasons"], info[b.id]["reasons"])
    assert d["decided_by"] == case["decided_by"], (case["id"], d["decided_by"])


# ---------------------------------------------------------------------------- R4-RNK-04 (tier 1)

def _options(option):
    """The command-line option as selection_rules.load_profile's options (its documented argument)."""
    name, _, value = option.partition(" ")
    return {"--3d-only": lambda: {"require": {"mr_acq": "3D"}},
            "--isotropic": lambda: {"require": {"isotropic": True}},
            "--no-fallback": lambda: {"require": {"no_fallback": True}},
            "--max-voxel-mm": lambda: {"require": {"max_voxel_mm": float(value)}},
            "--min-directions": lambda: {"require": {"min_directions": int(value)}},
            "--field-strength": lambda: {"field_strength": [float(value)]},
            "--manufacturer": lambda: {"manufacturer": [value]}}[name]()


@pytest.mark.parametrize("case", TABLE["options"]["cases"], ids=lambda c: c["id"])
def test_r4_rnk_04_options(case):
    from mri_dicom_pipeline import selection_rules as rules

    profile = rules.load_profile(None, _options(case["option"]))
    if "session" in case:
        rec = records.series("S1", **{k: v for k, v in case["session"].items()})
        got = rules.session_requirement([rec], profile)
        if "session_excluded" in case:
            assert (got is not None and got[0] == "session_excluded") is case["session_excluded"], (case["id"], got)
        if case.get("outcome"):
            assert got == (case["outcome"], case["reason"]), (case["id"], got)
        return
    recs, vols = records.candidate("A", case["role"], case["candidate"], 3, 0.0)
    session, decisions, _fates, info = records.decide(recs, vols, profile)
    acq = _acq_of(session, recs[0])
    reasons = info[acq.id]["reasons"]
    if case["reason"] is None:
        assert not reasons, (case["id"], reasons)
    else:
        assert case["reason"] in reasons, (case["id"], reasons)
    if case.get("outcome"):
        assert decisions[case["role"]]["outcome"] == case["outcome"], (case["id"], decisions[case["role"]])


# ---------------------------------------------------------------------------- tier 3: the catalog

@pytest.mark.r4real
@pytest.mark.parametrize("cat_name,exam_id,req",
                         e2e.exam_cases({"R-RNK-3", "R-RNK-5", "R-RNK-6", "R-RNK-7", "R-REV-1", "R-REV-3"}),
                         ids=lambda x: str(x))
def test_r4_rnk_catalog_end_to_end(real_exam, cat_name, exam_id, req):
    """The catalog's ranking keys, and its R-REV-1 (empty role folders) and R-REV-3 (exit code) keys on
    the real converter's output (TG-REV checks the same behaviors with the tier 2 converter)."""
    problems = real_exam(cat_name, exam_id).check(req)
    assert not problems, json.dumps(problems, indent=1)


def _variant_args(name, tmp_path):
    if name.startswith("profile "):
        key, _, value = name[len("profile "):].partition("=")
        path = tmp_path / "variant_profile.json"
        path.write_text(json.dumps({key: value}), encoding="utf-8")
        return ["--profile", str(path)]
    return name.split()


@pytest.mark.r4real
@pytest.mark.parametrize("cat_name,exam_id,req", e2e.exam_cases({"R-RNK-4"}), ids=lambda x: str(x))
def test_r4_rnk_04_catalog_variants(real_exam, tmp_path, cat_name, exam_id, req):
    conv = real_exam(cat_name, exam_id)
    problems = {}
    for i, (variant, want) in enumerate(conv.exam["expected"][req]["outcome"]["variants"].items()):
        run = conv.select(tmp_path / f"v{i}", *_variant_args(variant, tmp_path))
        found = []
        if "session_excluded" in want:
            excluded = any(r["outcome"] == "session_excluded" for r in run.rows)
            if excluded is not want["session_excluded"]:
                found.append(f"session_excluded {excluded}")
        found += e2e.check_decisions(run, {k: v for k, v in want.items() if k != "session_excluded"})
        if found:
            problems[variant] = found
    e2e.record("catalog_checks", {"exam": exam_id, "requirement": req, "problems": problems})
    assert not problems, json.dumps(problems, indent=1)


# ---------------------------------------------------------------------------- R4-RNK-05

def _coverage_exam(n_slices, thick, uid):
    data = catalog.load_file("structural")
    exam = copy.deepcopy(next(e for e in data["exams"] if e["id"] == "STR-01-GE-PREMIER-CENSUS"))
    exam["uid_exam"] = uid
    exam["roster_rows"] = [["S990", "20260725"]]
    session = exam["sessions"][0]
    session["zip_name"] = "S990_20260725.zip"
    t1 = next(s for s in session["series"] if s["uid_series"] == 3)
    t1["slices"] = n_slices
    t1["tags"]["SliceThickness"] = thick
    session["series"] = [t1]
    return exam, t1


@pytest.mark.r4real
@pytest.mark.parametrize("n_slices,thick,extent,partial", [(110, 0.9, 109.9, True), (110, 1.0, 110.0, False)])
def test_r4_rnk_05_default_coverage_gate(tmp_path, r4_dcm2niix, n_slices, thick, extent, partial):
    """The T1's extent comes from the geometry oracle (5.3: (n - 1) x step + thickness), checked first;
    the expected outcome is the plan's: partial_coverage at 109.9 mm, passing at 110.0 mm."""
    exam, t1 = _coverage_exam(n_slices, thick, 9700 + int(partial))
    g = t1["geometry"]
    normal = geometry.slice_normal(g["iop"])
    ipps = [[g["first_position"][i] + k * g["step_mm"] * normal[i] for i in range(3)] for k in range(n_slices)]
    facts = geometry.stack_facts(ipps, g["iop"], thick)
    assert abs(facts["extent_mm"] - extent) < 1e-6, facts
    project = R4Project(tmp_path / "project")
    project.place_exam(exam)
    assert project.run("prepare").code == 0
    assert project.run("convert", "--dcm2niix-bin", r4_dcm2niix).code == 0
    conv = e2e.Converted(project, exam, "real", [])
    run = conv.select(tmp_path / "Sel")
    row = run.role_row("T1")
    e2e.record("rnk05", {"extent": extent, "outcome": row["outcome"], "reasons": row["reasons"]})
    if partial:
        assert row["outcome"] == "no_eligible_candidate" and "partial_coverage" in row["reasons"], row
    else:
        assert row["outcome"] in ("selected", "selected_fallback") and "partial_coverage" not in row["reasons"], row


# ---------------------------------------------------------------------------- R4-RNK-06

RANKED_OR_DERIVED = {"T1", "T1_POST", "T2", "FLAIR", "DWI", "ADC", "EADC", "SWI", "SWI_PHASE", "SWI_MINIP", "QSM",
                     "GRE"}


def folder_problems(run):
    """6.6: a ranked or derived role folder contains a single acquisition (or a single derived group), the
    non-reformat maps linked to it, and in DWI/ the partner scans; no volume ends up with two fates or none."""
    out = []
    by_folder = {}
    for r in run.vrows:
        if not r["fate"]:
            out.append(f"volume {r['volume_id']} has no fate")
        folder = e2e.folder_of(r)
        if folder in RANKED_OR_DERIVED:
            by_folder.setdefault(folder, []).append(r)
    for folder, rows in by_folder.items():
        primaries = {r["acquisition_id"] for r in rows if r["acquisition_id"] and r["fate"] == "selected"}
        groups = {r["derived_group_id"] for r in rows if r["derived_group_id"]}
        partners = {r["acquisition_id"] for r in rows if r["fate"] == "companion"}
        if len(primaries) > 1:
            out.append(f"{folder}: {len(primaries)} acquisitions")
        if partners and folder != "DWI":
            out.append(f"{folder}: partner volumes outside DWI/")
        for g in groups:
            members = [r for r in rows if r["derived_group_id"] == g]
            if any(r["kind"] == "reformat" for r in members):
                out.append(f"{folder}: a reformat")
            if primaries and not any(r["link_parent"] in primaries for r in members):
                out.append(f"{folder}: a derived group not linked to the folder's acquisition")
        if not primaries and len(groups) > 1:
            out.append(f"{folder}: {len(groups)} derived groups")
    return out


@pytest.mark.r4real
@pytest.mark.parametrize("cat_name,exam_id", sorted({(c, e) for c, e, _ in e2e.exam_cases({"R-RNK-6", "R-REV-3"})}))
def test_r4_rnk_06_folders_hold_one_acquisition(real_exam, cat_name, exam_id):
    run, extras, pub, _visit = real_exam(cat_name, exam_id).standard()
    problems = folder_problems(run) + [f"(publish run) {p}" for p in folder_problems(pub)]
    assert not problems, problems


# Added in phase 2 after TG-SEED (fault F-RNK-2, 3d_ir_gre and 3d_gre exchanged, survived the frozen
# tables): hand-derived from scan-selection.md 6.7, where the T1 tiers are 3d_ir_gre, then 3d_gre. A
# T1_3D_IRGRE and a T1_3D_GRE candidate that differ in nothing else: the IRGRE wins by tier, and its
# outcome is `selected` (3d_ir_gre is not a fallback tier). P only.
TIER_ORDER_CASE = {"id": "C21", "role": "T1", "a": {"family": "T1_3D_IRGRE", "native": [1.0, 1.0, 1.0]},
                   "b": {"family": "T1_3D_GRE", "native": [1.0, 1.0, 1.0]}, "winner": "a", "decided_by": "tier"}


def test_r4_rnk_03_t1_tier_order():
    test_r4_rnk_03_ranking_components(TIER_ORDER_CASE)
    ra, va = records.candidate("A", "T1", TIER_ORDER_CASE["a"], 3, 0.0)
    rb, vb = records.candidate("B", "T1", TIER_ORDER_CASE["b"], 5, 600.0)
    _session, decisions, _fates, _info = records.decide(ra + rb, va + vb)
    assert decisions["T1"]["outcome"] == "selected", decisions["T1"]
