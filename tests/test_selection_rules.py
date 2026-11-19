"""Scan selection rules: identity, linking, contrast, gates, ranking and fates (design: scan selection, 10.1)."""
import copy
import hashlib
import random

import pytest

from mri_dicom_pipeline import selection_identity as si
from mri_dicom_pipeline import selection_rules as sr

BASE = {"series_key": "", "series_number": 1, "image_type": ["ORIGINAL", "PRIMARY"], "localizer": False,
        "mosaic": False, "manufacturer": "GE", "model": "SIGNA Premier", "field_t": 3.0, "software": "",
        "mr_acq": "3D", "scan_seq": ["GR"], "seq_var": [], "scan_opt": [], "seq_name": "", "tr_ms": None,
        "te_ms": None, "ti_ms": None, "flip_deg": None, "etl": None, "rows": 256, "cols": 256,
        "pixel_mm": [1.0, 1.0], "thick_mm": 1.0, "sbs_mm": None, "acq_matrix": [0, 256, 256, 0], "phase_dir": "ROW",
        "pct_phase_fov": None, "orientation": "SAG", "n_positions": 176, "pos_min": -88.0, "pos_max": 87.0,
        "step_mm": 1.0, "uniform_spacing": True, "extent_mm": 176.0, "for_hash": "F1", "acq_rel_s": 0.0,
        "series_rel_s": 0.0, "bolus_rel_s": None, "contrast_agent": False, "parts": {"mag": 176}, "echoes": [],
        "desc_tokens": [], "desc_phase": None, "multiframe": False, "has_image_type_text": False,
        "ref_series_keys": [], "mixed": False}


THREE_D = sr.load_profile(options={"require": {"mr_acq": "3D"}})


def rec(key, number, **fields):
    r = copy.deepcopy(BASE)
    r.update(series_key=key, series_number=number, **fields)
    return r


def ge_t1(key="t1", number=3, **kw):
    fields = dict(scan_seq=["GR"], seq_var=["SS", "SK"], scan_opt=["IR_GEMS"], tr_ms=6.0, te_ms=2.4, ti_ms=450.0,
                  flip_deg=13.0, rows=1024, cols=1024, pixel_mm=[0.234375, 0.234375], acq_matrix=[0, 240, 240, 0],
                  acq_rel_s=300.0)
    fields.update(kw)
    return rec(key, number, **fields)


def ge_flair(key="flair", number=5, **kw):
    fields = dict(scan_seq=["SE", "IR"], tr_ms=6000.0, te_ms=120.0, ti_ms=1700.0, flip_deg=90.0, rows=512, cols=512,
                  pixel_mm=[0.5, 0.5], step_mm=0.5, n_positions=352, pos_min=-88.0, pos_max=87.5, extent_mm=176.5,
                  acq_rel_s=600.0)
    fields.update(kw)
    return rec(key, number, **fields)


def ge_t2(key="t2", number=7, **kw):
    fields = dict(mr_acq="2D", scan_seq=["SE"], tr_ms=7342.0, te_ms=101.8, flip_deg=111.0, rows=512, cols=512,
                  pixel_mm=[0.449, 0.449], acq_matrix=[332, 0, 0, 256], phase_dir="COL", thick_mm=4.0, step_mm=4.0,
                  orientation="AX", n_positions=36, pos_min=-70.0, pos_max=70.0, extent_mm=144.0, acq_rel_s=900.0)
    fields.update(kw)
    return rec(key, number, **fields)


def reformat_of(source, key, number, desc_phase=None, **kw):
    fields = {k: source[k] for k in ("mr_acq", "scan_seq", "seq_var", "scan_opt", "tr_ms", "te_ms", "ti_ms",
                                     "flip_deg", "for_hash", "acq_rel_s")}
    fields.update(image_type=["DERIVED", "SECONDARY", "REFORMATTED", "AVERAGE"], orientation="AX", rows=512,
                  cols=512, pixel_mm=[0.5, 0.5], thick_mm=2.0, step_mm=2.0, n_positions=60, pos_min=-60.0,
                  pos_max=58.0, extent_mm=120.0, desc_phase=desc_phase)
    fields.update(kw)
    return rec(key, number, **fields)


def dwi(key="dwi", number=9, dirs=30, b=1000, pe="j-", **kw):
    fields = dict(mr_acq="2D", scan_seq=["EP", "SE"], tr_ms=8000.0, te_ms=85.0, flip_deg=90.0, rows=256, cols=256,
                  pixel_mm=[0.9375, 0.9375], acq_matrix=[128, 0, 0, 128], phase_dir="COL", thick_mm=4.0, step_mm=4.0,
                  orientation="AX", n_positions=36, pos_min=-70.0, pos_max=70.0, extent_mm=144.0, acq_rel_s=1200.0)
    fields.update(kw)
    return rec(key, number, **fields), gradients(dirs, b), pe


def gradients(dirs, b=1000, b0=1):
    vectors = []
    for i in range(dirs):
        z = 1 - (i + 0.5) / dirs  # one hemisphere, so no two vectors are antipodal
        r = (1 - z * z) ** 0.5
        phi = i * 2.399963
        vectors.append((r * __import__("math").cos(phi), r * __import__("math").sin(phi), z))
    bvals = [0] * b0 + [b] * dirs
    rows = [[0.0] * b0 + [v[axis] for v in vectors] for axis in range(3)]
    return si.gradient_stats(bvals, rows, len(bvals))


def vol(r, folder, te=None, image_type=None, dims=None, grads=None, companions=(), pe=None, token=None):
    image_type = list(image_type or r["image_type"])
    token = token or hashlib.sha256(f"{r['series_key']}|{folder}|{te}|{image_type}".encode()).hexdigest()[:16]
    sidecar = {"source_series_key": r["series_key"], "SeriesNumber": r["series_number"], "ImageType": image_type}
    echo = te if te is not None else r.get("te_ms")
    if echo is not None:
        sidecar["EchoTime"] = echo / 1000
    if pe:
        sidecar["PhaseEncodingDirection"] = pe
    if dims is None:
        dims = [3, r.get("cols") or 10, r.get("rows") or 10, r.get("n_positions") or 10, 1, 1, 1, 1]
        if grads:
            dims = [4, dims[1], dims[2], dims[3], grads["n_volumes"], 1, 1, 1]
    return si.Volume(relpath=f"Subjects/S001/20260725/{folder}/S001_20260725_{folder}_{token}.nii.gz",
                     sha256=token * 4, folder=folder, token=token, companions=list(companions), sidecar=sidecar,
                     dims=dims, gradients=grads, series=r)


def dwi_vol(spec, folder="DWI", companions=()):
    r, grads, pe = spec
    return vol(r, folder, grads=grads, pe=pe, companions=companions)


def decide(volumes, profile=None, overrides=None):
    series = {v.series["series_key"]: v.series for v in volumes if v.series}
    session = si.analyze(series, volumes)
    decisions, fates, info = sr.decide_session(session, profile or sr.load_profile(), overrides)
    check_invariant(session, decisions, fates)
    return session, decisions, fates, info


def check_invariant(session, decisions, fates):
    """Each ranked or derived role folder holds at most one acquisition or derived group (6.6)."""
    owners = {}
    for v in session.volumes:
        fate = fates[v]
        if fate["fate"] in ("selected", "selected_derived") and v.role not in si.KEPT_ROLES:
            owners.setdefault(fate["folder"], set())
            if fate["fate"] == "selected":
                owners[fate["folder"]].add(v.acq.id)
            elif v.group.parent is None or v.group.parent.id not in {a.id for a in session.acquisitions
                                                                     if any(fates[m]["fate"] == "selected" for m in a.members)}:
                owners[fate["folder"]].add(v.group.id)
    assert all(len(ids) <= 1 for ids in owners.values()), owners


def by_series(session, key):
    return [v for v in session.volumes if v.series and v.series["series_key"] == key]


def fate_of(session, fates, key):
    return {fates[v]["fate"] for v in by_series(session, key)}, {fates[v]["folder"] for v in by_series(session, key)}


# ---- identity ----------------------------------------------------------------------------------------------

def test_ge_3d_t1_native_family_and_native_resolution():
    r = ge_t1()
    assert si.structural_family({"mr_acq": "3D", "scan_seq": r["scan_seq"], "seq_var": r["seq_var"],
                                 "scan_opt": r["scan_opt"], "tr": 6.0, "te": 2.4, "ti": 450.0}) == "T1_3D_IRGRE"
    sampling = si.native_sampling(r)
    assert sampling["inplane"] == [(1.0, 1.0)] and sampling["max"] == 1.0 and sampling["interpolated"]


@pytest.mark.parametrize('params, family', [
    # Siemens MPRAGE exported without IR or MP: the inversion time shows the preparation.
    ({'mr_acq': '3D', 'scan_seq': ['GR'], 'seq_var': ['SK'], 'tr': 1600.0, 'te': 2.68, 'ti': 900.0}, 'T1_3D_IRGRE'),
    # GE research mode gives RM instead of GR.
    ({'mr_acq': '3D', 'scan_seq': ['RM', 'IR'], 'seq_var': ['NONE'], 'tr': 2462.1, 'te': 2.84, 'ti': 1000.0},
     'T1_3D_IRGRE'),
    # Philips 3D T1 TFE: magnetization prepared, no inversion time, the TR of one shot.
    ({'mr_acq': '3D', 'scan_seq': ['GR'], 'seq_var': ['MP'], 'tr': 6.5, 'te': 2.95, 'ti': None}, 'T1_3D_IRGRE'),
    # Philips inversion-recovery spin echo gives IR without SE: T1-FLAIR and FLAIR.
    ({'mr_acq': '2D', 'scan_seq': ['IR'], 'seq_var': ['SK'], 'tr': 2000.0, 'te': 20.0, 'ti': 800.0}, 'T1_2D_IR'),
    ({'mr_acq': '2D', 'scan_seq': ['IR'], 'seq_var': ['SK'], 'tr': 11000.0, 'te': 125.0, 'ti': 2800.0}, 'FLAIR_2D'),
    # Siemens SPACE FLAIR exported without IR: an inversion time is not a T2.
    ({'mr_acq': '3D', 'scan_seq': ['SE'], 'seq_var': ['SP', 'SK'], 'tr': 7000.0, 'te': 428.0, 'ti': 2050.0}, 'FLAIR_3D'),
    # GE research-mode fast spin echo.
    ({'mr_acq': '2D', 'scan_seq': ['RM'], 'seq_var': ['OSP'], 'tr': 4976.0, 'te': 124.0, 'ti': None}, 'T2_2D_TSE'),
    # Unchanged: TI 0 is no inversion; a short-TE spin echo, a T2* GRE and a 2D FLASH are no structural family.
    ({'mr_acq': '2D', 'scan_seq': ['SE'], 'seq_var': ['SK'], 'tr': 4500.0, 'te': 100.0, 'ti': 0.0}, 'T2_2D_TSE'),
    ({'mr_acq': '2D', 'scan_seq': ['SE'], 'seq_var': ['SK'], 'tr': 4050.0, 'te': 9.0, 'ti': 0.0}, None),
    ({'mr_acq': '2D', 'scan_seq': ['GR'], 'seq_var': ['SP'], 'tr': 603.0, 'te': 20.0, 'ti': None}, None),
    ({'mr_acq': '2D', 'scan_seq': ['GR'], 'seq_var': ['SP'], 'tr': 250.0, 'te': 2.46, 'ti': None}, None),
    ({'mr_acq': '3D', 'scan_seq': ['GR'], 'seq_var': ['SP'], 'tr': 25.0, 'te': 4.0, 'ti': None}, 'T1_3D_GRE'),
])
def test_structural_family_vendor_conventions(params, family):
    """Header conventions from real GE, Siemens and Philips exports (2026-11-19)."""
    assert si.structural_family(params) == family


def test_agent_tag_on_every_series_is_judged_per_session_of_a_visit():
    """In a visit each session is its own exam: the agent tag on all of one session's series is exam-wide."""
    plain = {'series_key': 'a', 'contrast_agent': False}
    tagged = [{'series_key': k, 'contrast_agent': True} for k in ('b', 'c')]
    assert si.series_phases([plain, *tagged])['b'] == ('post', 'agent_tag')  # one exam
    visit = [dict(plain, visit_part=0), *(dict(s, visit_part=1) for s in tagged)]
    assert si.series_phases(visit)['b'] == ('unknown', 'agent_tag_exam_wide')


def test_native_sampling_phase_direction_and_missing_fields():
    flair = rec("f", 1, rows=256, cols=224, pixel_mm=[1.0, 1.0], acq_matrix=[256, 0, 0, 202], phase_dir="ROW",
                thick_mm=1.05)
    row, col = si.native_sampling(flair)["inplane"][0]
    assert round(row, 2) == 1.11 and col == 1.0
    assert si.native_sampling(dict(flair, phase_dir="COL"))["inplane"][0] == (224 / 256, 256 / 202)
    both = si.native_sampling(dict(flair, phase_dir=None))["inplane"]
    assert len(both) == 2
    tier = {"name": "3d", "families": ["FLAIR_3D"], "max_in_plane_mm": 1.2, "max_slice_mm": 1.5}
    assert sr.assign_tier("FLAIR_3D", [tier], si.native_sampling(dict(flair, phase_dir=None)))[0] is None
    assert sr.assign_tier("FLAIR_3D", [tier], si.native_sampling(dict(flair, phase_dir=None)))[1] == ["native_unknown"]
    assert sr.assign_tier("FLAIR_3D", [tier], si.native_sampling(dict(flair, acq_matrix=None)))[1] == ["native_unknown"]
    assert sr.assign_tier("FLAIR_3D", [dict(tier, max_in_plane_mm=0.5)], si.native_sampling(flair))[1] == ["too_coarse"]


def test_role_order_keeps_specialized_labels_and_flags_conflicts():
    qsm = rec("q", 2, mr_acq="3D", scan_seq=["GR"], tr_ms=40.0, flip_deg=15.0, echoes=[
        {"te_ms": 4.0, "echo_number": 1, "n_instances": 60}, {"te_ms": 12.0, "echo_number": 2, "n_instances": 60}])
    tof = rec("tof", 3, mr_acq="3D", scan_seq=["GR"], tr_ms=22.0, te_ms=3.4, flip_deg=18.0)
    flair_as_t1 = ge_flair("ft1", 4)
    unknown = rec("u", 5, mr_acq=None, scan_seq=[])
    session, decisions, fates, _ = decide([vol(qsm, "QSM", te=4.0), vol(qsm, "QSM", te=12.0), vol(tof, "MRA"),
                                           vol(flair_as_t1, "T1"), vol(unknown, "T1")])
    q = by_series(session, "q")
    assert {v.role for v in q} == {"QSM"}  # multi-echo, so never T1_3D_GRE
    [t] = by_series(session, "tof")
    assert t.role == "MRA" and fates[t]["fate"] == "kept" and "label_parameter_conflict" in t.warnings
    [f] = by_series(session, "ft1")
    assert f.role == "FLAIR" and "label_parameter_conflict" in f.warnings
    assert decisions["FLAIR"]["outcome"] == "review_required" and "label_parameter_conflict" in decisions["FLAIR"]["reasons"]
    [u] = by_series(session, "u")
    assert u.role == "T1" and u.family == "UNCLASSIFIED"


def test_diffusion_b0_is_not_t2_and_gradients_make_it_dwi():
    b0 = rec("b0", 2, mr_acq="2D", scan_seq=["EP", "SE"], tr_ms=6000.0, te_ms=80.0)
    assert si.structural_family({"mr_acq": "2D", "scan_seq": ["EP", "SE"], "tr": 6000.0, "te": 80.0}) is None
    session, *_ = decide([vol(b0, "OTHER", grads=si.gradient_stats([0, 0], [[0, 0]] * 3, 2))])
    assert session.volumes[0].role == "DWI" and session.volumes[0].family == "DWI_B0_ONLY"


def test_dual_echo_series_is_one_acquisition_and_unmatched_echo_is_missing_evidence():
    r = rec("pdt2", 4, mr_acq="2D", scan_seq=["SE"], tr_ms=3000.0, flip_deg=90.0, orientation="AX",
            echoes=[{"te_ms": 12.0, "echo_number": 1, "n_instances": 30},
                    {"te_ms": 96.0, "echo_number": 2, "n_instances": 50}], n_positions=50, thick_mm=3.0, step_mm=3.0,
            extent_mm=150.0, pos_min=0.0, pos_max=147.0)
    session, decisions, _, _ = decide([vol(r, "T2", te=12.0), vol(r, "T2", te=96.0)])
    pd, t2 = sorted(session.volumes, key=lambda v: v.te)
    assert pd.family == "UNCLASSIFIED" and t2.family == "T2_2D_TSE" and pd.acq is t2.acq
    assert t2.acq.family == "T2_2D_TSE" and decisions["T2"]["outcome"] == "selected"
    session, *_ = decide([vol(r, "T2", te=50.0)])
    assert "metadata_unavailable" in session.volumes[0].issues


def test_derived_kinds():
    t1 = ge_t1()
    adc = rec("adc", 901, mr_acq="2D", scan_seq=["EP", "SE"], image_type=["ORIGINAL", "PRIMARY", "OTHER"])
    tracew = rec("tw", 902, mr_acq="2D", image_type=["ORIGINAL", "PRIMARY", "DIFFUSION", "TRACEW"])
    minip = rec("mip", 903, image_type=["DERIVED", "PRIMARY", "MIN IP"])
    session, *_ = decide([vol(t1, "T1"), vol(reformat_of(t1, "ref", 300), "T1"), vol(adc, "ADC"),
                          vol(tracew, "DWI"), vol(minip, "SWI_MINIP")])
    kinds = {v.series["series_key"]: v.kind for v in session.volumes}
    assert kinds == {"t1": "native", "ref": "reformat", "adc": "map", "tw": "map", "mip": "projection"}
    assert "derived_by_role" in by_series(session, "adc")[0].warnings


def test_gradient_statistics():
    tensor = gradients(30)
    assert tensor["n_dirs"] == 30 and tensor["b0"] == 1 and tensor["shells"] == [1000] and not tensor["mismatch"]
    trace = si.gradient_stats([0, 1000, 1000, 1000], [[0, 0, 0, 0]] * 3, 4)
    assert trace["n_dirs"] == 0 and si.dwi_family(trace, 6) == "DWI_TRACE"
    antipodal = si.gradient_stats([1000, 1000], [[1, -1], [0, 0], [0, 0]], 2)
    assert antipodal["n_dirs"] == 1
    assert si.gradient_stats([0, 40, 1000], [[0, 0, 1], [0, 0, 0], [0, 0, 0]], 3)["b0"] == 2
    assert si.gradient_stats([0, 1000], [[0, 1], [0, 0], [0, 0]], 3)["mismatch"]
    assert si.dwi_family(None, 6) == "DWI_UNKNOWN"


def test_grouping_magnitude_phase_echoes_and_duplicates():
    mag = rec("mag", 10, tr_ms=40.0, flip_deg=15.0, orientation="AX", n_positions=60, pos_min=0.0, pos_max=59.0,
              acq_rel_s=2000.0, echoes=[{"te_ms": t, "echo_number": i + 1, "n_instances": 60}
                                        for i, t in enumerate((5.0, 10.0, 15.0, 20.0))])
    phase = dict(copy.deepcopy(mag), series_key="pha", series_number=11, parts={"phase": 240})
    vols = [vol(mag, "SWI", te=t) for t in (5.0, 10.0, 15.0, 20.0)]
    vols += [vol(phase, "SWI_PHASE", te=t, image_type=["ORIGINAL", "PRIMARY", "P", "ND"]) for t in (5.0, 10.0, 15.0, 20.0)]
    session, decisions, fates, _ = decide(vols)
    [acq] = session.acquisitions
    assert acq.has_phase and acq.n_echoes == 4 and len(acq.members) == 8
    assert decisions["SWI"]["outcome"] == "selected"
    assert {fates[v]["folder"] for v in vols if v.part == "phase"} == {"SWI_PHASE"}
    t1 = ge_t1()
    copy_t1 = dict(copy.deepcopy(t1), series_key="t1copy", series_number=1)
    session, decisions, *_ = decide([vol(t1, "T1"), vol(copy_t1, "OTHER")])
    assert decisions["T1"]["outcome"] == "review_required" and decisions["T1"]["reasons"] == ["duplicate_series"]
    lower = next(a for a in session.acquisitions if a.primary.series["series_key"] == "t1copy")
    assert decisions["T1"]["rule_recommendation"] == lower.id


def test_linking_ladder():
    pre, post = ge_t1("pre", 3, acq_rel_s=300.0), ge_t1("post", 12, acq_rel_s=1800.0, contrast_agent=True)
    ref_pre = reformat_of(pre, "rpre", 300, desc_phase="pre")
    ref_post = reformat_of(post, "rpost", 1200)
    session, *_ = decide([vol(pre, "T1"), vol(post, "T1_POST"), vol(ref_pre, "T1"), vol(ref_post, "T1_POST")])
    groups = {g.series["series_key"]: g for g in session.groups}
    assert groups["rpre"].link_state == "linked" and groups["rpre"].link_rung == "acquisition_time"
    assert groups["rpre"].parent.primary.series["series_key"] == "pre"
    assert groups["rpost"].parent.primary.series["series_key"] == "post"
    a, b = dwi("a", 9, dirs=3), dwi("b", 10, dirs=30)
    same = dict(copy.deepcopy(a[0]), series_key="adc", series_number=901, image_type=["ORIGINAL", "PRIMARY", "OTHER"])
    session, *_ = decide([dwi_vol(a), dwi_vol(b), vol(same, "ADC")])
    assert session.groups[0].link_state == "ambiguous"
    foreign = dict(copy.deepcopy(same), for_hash="F2")
    session, *_ = decide([dwi_vol(a), vol(foreign, "ADC")])
    assert session.groups[0].link_state == "unlinked" and session.groups[0].link_rung == "frame_of_reference"
    skipped = dict(copy.deepcopy(same), for_hash=None)
    session, *_ = decide([dwi_vol(dwi("c", 9, dirs=3, rows=128, cols=128)), dwi_vol(a), vol(skipped, "ADC")])
    assert session.groups[0].link_rung == "geometry"
    referenced = dict(copy.deepcopy(same), ref_series_keys=["b"])
    session, *_ = decide([dwi_vol(a), dwi_vol(b), vol(referenced, "ADC")])
    assert session.groups[0].link_rung == "reference" and session.groups[0].parent.primary.series["series_key"] == "b"
    swi = rec("swi", 10, tr_ms=40.0, te_ms=20.0, flip_deg=15.0, orientation="AX", n_positions=60, pos_min=0.0,
              pos_max=106.2, step_mm=1.8, thick_mm=1.8, extent_mm=108.0)
    minip = rec("mip", 11, tr_ms=40.0, te_ms=20.0, flip_deg=15.0, orientation="AX", n_positions=14, pos_min=4.0,
                pos_max=100.0, thick_mm=8.0, step_mm=7.4, image_type=["DERIVED", "PRIMARY", "MIN IP"])
    session, *_ = decide([vol(swi, "SWI"), vol(rec("other", 20, mr_acq="2D", orientation="COR", tr_ms=40.0,
                                                   te_ms=20.0, flip_deg=15.0), "SWI"), vol(minip, "SWI_MINIP")])
    assert session.groups[0].link_state == "linked" and session.groups[0].link_rung == "z_range"


def test_contrast_rules():
    t1 = ge_t1(bolus_rel_s=100.0)
    session, *_ = decide([vol(t1, "T1")])
    assert (session.acquisitions[0].phase, session.acquisitions[0].evidence) == ("post", "bolus_time")
    session, *_ = decide([vol(ge_t1(bolus_rel_s=400.0), "T1")])
    assert (session.acquisitions[0].phase, session.acquisitions[0].evidence) == ("pre", "bolus_time")
    everything = [vol(ge_t1(contrast_agent=True), "T1"), vol(ge_t2(contrast_agent=True), "T2")]
    session, decisions, *_ = decide(everything)
    assert {(a.phase, a.evidence) for a in session.acquisitions} == {("unknown", "agent_tag_exam_wide")}
    assert decisions["T1"]["outcome"] == "review_required" and decisions["T1"]["reasons"] == ["contrast_unknown"]
    session, decisions, *_ = decide([vol(ge_t1(desc_phase="post"), "T1")])
    assert decisions["T1_POST"]["outcome"] == "selected" and session.acquisitions[0].evidence == "description"
    timing = [vol(ge_t1(), "T1"), vol(ge_t2(contrast_agent=True), "T2"), vol(ge_flair(acq_rel_s=1500.0, number=8), "FLAIR")]
    session, decisions, *_ = decide(timing)
    phases = {a.role: (a.phase, a.evidence) for a in session.acquisitions}
    assert phases == {"T1": ("pre", "timing"), "T2": ("post", "agent_tag"), "FLAIR": ("post", "timing")}
    assert "contrast_by_timing" in decisions["T1"]["warnings"]
    review = sr.load_profile({"contrast_unknown": "review"})
    session = si.analyze({"t1": ge_t1()}, [vol(ge_t1(), "T1")], contrast_unknown="review")
    decisions, _, _ = sr.decide_session(session, review)
    assert decisions["T1"]["outcome"] == "review_required" and decisions["T1"]["reasons"] == ["contrast_unknown"]


def test_mixed_series_volumes_are_set_aside_unless_chosen():
    t1, flair = ge_t1(), ge_flair()
    mixed = {"series_key": "copy", "series_number": 1, "manufacturer": "GE", "model": "", "field_t": 3.0,
             "n_instances": 723, "n_stacks": 13, "mixed": True}
    copy_t1 = si.Volume(relpath="Subjects/S001/20260725/OTHER/S001_20260725_OTHER_c1.nii.gz", sha256="c1" * 32,
                        folder="OTHER", token="c1", companions=[], dims=[3, 1024, 1024, 176],
                        gradients=None, series=mixed,
                        sidecar={"source_series_key": "copy", "MRAcquisitionType": "3D", "ScanningSequence": "GR_IR",
                                 "SequenceVariant": "SS_SK", "RepetitionTime": 0.006, "EchoTime": 0.0024,
                                 "InversionTime": 0.45, "ImageType": ["ORIGINAL", "PRIMARY"]})
    session, decisions, fates, info = decide([vol(t1, "T1"), vol(flair, "FLAIR"), copy_t1])
    assert copy_t1.role == "T1" and "mixed_series" in copy_t1.issues
    assert decisions["T1"]["decided_by"] == "only_eligible" and fates[copy_t1]["fate"] == "excluded"
    session, decisions, *_ = decide([copy_t1])
    assert decisions["T1"]["outcome"] == "review_required"
    chosen = {"T1": {"choose": copy_t1.acq.id, "none": False, "exclude": set()}}
    session, decisions, fates, _ = decide([copy_t1], overrides=chosen)
    assert decisions["T1"]["outcome"] == "selected" and fates[copy_t1]["folder"] == "T1"


# ---- census sessions ------------------------------------------------------------------------------------------

def census_ge_session():
    t1, flair, t2 = ge_t1(), ge_flair(), ge_t2()
    return [vol(t1, "T1"), vol(reformat_of(t1, "t1ax", 300, desc_phase="pre"), "T1"), vol(flair, "FLAIR"),
            vol(reformat_of(flair, "flax", 500), "FLAIR"), vol(reformat_of(flair, "flcor", 501, orientation="COR"), "FLAIR"),
            vol(t2, "T2")]


def test_census_ge_session():
    session, decisions, fates, _ = decide(census_ge_session())
    assert decisions["T1"]["outcome"] == "selected" and decisions["T1"]["decided_by"] == "only_eligible"
    assert decisions["FLAIR"]["outcome"] == "selected" and "overlapping_slices" in decisions["FLAIR"]["warnings"]
    assert (decisions["T2"]["outcome"], decisions["T2"]["tier"][1]["name"]) == ("selected_fallback", "2d_clinical")
    assert fate_of(session, fates, "t1ax") == ({"reformat"}, {"OTHER/T1"})
    assert fate_of(session, fates, "flcor") == ({"reformat"}, {"OTHER/FLAIR"})
    assert next(a for a in session.acquisitions if a.role == "T1").evidence == "reformat_description"
    assert "interpolated_grid" in decisions["T1"]["warnings"]


def test_census_session_f_pre_and_post_t1():
    pre, post = ge_t1("pre", 3), ge_t1("post", 12, acq_rel_s=1800.0, contrast_agent=True)
    t1flair = rec("t1flair", 14, mr_acq="2D", scan_seq=["SE", "IR"], tr_ms=2000.0, te_ms=21.0, ti_ms=889.0,
                  contrast_agent=True, rows=512, cols=512, pixel_mm=[0.45, 0.45], acq_matrix=[320, 0, 0, 224],
                  phase_dir="COL", thick_mm=5.0, step_mm=6.0, orientation="AX", n_positions=24, extent_mm=143.0,
                  acq_rel_s=2100.0)
    vols = [vol(pre, "T1"), vol(reformat_of(pre, "rpre", 300, desc_phase="pre"), "T1"), vol(post, "T1_POST"),
            vol(reformat_of(post, "rpost1", 1200), "T1_POST"), vol(reformat_of(post, "rpost2", 1201), "T1_POST"),
            vol(t1flair, "T1_POST"), vol(ge_t2(contrast_agent=True), "T2")]
    session, decisions, fates, _ = decide(vols)
    assert decisions["T1"]["candidate_id"] == by_series(session, "pre")[0].acq.id
    assert decisions["T1_POST"]["candidate_id"] == by_series(session, "post")[0].acq.id
    assert decisions["T1_POST"]["decided_by"] == "tier"
    assert fate_of(session, fates, "t1flair") == ({"alternative"}, {"OTHER/T1_POST"})
    assert fate_of(session, fates, "rpost1") == ({"reformat"}, {"OTHER/T1_POST"})


def test_census_session_g_split_mpr_and_presumed_precontrast():
    t1 = ge_t1(manufacturer="SIEMENS", tr_ms=2200.0, ti_ms=900.0, te_ms=2.9, scan_seq=["GR", "IR"], seq_var=["SK", "SP", "MP"],
               scan_opt=[], image_type=["ORIGINAL", "PRIMARY", "M", "NORM", "DIS2D"], rows=256, cols=256,
               pixel_mm=[1.0, 1.0], acq_matrix=[0, 256, 256, 0])
    mpr = reformat_of(t1, "mpr", 301, image_type=["DERIVED", "SECONDARY", "MPR", "CSA MPR", "NORM", "DIS2D"])
    one, big = vol(mpr, "T1", dims=[3, 484, 484, 1], token="a" * 16), vol(mpr, "T1", dims=[3, 256, 256, 177], token="b" * 16)
    session, decisions, fates, _ = decide([vol(t1, "T1"), one, big])
    assert decisions["T1"]["outcome"] == "selected" and "contrast_presumed_pre" in decisions["T1"]["warnings"]
    assert {fates[one]["folder"], fates[big]["folder"]} == {"OTHER/T1"}


def session_d(**t1_kw):
    common = dict(manufacturer="SIEMENS", field_t=1.5, mr_acq="2D", rows=192, cols=256, pixel_mm=[1.25, 1.25],
                  acq_matrix=[256, 0, 0, 192], phase_dir="COL", thick_mm=5.0, step_mm=5.5)
    t1ax = rec("t1ax", 2, scan_seq=["SE"], tr_ms=540.0, te_ms=12.0, orientation="AX", n_positions=24, extent_mm=131.5,
               acq_rel_s=100.0, **common)
    t1sag = rec("t1sag", 3, scan_seq=["SE"], tr_ms=540.0, te_ms=12.0, orientation="SAG", n_positions=25,
                extent_mm=137.0, acq_rel_s=200.0, **common)
    flairs = [rec(f"fl{o}", 4 + i, scan_seq=["SE", "IR"], tr_ms=8000.0, te_ms=100.0, ti_ms=2500.0, orientation=o,
                  n_positions=24, extent_mm=131.5, acq_rel_s=300.0 + i * 100, **common)
              for i, o in enumerate(("COR", "AX", "SAG"))]
    return [vol(t1ax, "T1"), vol(t1sag, "T1")] + [vol(f, "FLAIR") for f in flairs]


def test_census_session_d_2d_fallbacks_and_3d_only():
    session, decisions, fates, _ = decide(session_d())
    for role, tier in (("T1", "2d"), ("FLAIR", "2d_legacy")):
        d = decisions[role]
        assert (d["outcome"], d["decided_by"], d["tier"][1]["name"]) == ("selected_fallback", "orientation", tier)
        assert d["winner"].primary.s("orientation") == "AX"
    session, decisions, fates, _ = decide(session_d() + [dwi_vol(dwi())], profile=THREE_D)
    for role in ("T1", "FLAIR"):
        assert decisions[role]["outcome"] == "no_eligible_candidate"
        assert decisions[role]["reasons"][0].startswith("not_3d(")
    assert decisions["DWI"]["outcome"] == "selected"
    assert {fates[v]["folder"] for v in session.volumes if v.role == "FLAIR"} == {"OTHER/FLAIR"}
    off = sr.load_profile({"roles": {"T1": {"tiers": [sr.DEFAULT_PROFILE["roles"]["T1"]["tiers"][0]]}}})
    _, decisions, _, _ = decide(session_d(), profile=off)
    assert decisions["T1"]["outcome"] == "no_eligible_candidate" and decisions["T1"]["reasons"] == ["family_not_enabled(2)"]


def test_coarse_3d_t1_falls_back_to_the_any_tier():
    _, decisions, _, _ = decide([vol(ge_t1(thick_mm=1.5), "T1")])
    assert (decisions["T1"]["outcome"], decisions["T1"]["tier"][1]["name"]) == ("selected_fallback", "any")


def test_artist_flair_with_overlapping_slices_is_3d_tier():
    flair = ge_flair(thick_mm=1.39, step_mm=0.7, rows=256, cols=256, pixel_mm=[1.0, 1.0], acq_matrix=[0, 250, 250, 0])
    _, decisions, _, _ = decide([vol(flair, "FLAIR")])
    assert decisions["FLAIR"]["tier"][1]["name"] == "3d" and "overlapping_slices" in decisions["FLAIR"]["warnings"]


# ---- ranking and repeats -------------------------------------------------------------------------------------

def test_repeats_follow_the_repeat_policy():
    first, second = ge_t1("a", 3, acq_rel_s=300.0, for_hash="F1"), ge_t1("b", 7, acq_rel_s=900.0, for_hash="F2")
    session, decisions, fates, _ = decide([vol(first, "T1"), vol(second, "T1")])
    later = by_series(session, "b")[0].acq.id
    assert decisions["T1"]["outcome"] == "review_required" and decisions["T1"]["reasons"] == ["repeat_needs_review"]
    assert decisions["T1"]["rule_recommendation"] == later
    assert {fates[v]["fate"] for v in session.volumes} == {"pending_review"}
    for policy, key in (("later", "b"), ("earlier", "a")):
        session, decisions, _, _ = decide([vol(first, "T1"), vol(second, "T1")],
                                          profile=sr.load_profile({"repeat_policy": policy}))
        assert decisions["T1"]["candidate_id"] == by_series(session, key)[0].acq.id
        assert "repeat_order_tiebreak" in decisions["T1"]["warnings"]
    swapped = ge_t1("b", 1, acq_rel_s=900.0, for_hash="F2")
    _, decisions, _, _ = decide([vol(first, "T1"), vol(swapped, "T1")])
    assert decisions["T1"]["reasons"] == ["order_unreliable"]


def test_t2_tiers_and_contrast_preference():
    thin = ge_t2("thin", 8, thick_mm=3.0, step_mm=3.0, acq_rel_s=950.0, for_hash="F2")
    session, decisions, _, _ = decide([vol(ge_t2(), "T2"), vol(thin, "T2")])
    assert decisions["T2"]["outcome"] == "selected" and decisions["T2"]["decided_by"] == "tier"
    post = ge_t2("post", 15, acq_rel_s=2000.0, contrast_agent=True, for_hash="F3")
    session, decisions, fates, _ = decide([vol(ge_t2(), "T2"), vol(post, "T2"), vol(ge_t1(), "T1")])
    assert decisions["T2"]["decided_by"] == "contrast"
    assert fate_of(session, fates, "post") == ({"alternative"}, {"OTHER/T2"})


def test_missing_field_never_decides():
    a = ge_flair("a", 5, thick_mm=2.0, for_hash="F1")
    b = ge_flair("b", 6, thick_mm=2.0, acq_matrix=None, acq_rel_s=900.0, for_hash="F2")
    _, decisions, _, _ = decide([vol(a, "FLAIR"), vol(b, "FLAIR")])
    assert decisions["FLAIR"]["reasons"] == ["undecidable_missing_field"]


def test_siemens_variant_preference():
    base = dict(manufacturer="SIEMENS", acq_rel_s=300.0)
    dis = ge_t1("dis", 3, image_type=["ORIGINAL", "PRIMARY", "M", "DIS2D"], **base)
    nd = ge_t1("nd", 4, image_type=["ORIGINAL", "PRIMARY", "M", "ND"], for_hash="F2", **base)
    _, decisions, _, _ = decide([vol(dis, "T1"), vol(nd, "T1")])
    assert decisions["T1"]["decided_by"] == "variant"
    assert decisions["T1"]["winner"].primary.series["series_key"] == "dis"
    norm = ge_t1("norm", 5, image_type=["ORIGINAL", "PRIMARY", "M", "DIS2D", "NORM"], for_hash="F3", **base)
    profile = sr.load_profile({"roles": {"T1": {"prefer_normalized": True}}})
    _, decisions, _, _ = decide([vol(dis, "T1"), vol(norm, "T1")], profile=profile)
    assert decisions["T1"]["winner"].primary.series["series_key"] == "norm"


# ---- diffusion -----------------------------------------------------------------------------------------------

def test_tensor_beats_trace_and_the_trace_adc_goes_with_its_source():
    dti, trace = dwi("dti", 9, dirs=30), dwi("trace", 10, dirs=3, rows=128, cols=128, acq_rel_s=1500.0)
    adc = dict(copy.deepcopy(trace[0]), series_key="adc", series_number=1001,
               image_type=["DERIVED", "PRIMARY", "DIFFUSION", "ADC"])
    session, decisions, fates, _ = decide([dwi_vol(dti), dwi_vol(trace), vol(adc, "ADC")])
    assert decisions["DWI"]["winner"].primary.series["series_key"] == "dti" and decisions["DWI"]["decided_by"] == "tier"
    assert fate_of(session, fates, "trace") == ({"alternative"}, {"OTHER/DWI"})
    assert decisions["ADC"]["outcome"] == "no_eligible_candidate" and decisions["ADC"]["reasons"] == ["source_not_selected"]
    assert fate_of(session, fates, "adc") == ({"alternative"}, {"OTHER/ADC"})


def test_scanner_maps_follow_the_single_dwi():
    spec = dwi(dirs=3)
    adc = dict(copy.deepcopy(spec[0]), series_key="adc", series_number=901, image_type=["ORIGINAL", "PRIMARY", "OTHER"],
               acq_rel_s=1300.0)
    eadc = dict(copy.deepcopy(adc), series_key="eadc", series_number=902)
    companion = [{"name": "DWI/x_ADC.nii.gz", "kind": "adc", "sha256": "0" * 64}]
    session, decisions, fates, _ = decide([dwi_vol(spec, companions=companion), vol(adc, "ADC"), vol(eadc, "EADC")])
    for role in ("ADC", "EADC"):
        assert (decisions[role]["outcome"], decisions[role]["decided_by"]) == ("selected", "follows_source")
        assert "derived_by_role" in decisions[role]["warnings"]
    assert fate_of(session, fates, "adc") == ({"selected_derived"}, {"ADC"})
    assert fate_of(session, fates, "dwi") == ({"selected"}, {"DWI"})


def test_ambiguous_map_and_equal_dwis_go_to_review():
    a, b = dwi("a", 9, dirs=30), dwi("b", 10, dirs=30, acq_rel_s=1900.0, for_hash="F1")
    adc = dict(copy.deepcopy(a[0]), series_key="adc", series_number=901, acq_rel_s=None,
               image_type=["DERIVED", "PRIMARY", "ADC"], te_ms=None, tr_ms=None)
    session, decisions, fates, _ = decide([dwi_vol(a), dwi_vol(b), vol(adc, "ADC")])
    assert decisions["DWI"]["outcome"] == "review_required"
    assert decisions["ADC"]["reasons"] == ["parent_review"]
    assert fate_of(session, fates, "adc") == ({"pending_review"}, {"OTHER/ADC"})


def test_dwi_without_gradients_needs_review():
    r = dwi()[0]
    session, decisions, *_ = decide([vol(r, "DWI", image_type=["ORIGINAL", "PRIMARY", "DIFFUSION"])])
    assert decisions["DWI"]["outcome"] == "review_required" and decisions["DWI"]["reasons"] == ["no_gradient_table(1)"]


def test_reverse_phase_encode_partner():
    main = dwi(pe="j-")
    rev_r = dict(copy.deepcopy(main[0]), series_key="rev", series_number=10, acq_rel_s=1500.0)
    b0 = si.gradient_stats([0, 0], [[0, 0]] * 3, 2)
    for pe, expected in (("j", ({"companion"}, {"DWI"})), ("j-", ({"excluded"}, {"OTHER/DWI"})),
                         (None, ({"excluded"}, {"OTHER/DWI"}))):
        session, decisions, fates, info = decide([dwi_vol(main), vol(rev_r, "DWI", grads=b0, pe=pe)])
        assert fate_of(session, fates, "rev") == expected


def test_dwi_ranking_caps_directions_and_prefers_b1000():
    many = dwi("many", 9, dirs=64)
    thirty = dwi("thirty", 10, dirs=30, acq_rel_s=1500.0, for_hash="F2", tr_ms=8100.0)
    _, decisions, _, _ = decide([dwi_vol(many), dwi_vol(thirty)])
    assert decisions["DWI"]["outcome"] == "review_required"  # directions tie at the cap, nothing else differs
    high = dwi("high", 10, dirs=30, b=2000, acq_rel_s=1500.0, for_hash="F2")
    _, decisions, _, _ = decide([dwi_vol(dwi("low", 9, dirs=30)), dwi_vol(high)])
    assert decisions["DWI"]["decided_by"] == "b_target"


# ---- susceptibility ------------------------------------------------------------------------------------------

def swi_parts(key="swi", number=10, phase=True, **kw):
    fields = dict(tr_ms=40.0, te_ms=20.0, flip_deg=15.0, orientation="AX", n_positions=60, pos_min=0.0, pos_max=106.2,
                  step_mm=1.8, thick_mm=1.8, extent_mm=108.0, rows=256, cols=256, pixel_mm=[0.9, 0.9],
                  acq_rel_s=2000.0)
    fields.update(kw)
    mag = rec(key, number, **fields)
    vols = [vol(mag, "SWI")]
    if phase:
        pha = dict(copy.deepcopy(mag), series_key=key + "p", series_number=number + 1, parts={"phase": 60})
        vols.append(vol(pha, "SWI_PHASE", image_type=["ORIGINAL", "PRIMARY", "P", "ND"]))
    return mag, vols


def test_swi_bundle_files_each_part_in_its_folder():
    mag, vols = swi_parts()
    processed = dict(copy.deepcopy(mag), series_key="proc", series_number=12, image_type=["DERIVED", "PRIMARY", "SWI"])
    minip = dict(copy.deepcopy(mag), series_key="mip", series_number=13, image_type=["DERIVED", "PRIMARY", "MIN IP"],
                 n_positions=14, pos_min=4.0, pos_max=100.0, thick_mm=8.0, step_mm=7.4)
    session, decisions, fates, _ = decide(vols + [vol(processed, "SWI"), vol(minip, "SWI_MINIP")])
    assert decisions["SWI"]["outcome"] == "selected"
    assert fate_of(session, fates, "swi") == ({"selected"}, {"SWI"})
    assert fate_of(session, fates, "swip") == ({"selected"}, {"SWI_PHASE"})
    assert fate_of(session, fates, "proc") == ({"selected_derived"}, {"SWI"})
    assert decisions["SWI_MINIP"]["outcome"] == "selected" and fate_of(session, fates, "mip")[1] == {"SWI_MINIP"}


def test_swi_ranking_phase_then_contrast_and_echo_cap():
    _, with_phase = swi_parts("a", 10)
    _, without = swi_parts("b", 20, phase=False, acq_rel_s=2500.0, for_hash="F2")
    _, decisions, _, _ = decide(with_phase + without)
    assert decisions["SWI"]["decided_by"] == "has_phase" and decisions["SWI"]["winner"].has_phase
    _, pre = swi_parts("pre", 10)
    _, post = swi_parts("post", 20, acq_rel_s=2500.0, for_hash="F2", contrast_agent=True)
    session, decisions, _, _ = decide(pre + post + [vol(ge_t1(), "T1")])
    assert decisions["SWI"]["decided_by"] == "contrast"
    assert decisions["SWI"]["winner"].primary.series["series_key"] == "pre"


def test_orphan_phase_needs_review():
    mag, _ = swi_parts(phase=False)
    loose = dict(copy.deepcopy(mag), series_key="loose", series_number=30, acq_rel_s=2600.0, parts={"phase": 60})
    _, decisions, _, _ = decide([vol(mag, "SWI"), vol(loose, "SWI_PHASE", image_type=["ORIGINAL", "PRIMARY", "P"])])
    assert decisions["SWI"]["outcome"] == "review_required" and decisions["SWI"]["reasons"] == ["orphan_phase"]


def test_qsm_maps_follow_their_source():
    source = rec("qsrc", 40, tr_ms=40.0, flip_deg=15.0, orientation="AX", n_positions=60, pos_min=0.0, pos_max=59.0,
                 acq_rel_s=3000.0, echoes=[{"te_ms": t, "echo_number": i + 1, "n_instances": 60}
                                           for i, t in enumerate((4.0, 8.0, 12.0))])
    qmap = dict(copy.deepcopy(source), series_key="qmap", series_number=41, echoes=[], te_ms=None,
                image_type=["DERIVED", "PRIMARY", "QSM"])
    session, decisions, fates, _ = decide([vol(source, "QSM", te=t) for t in (4.0, 8.0, 12.0)] + [vol(qmap, "QSM")])
    assert decisions["QSM"]["outcome"] == "selected" and fate_of(session, fates, "qmap") == ({"selected_derived"}, {"QSM"})
    mag, vols = swi_parts()
    from_swi = dict(copy.deepcopy(mag), series_key="qmap", series_number=41, image_type=["DERIVED", "PRIMARY", "QSM"])
    session, decisions, fates, _ = decide(vols + [vol(from_swi, "QSM")])
    assert (decisions["QSM"]["outcome"], decisions["QSM"]["decided_by"]) == ("selected", "follows_source")
    alone = dict(copy.deepcopy(from_swi), for_hash="F9")
    _, decisions, _, _ = decide([vol(alone, "QSM")])
    assert decisions["QSM"]["outcome"] == "review_required" and decisions["QSM"]["reasons"] == ["derived_unlinked"]


def test_gre_prefers_thin_sections():
    thick = rec("g2d", 50, mr_acq="2D", tr_ms=600.0, te_ms=20.0, flip_deg=20.0, orientation="AX", thick_mm=5.0,
                step_mm=6.0, n_positions=24)
    thin = rec("g3d", 51, mr_acq="3D", tr_ms=30.0, te_ms=20.0, flip_deg=15.0, orientation="AX", thick_mm=2.0,
               step_mm=2.0, n_positions=72, acq_rel_s=500.0, for_hash="F2")
    _, decisions, _, _ = decide([vol(thick, "GRE"), vol(thin, "GRE")])
    assert decisions["GRE"]["decided_by"] == "tier" and decisions["GRE"]["tier"][1]["name"] == "thin"


def test_kept_roles_and_localizers():
    tof = rec("tof", 60, tr_ms=22.0, te_ms=3.4, flip_deg=18.0)
    mip = rec("mip", 61, image_type=["DERIVED", "PRIMARY", "MIP"])
    loc = rec("loc", 1, image_type=["ORIGINAL", "PRIMARY", "LOCALIZER"])
    session, decisions, fates, _ = decide([vol(tof, "MRA"), vol(dict(tof, series_key="tof2"), "MRA"), vol(mip, "MRA"),
                                           vol(loc, "LOCALIZER")])
    assert decisions["MRA"]["outcome"] == "kept_all" and decisions["LOCALIZER"]["outcome"] == "kept_all"
    assert {fates[v]["folder"] for v in session.volumes if v.role == "MRA"} == {"MRA"}


def test_unclassified_volumes_go_to_other_unclassified():
    odd = rec("odd", 70, mr_acq="2D", scan_seq=["GR"], tr_ms=100.0, te_ms=5.0)
    session, _, fates, _ = decide([vol(odd, "OTHER")])
    assert fates[session.volumes[0]] == {"fate": "unclassified", "folder": "OTHER/UNCLASSIFIED", "candidate_id": ""}


# ---- overrides, profile, determinism -------------------------------------------------------------------------

def test_overrides():
    first, second = ge_t1("a", 3, acq_rel_s=300.0, for_hash="F1"), ge_t1("b", 7, acq_rel_s=900.0, for_hash="F2")
    session, decisions, _, _ = decide([vol(first, "T1"), vol(second, "T1")])
    a, b = (by_series(session, k)[0].acq.id for k in ("a", "b"))
    ov = {"T1": {"choose": a, "none": False, "exclude": set()}}
    _, decisions, _, _ = decide([vol(first, "T1"), vol(second, "T1")], overrides=ov)
    assert (decisions["T1"]["outcome"], decisions["T1"]["decided_by"], decisions["T1"]["candidate_id"]) == \
        ("selected", "override", a)
    assert decisions["T1"]["rule_recommendation"] == b and "override_applied" in decisions["T1"]["warnings"]
    _, decisions, fates, _ = decide([vol(first, "T1"), vol(second, "T1")],
                                    overrides={"T1": {"choose": None, "none": False, "exclude": {b}}})
    assert decisions["T1"]["candidate_id"] == a and decisions["T1"]["decided_by"] == "only_eligible"
    _, decisions, _, _ = decide([vol(first, "T1")], overrides={"T1": {"choose": None, "none": True, "exclude": set()}})
    assert (decisions["T1"]["outcome"], decisions["T1"]["decided_by"]) == ("no_eligible_candidate", "override")
    _, decisions, _, _ = decide([vol(first, "T1")], overrides={"T1": {"choose": "0" * 12, "none": False, "exclude": set()}})
    assert decisions["T1"]["reasons"] == ["override_stale"]
    spec = dwi(dirs=3)
    adc = dict(copy.deepcopy(spec[0]), series_key="adc", series_number=901, for_hash="F7",
               image_type=["DERIVED", "PRIMARY", "ADC"])
    session, decisions, _, _ = decide([dwi_vol(spec), vol(adc, "ADC")])
    assert decisions["ADC"]["reasons"] == ["derived_unlinked"]
    group = session.groups[0].id
    _, decisions, fates, _ = decide([dwi_vol(spec), vol(adc, "ADC")],
                                    overrides={"ADC": {"choose": group, "none": False, "exclude": set()}})
    assert decisions["ADC"]["outcome"] == "selected"


@pytest.mark.parametrize("data, message", [
    ({"colour": 1}, "Unknown profile key"),
    ({"name": "bad name"}, "name must match"),
    ({"roles": {"T1": {"tiers": [{"name": "x", "families": ["FLAIR_3D"]}]}}}, "families must be"),
    ({"roles": {"NOPE": {}}}, "Unknown profile role"),
    ({"roles": {"T1": {"tiers": []}}}, "tiers must be a nonempty list"),
    ({"roles": {"MRA": {"reduce": True}}}, "no selection rules yet"),
    ({"roles": {"ADC": {"contrast": "pre"}}}, "Unknown key|takes no settings"),
])
def test_profile_validation(data, message):
    with pytest.raises(ValueError, match=message):
        sr.load_profile(data)


def test_profile_hash_changes_with_3d_only():
    assert sr.profile_sha256(sr.load_profile()) != sr.profile_sha256(THREE_D)


def test_decisions_do_not_depend_on_input_order():
    def summary():
        vols = census_ge_session() + [dwi_vol(dwi())] + swi_parts()[1]
        random.Random(len(vols) * 7).shuffle(vols)
        session, decisions, fates, _ = decide(vols)
        return ({r: (d["outcome"], d["candidate_id"]) for r, d in decisions.items()},
                sorted((v.relpath, fates[v]["fate"]) for v in session.volumes))
    assert summary() == summary()
    baseline = decide(census_ge_session() + [dwi_vol(dwi())] + swi_parts()[1])
    assert summary()[0] == {r: (d["outcome"], d["candidate_id"]) for r, d in baseline[1].items()}


def test_xa_image_type_text_tokens_count_for_the_variant():
    """Siemens XA: DIS2D is only in ImageTypeText (series record), not in the sidecar ImageType."""
    base = dict(manufacturer="SIEMENS", acq_rel_s=300.0)
    xa = ge_t1("xa", 3, image_type=["ORIGINAL", "PRIMARY", "M", "DIS2D"], **base)
    nd = ge_t1("nd", 4, image_type=["ORIGINAL", "PRIMARY", "M", "ND"], for_hash="F2", **base)
    _, decisions, _, _ = decide([vol(xa, "T1", image_type=["ORIGINAL", "PRIMARY", "M"]), vol(nd, "T1")])
    assert decisions["T1"]["decided_by"] == "variant"
    assert decisions["T1"]["winner"].primary.series["series_key"] == "xa"


def test_resolution_requirements_and_unknown_native():
    iso = sr.load_profile(options={"require": {"isotropic": True}})
    _, decisions, _, _ = decide([vol(ge_flair(thick_mm=1.39, step_mm=0.7, rows=256, cols=256, pixel_mm=[1.0, 1.0],
                                              acq_matrix=[0, 250, 250, 0]), "FLAIR")], profile=iso)
    assert decisions["FLAIR"]["reasons"] == ["not_isotropic(1)"]  # 1.02 x 1.02 x 1.39
    siemens = ge_flair(rows=256, cols=224, pixel_mm=[1.0, 1.0], acq_matrix=[256, 0, 0, 202], thick_mm=1.05)
    _, decisions, _, _ = decide([vol(siemens, "FLAIR")], profile=iso)
    assert decisions["FLAIR"]["outcome"] == "selected"  # 1.11 x 1.0 x 1.05 is within 20 %
    _, decisions, _, _ = decide([vol(dict(siemens, phase_dir=None, acq_matrix=[256, 0, 0, 224]), "FLAIR")], profile=iso)
    assert decisions["FLAIR"]["outcome"] == "review_required"  # isotropic under one phase direction only
    capped = sr.load_profile(options={"require": {"max_voxel_mm": 1.05}})
    _, decisions, _, _ = decide([vol(siemens, "FLAIR")], profile=capped)
    assert decisions["FLAIR"]["reasons"] == ["voxel_too_large(1)"]
    _, decisions, _, _ = decide([vol(ge_t1(acq_matrix=None), "T1")], profile=capped)
    assert decisions["T1"]["outcome"] == "review_required" and decisions["T1"]["reasons"] == ["native_unknown(1)"]


def test_requirements_apply_only_where_they_make_sense():
    profile = sr.load_profile(options={"require": {"mr_acq": "3D", "min_directions": 30, "no_fallback": True}})
    assert profile["roles"]["DWI"]["require"] == {"min_directions": 30, "no_fallback": True}
    assert profile["roles"]["T1"]["require"] == {"mr_acq": "3D", "no_fallback": True}
    _, decisions, _, _ = decide([dwi_vol(dwi(dirs=12))], profile=profile)
    assert decisions["DWI"]["reasons"] == ["too_few_directions(1)"]
    _, decisions, _, _ = decide([vol(ge_t2(), "T2")], profile=sr.load_profile(options={"require": {"no_fallback": True}}))
    assert decisions["T2"]["reasons"] == ["fallback_only(1)"]


def test_session_requirements():
    gauss = [ge_t1(field_t=30000)]
    profile = sr.load_profile(options={"field_strength": [3]})
    assert sr.session_requirement(gauss, profile) is None
    assert sr.session_requirement([ge_t1(field_t=2.89)], profile) is None
    assert sr.session_requirement([ge_t1(field_t=1.494)], profile) == ("session_excluded", "field_strength")
    assert sr.session_requirement([ge_t1(field_t=None)], profile) == ("review_required", "field_strength_unknown")
    makers = sr.load_profile(options={"manufacturer": ["siemens"]})
    assert makers["manufacturer"] == ["SIEMENS"]
    assert sr.session_requirement([ge_t1()], makers) == ("session_excluded", "manufacturer")
    assert sr.session_requirement([ge_t1(manufacturer="")], makers) == ("review_required", "manufacturer_unknown")


@pytest.mark.parametrize("data, message", [
    ({"modalities": ["T1", "BOLD"]}, "modalities must list"),
    ({"complete_only": True}, "complete_only needs"),
    ({"field_strength": ["3T"]}, "field_strength must"),
    ({"roles": {"T1": {"require": {"min_directions": 30}}}}, "min_directions applies to DWI"),
    ({"roles": {"T1": {"require": {"colour": 1}}}}, "require takes only"),
    ({"roles": {"MRA": {"require": {"isotropic": True}}}}, "takes no requirements"),
])
def test_criteria_validation(data, message):
    with pytest.raises(ValueError, match=message):
        sr.load_profile(data)
