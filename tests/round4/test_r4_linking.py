"""TG-LNK: linking derived series (plan section 5, TG-LNK; R-LNK-1 to R-LNK-3).

Tier 1: expected/unit/linking_contrast.json "linking" (R4-LNK-01), run through
selection_identity.analyze on records built from the table's facts. A derived series is
parent_base plus the facts its case gives; a fact given as null is missing, so its rung is passed
over (5.6).
Tier 3: the catalog's R-LNK keys end to end, including census session F (R4-LNK-02, STR-04) and
the unlinked ADC map (R4-LNK-03, DIF-13).
"""

from __future__ import annotations

import json

import pytest

from r4lib import e2e, records

TABLE = records.unit_table("linking_contrast.json")["linking"]
LADDER_FIELDS = ("orientation", "rows", "cols", "n_positions", "pos_min", "pos_max", "extent_mm", "tr_ms",
                 "ti_ms", "flip_deg", "te_ms", "acq_rel_s", "series_number", "for_hash", "ref_series_keys")
GROUP_IMAGE_TYPE = {"map": ["DERIVED", "PRIMARY", "OTHER"], "projection": ["DERIVED", "PRIMARY", "MIN IP"],
                    "reformat": ["DERIVED", "SECONDARY", "REFORMATTED"]}
PARENT_PARAMS = {"DWI": dict(records.NEUTRAL_EPI, mr_acq="2D"), "SWI": dict(records.NEUTRAL_GR),
                 "T1": {"mr_acq": "3D", "scan_seq": ["GR"], "seq_var": ["SS"]},
                 "T1_POST": {"mr_acq": "3D", "scan_seq": ["GR"], "seq_var": ["SS"]}}


def _parent(name, spec):
    facts = dict(TABLE["parent_base"])
    facts.update({k: v for k, v in spec.items() if k != "role"})
    facts.setdefault("series_key", "P" + name)
    if "series_key" not in spec:
        facts["series_key"] = "P" + name
    if "for_hash" not in spec and facts["for_hash"] == TABLE["parent_base"]["for_hash"]:
        facts["for_hash"] = TABLE["parent_base"]["for_hash"]
    role = spec["role"]
    params = dict(PARENT_PARAMS[role])
    for key in ("tr_ms", "te_ms", "ti_ms"):
        params.pop(key, None)
    rec = records.series(facts.pop("series_key"), **params, **{k: v for k, v in facts.items()})
    if role == "T1_POST":
        rec["bolus_rel_s"] = (rec["acq_rel_s"] or 0.0) - 10.0
    grads = records.gradients(6) if role == "DWI" else None
    label = "T1" if role == "T1_POST" else role
    return rec, records.volume(rec, label, gradients=grads)


def _group(spec):
    """The derived series: parent_base plus the case's own facts, an explicit null kept (the table's
    worked notes, for example L07 and L09, treat a fact the case leaves out as parent_base's)."""
    facts = {k: None for k in LADDER_FIELDS}
    facts.update({k: v for k, v in TABLE["parent_base"].items() if k != "series_key"})
    facts.update({k: v for k, v in spec.items() if k not in ("kind", "role")})
    params = {"mr_acq": "3D", "scan_seq": ["GR"], "seq_var": ["SS"]}
    rec = records.series("G", image_type=GROUP_IMAGE_TYPE[spec["kind"]], **params,
                         **{k: v for k, v in facts.items() if k != "series_key"})
    return rec, records.volume(rec, spec["role"], image_type=GROUP_IMAGE_TYPE[spec["kind"]])


@pytest.mark.parametrize("case", TABLE["cases"], ids=lambda c: c["id"])
def test_r4_lnk_01_ladder(case):
    parents = {name: _parent(name, spec) for name, spec in case["parents"].items()}
    grec, gvol = _group(case["group"])
    series_list = [grec] + [p[0] for p in parents.values()]
    volumes = [gvol] + [p[1] for p in parents.values()]
    session = records.analyze(series_list, volumes)
    group = next(v for v in session.volumes if v.series is grec).group
    assert group is not None, (case["id"], "the derived volume formed no group")
    assert group.link_state == case["state"], (case["id"], group.link_state, group.link_rung)
    if "rung" in case:
        assert group.link_rung == case["rung"], (case["id"], group.link_rung)
    key_of = {name: p[0]["series_key"] for name, p in parents.items()}
    if "parent" in case:
        assert group.parent is not None and group.parent.primary.series["series_key"] == key_of[case["parent"]], \
            (case["id"], group.parent and group.parent.primary.series["series_key"])
    if "candidates" in case:
        acq_of = {p[0]["series_key"]: next(v for v in session.volumes if v.series is p[0]).acq.id
                  for p in parents.values()}
        assert sorted(group.candidates) == sorted(acq_of[key_of[n]] for n in case["candidates"]), case["id"]


@pytest.mark.r4real
@pytest.mark.parametrize("cat_name,exam_id,req", e2e.exam_cases({"R-LNK-1", "R-LNK-2", "R-LNK-3"}),
                         ids=lambda x: str(x))
def test_r4_lnk_catalog_end_to_end(real_exam, cat_name, exam_id, req):
    """R4-LNK-01 on the catalog, R4-LNK-02 (STR-04, session F) and R4-LNK-03 (DIF-13)."""
    problems = real_exam(cat_name, exam_id).check(req)
    assert not problems, json.dumps(problems, indent=1)


# Added in phase 2 after TG-SEED (fault F-LNK-1 survived the table): a case where acquisition time alone
# separates two parents. Hand-derived from scan-selection.md 5.6 ("have acq_rel_s within 1 s"): every
# earlier rung keeps both T1 parents (same frame of reference, same TR/TI/flip/TE); only B is within 1 s of
# the reformat (0.5 s; A is 50.5 s away), so the reformat links to B at acquisition_time. P only.
ACQ_TIME_CASE = {"id": "L13", "group": {"kind": "reformat", "role": "T1", "for_hash": "F1", "tr_ms": 6.0, "ti_ms": 450.0,
                                         "flip_deg": 13.0, "te_ms": 2.4, "acq_rel_s": 900.5, "series_number": 600},
                 "parents": {"A": {"role": "T1", "tr_ms": 6.0, "ti_ms": 450.0, "flip_deg": 13.0, "te_ms": 2.4,
                                   "acq_rel_s": 850.0, "series_number": 3},
                             "B": {"role": "T1", "tr_ms": 6.0, "ti_ms": 450.0, "flip_deg": 13.0, "te_ms": 2.4,
                                   "acq_rel_s": 900.0, "series_number": 6}},
                 "state": "linked", "rung": "acquisition_time", "parent": "B"}


def test_r4_lnk_01_acquisition_time_alone_decides():
    test_r4_lnk_01_ladder(ACQ_TIME_CASE)
