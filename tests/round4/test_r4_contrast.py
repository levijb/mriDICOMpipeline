"""TG-CON: contrast phase (plan section 5, TG-CON; R-CON-1 to R-CON-3, part of R-VIS-3).

Tier 1: expected/unit/linking_contrast.json "contrast" (R4-CON-01) through
selection_identity.analyze and selection_rules.decide_session. A description becomes the 5.1
record fields with the pipeline's own description handling, so the cases exercise it together
with the rules. Tier 3: the catalog's R-CON-1 and R-CON-2 keys end to end (R4-CON-02, and
R4-CON-04 on the visit CON-04), and R4-CON-03: the objective contrast check (oracles/
objective_checks.py on header-reader facts) on every tier 3 exam with contrast.
"""

from __future__ import annotations

import json
import os

import pytest

from r4lib import e2e, records

TABLE = records.unit_table("linking_contrast.json")["contrast"]
BASES = records.IDENTITY["families"]["base"]
# Series of a case get different structural families, so that no two form a same-protocol repeat.
CYCLE = ("irgre", "t22d", "flair", "t23d")


def _build(case):
    from mri_dicom_pipeline import selection_identity as sid

    series_list, volumes, keys = [], [], {}
    acq_groups = {}
    for i, (name, facts) in enumerate(case["series"].items()):
        facts = dict(facts)
        description = facts.pop("description", None)
        kind = facts.pop("kind", None)
        linked_to = facts.pop("linked_to", None)
        acquisition = facts.pop("acquisition", None)
        params = dict(BASES[CYCLE[i % len(CYCLE)]])
        label = {"irgre": "T1", "t22d": "T2", "flair": "FLAIR", "t23d": "T2"}[CYCLE[i % len(CYCLE)]]
        image_type = ["ORIGINAL", "PRIMARY", "OTHER"]
        part = "mag"
        if acquisition:
            params, label = dict(records.NEUTRAL_GR), "SWI"
            part = "phase" if acquisition in acq_groups else "mag"
            acq_groups.setdefault(acquisition, name)
        if kind == "reformat":
            image_type = ["DERIVED", "SECONDARY", "REFORMATTED"]
            params = dict(BASES["irgre"])
        params.pop("echoes", None)
        rec = records.series("K" + name, series_number=facts.pop("series_number", 10 + i), image_type=image_type,
                             desc_phase=sid.description_phase(description, None),
                             desc_tokens=sid.description_tokens(description, None)[0], **params)
        rec.update(facts)
        if acquisition and part == "phase":
            first = series_list[[s["series_key"] for s in series_list].index("K" + acq_groups[acquisition])]
            rec["for_hash"] = first["for_hash"]
        if linked_to:
            rec["for_hash"] = series_list[[s["series_key"] for s in series_list].index("K" + linked_to)]["for_hash"]
        series_list.append(rec)
        volumes.append(records.volume(rec, label, part=part, image_type=image_type))
        keys[name] = rec["series_key"]
    return series_list, volumes, keys


def _phase_of(session, key):
    v = next(v for v in session.volumes if v.series and v.series["series_key"] == key)
    item = v.acq or v.group
    return [item.phase, item.evidence]


@pytest.mark.parametrize("case", TABLE["cases"], ids=lambda c: c["id"])
def test_r4_con_01_rule_order(case):
    from mri_dicom_pipeline import selection_rules as rules

    series_list, volumes, keys = _build(case)
    profile = rules.load_profile(case.get("profile"))
    session, decisions, _fates, _info = records.decide(series_list, volumes, profile)
    for name, want in case.get("phases", {}).items():
        assert _phase_of(session, keys[name]) == want, (case["id"], name, _phase_of(session, keys[name]))
    for acq_name, want in case.get("acquisition_phase", {}).items():
        names = [n for n, f in case["series"].items() if f.get("acquisition") == acq_name]
        got = _phase_of(session, keys[names[0]])
        assert got[0] == want[0] and (want[1] is None or got[1] == want[1]), (case["id"], got)
    for warning in case.get("warnings", []):
        if warning == "contrast_presumed_pre":
            winners = [d for d in decisions.values() if d["winner"] is not None and hasattr(d["winner"], "primary")]
            assert winners and all(warning in d["warnings"] for d in winners if d["winner"].evidence == "presumed"), \
                (case["id"], [(d["role"], d["warnings"]) for d in winners])
        else:
            acq_warnings = {w for a in session.acquisitions for w in a.warnings}
            assert warning in acq_warnings, (case["id"], acq_warnings)


@pytest.mark.r4real
@pytest.mark.parametrize("cat_name,exam_id,req", e2e.exam_cases({"R-CON-1", "R-CON-2"}), ids=lambda x: str(x))
def test_r4_con_catalog_end_to_end(real_exam, cat_name, exam_id, req):
    """R4-CON-01 and R4-CON-02 on the catalog; R4-CON-04 is CON-04's R-CON-1 (agent tag on every series of
    the second session only: exam-wide judged per session)."""
    problems = real_exam(cat_name, exam_id).check(req)
    assert not problems, json.dumps(problems, indent=1)


# ---------------------------------------------------------------------------- R4-CON-03

def reader_records(conv, header_reader):
    """{series_key: header reader record} over every extracted session of the project."""
    out = {}
    base = os.path.join(conv.root, "DicomExtracted", "Subjects")
    for subject in os.listdir(base):
        for label in os.listdir(os.path.join(base, subject)):
            folder = os.path.join(base, subject, label)
            result = header_reader.read_session(folder)
            for key, rec in result["series"].items():
                rec = dict(rec, _session=label)
                out[key] = rec
    return out


def objective_flags(conv, header_reader, roles=("T1", "T1_POST")):
    """{role: (winner series keys, [flags])} for the chosen T1 and T1_POST acquisitions."""
    from oracles import objective_checks

    run = conv.standard()[0]
    recs = reader_records(conv, header_reader)
    out = {}
    for role in roles:
        row = run.role_row(role)
        if not isinstance(row, dict) or row["outcome"] not in ("selected", "selected_fallback"):
            continue
        members = sorted({r["_sidecar"].get("source_series_key") for r in run.members(row["candidate_id"])})
        member_recs = [recs[k] for k in members if k in recs]
        session = member_recs[0]["_session"] if member_recs else None
        same_session = [r for r in recs.values() if r["_session"] == session]
        flags = objective_checks.check_choice(role, member_recs, same_session)
        out[role] = (members, [f for f in flags if f["category"] == "contrast"], member_recs)
    return out


def _has_evidence(recs):
    return any(r.get("contrast_agent") or (r.get("bolus_rel_s") is not None and r.get("acq_rel_s") is not None
                                            and r["bolus_rel_s"] < r["acq_rel_s"]) for r in recs)


@pytest.mark.r4real
@pytest.mark.parametrize("cat_name,exam_id", [(c, e) for c, e, _ in e2e.exam_cases({"R-CON-3"})])
def test_r4_con_03_objective_contrast_check(real_exam, header_reader, cat_name, exam_id):
    conv = real_exam(cat_name, exam_id)
    want = conv.exam["expected"]["R-CON-3"]["outcome"]
    found = objective_flags(conv, header_reader)
    flags = [f for _members, fl, _recs in found.values() for f in fl]
    e2e.record("objective_contrast", {"exam": exam_id, "roles": sorted(found),
                                      "flags": [f["category"] for f in flags]})
    assert not flags, flags
    if "objective_contrast_flags" in want:
        assert [f["category"] for f in flags] == want["objective_contrast_flags"]
    if "T1_winner_has_contrast_evidence" in want:
        assert "T1" in found and _has_evidence(found["T1"][2]) is want["T1_winner_has_contrast_evidence"]
    if "T1_POST_winner_has_contrast_evidence" in want:
        assert "T1_POST" in found and _has_evidence(found["T1_POST"][2]) is want["T1_POST_winner_has_contrast_evidence"]
