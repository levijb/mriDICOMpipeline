"""TG-PROP: the four Hypothesis properties of D-6 (plan section 5, TG-PROP).

Each property is stated from its requirement, not read off the code. In CI the `ci` profile runs
them with fixed seeds (conftest.py). Unit functions: identity.parse_name and parse_key,
naming.resolve_date, selection_identity.analyze with selection_rules.decide_session, and
bids.plan_visit.
"""

from __future__ import annotations

import json
import os
import random
from datetime import date, timedelta

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

from r4lib import e2e  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{n}" for p in ("COM", "LPT") for n in range(10)}

# README: IDs are letters, digits, hyphens and underscores; read here as runs of letters and digits
# joined by single hyphens or underscores (no leading, trailing or doubled separator).
ids = st.from_regex(r"[A-Za-z0-9]{1,8}([-_][A-Za-z0-9]{1,6}){0,2}", fullmatch=True).filter(
    lambda s: s.upper() not in RESERVED)
dates = st.dates(min_value=date(1990, 1, 1), max_value=date(2099, 12, 31)).map(lambda d: d.strftime("%Y%m%d"))
suffixes = st.one_of(st.none(), st.integers(min_value=2, max_value=99))


# ---------------------------------------------------------------------------- R4-PROP-01

@given(ids, dates, suffixes)
def test_r4_prop_01_name_grammar_round_trips(sid, day, suffix):
    from mri_dicom_pipeline import identity

    label = day + (f"_{suffix}" if suffix else "")
    assert identity.parse_name(f"{sid}_{label}.zip") == (sid, day, label)
    assert identity.parse_key(f"{sid}/{label}") == (sid, label)


@given(ids, dates, st.sampled_from(["_1", "_0", "_02", "_x", ""]), st.sampled_from(["13", "00"]))
def test_r4_prop_01_forms_outside_the_grammar_are_rejected(sid, day, bad_suffix, bad_month):
    from mri_dicom_pipeline import identity

    for name in ([f"{sid}_{day}{bad_suffix}.zip"] if bad_suffix else []) + [
            f"{sid}_{day[:4]}{bad_month}{day[6:]}.zip", f"{sid}_{day[:7]}.zip", f"{sid}-{day}.zip"]:
        with pytest.raises(ValueError):
            identity.parse_name(name)


# ---------------------------------------------------------------------------- R4-PROP-04

@given(dates, st.integers(min_value=0, max_value=5), st.integers(min_value=0, max_value=8),
       st.lists(st.integers(min_value=-60, max_value=60), max_size=4))
def test_r4_prop_04_date_resolution_is_symmetric(day, tol, gap, others):
    from mri_dicom_pipeline import naming

    d = date(int(day[:4]), int(day[4:6]), int(day[6:]))
    before, after = d - timedelta(days=gap), d + timedelta(days=gap)
    listed = {day}
    for o in others:
        x = d + timedelta(days=o)
        if abs((x - before).days) > tol and abs((x - after).days) > tol:
            listed.add(x.strftime("%Y%m%d"))
    a, _ = naming.resolve_date([before.strftime("%Y%m%d")], listed, False, tol, "S1")
    b, _ = naming.resolve_date([after.strftime("%Y%m%d")], listed, False, tol, "S1")
    assert a == b


# ---------------------------------------------------------------------------- R4-PROP-02 and 03

def _cached_volumes(conv):
    """Series records and Volume objects of a converted tier 2 exam, from its header cache, built the way
    the cache documents them (7.3)."""
    from mri_dicom_pipeline import selection_identity as sid

    series, volumes = {}, []
    for doc in e2e.load_cache(conv).values():
        series.update(doc["series"])
        for _rel, r in e2e.cache_volumes(doc):
            volumes.append(r)
    return series, volumes


def _volumes(series, raw):
    from mri_dicom_pipeline import selection_identity as sid

    return [sid.Volume(relpath=r["relpath"], sha256=r["sha256"], folder=r["folder"], token=r["token"],
                       companions=r["companions"], sidecar=r["sidecar"], dims=r["dims"], gradients=r["gradients"],
                       series=series.get(r["sidecar"].get("source_series_key")), json_sha256=r["json_sha256"])
            for r in raw]


def _decide(series, raw):
    from mri_dicom_pipeline import selection_identity as sid
    from mri_dicom_pipeline import selection_rules as rules

    profile = rules.load_profile()
    session = sid.analyze(json.loads(json.dumps(series)), _volumes(json.loads(json.dumps(series)), raw), 6,
                          "presume_pre")
    decisions, fates, _info = rules.decide_session(session, profile)
    return session, decisions, fates, profile


@pytest.fixture(scope="module")
def pool(tmp_path_factory):
    pytest.importorskip("nibabel")
    base = tmp_path_factory.mktemp("prop")
    out = {}
    for cat, exam in (("structural", "STR-04-GE-SESSION-F-CONTRAST"), ("susceptibility", "SUS-01-SIEMENS-SWI-BUNDLE"),
                      ("diffusion", "DIF-01-GE-TENSOR-ADC-EADC"), ("kept_roles", "KR-01-TOF-MIP-LOCALIZER"),
                      ("hard_cases", "HC-03-REPEAT-CONSISTENT")):
        conv = e2e.build(base / exam, cat, exam, "fake")
        conv.select(base / exam / "Sel")
        out[exam] = _cached_volumes(conv)
    return out


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=40)
@given(st.randoms(use_true_random=False), st.sampled_from(["STR-04-GE-SESSION-F-CONTRAST",
                                                           "SUS-01-SIEMENS-SWI-BUNDLE", "DIF-01-GE-TENSOR-ADC-EADC",
                                                           "HC-03-REPEAT-CONSISTENT"]))
def test_r4_prop_02_decisions_do_not_depend_on_volume_order(pool, rnd, exam_id):
    series, raw = pool[exam_id]
    _s, d1, f1, _p = _decide(series, raw)
    shuffled = list(raw)
    rnd.shuffle(shuffled)
    _s, d2, f2, _p = _decide(series, shuffled)
    assert {r: (d["outcome"], d["candidate_id"], d["decided_by"]) for r, d in d1.items()} == \
        {r: (d["outcome"], d["candidate_id"], d["decided_by"]) for r, d in d2.items()}
    assert {v.relpath: dict(f) for v, f in f1.items()} == {v.relpath: dict(f) for v, f in f2.items()}


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=40)
@given(st.sets(st.sampled_from(["STR-04-GE-SESSION-F-CONTRAST", "SUS-01-SIEMENS-SWI-BUNDLE",
                                "DIF-01-GE-TENSOR-ADC-EADC", "KR-01-TOF-MIP-LOCALIZER", "HC-03-REPEAT-CONSISTENT"]),
               min_size=1), st.randoms(use_true_random=False))
def test_r4_prop_03_bids_names_never_collide(pool, exams, rnd):
    from mri_dicom_pipeline import bids, selection

    series, raw = {}, []
    for exam in sorted(exams):
        s, r = pool[exam]
        series.update(s)
        raw += [dict(x) for x in r if rnd.random() < 0.8]
    if not raw:
        return
    session, decisions, fates, profile = _decide(series, raw)
    chosen = [v for v in session.volumes if selection.copied(v, fates[v], profile)]
    try:
        plan = bids.plan_visit("S900", "20261201", chosen, fates, decisions, "unknown", lambda v: None)
    except bids.PlanError:
        return
    paths = [e["path"].lower() for e in plan if e["status"] != "omitted"]
    assert len(paths) == len(set(paths)), paths
