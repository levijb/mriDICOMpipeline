"""BIDS export plan: names, statuses, field maps, omission rules and dataset files (design: BIDS output, 5.4)."""
import copy
import gzip
import json
import logging
import struct

import pytest

from mri_dicom_pipeline import bids, nifti, selection, selection_metadata
from mri_dicom_pipeline import selection_identity as si
from mri_dicom_pipeline import selection_rules as sr
from test_selection_rules import decide, dwi, ge_flair, ge_t1, ge_t2, rec, swi_parts, vol

GRID = ([10, 10, 10], [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0]])
LOG = logging.getLogger("test_bids")


def gradient_companions(folder="DWI", token="x"):
    return [{"name": f"{folder}/S001_20260725_{folder}_{token}.{kind}", "kind": kind, "sha256": "0" * 64}
            for kind in ("bval", "bvec")]


def dwi_volume(spec, readout=0.05, companions=None, **sidecar):
    r, grads, pe = spec
    v = vol(r, "DWI", grads=grads, pe=pe, companions=gradient_companions() if companions is None else companions)
    if readout is not None:
        v.sidecar["TotalReadoutTime"] = readout
    v.sidecar.update(sidecar)
    return v


def reverse_b0(main, pe="j", readout=0.05, key="rev", number=10):
    r = dict(copy.deepcopy(main[0]), series_key=key, series_number=number, acq_rel_s=1500.0)
    v = vol(r, "DWI", grads=si.gradient_stats([0, 0], [[0, 0]] * 3, 2), pe=pe, companions=gradient_companions(
        token=key))
    if readout is not None:
        v.sidecar["TotalReadoutTime"] = readout
    return v


def plan(volumes, profile=None, agent="unknown", grid=lambda v: GRID, subject="S001", overrides=None):
    profile = profile or sr.load_profile()
    session, decisions, fates, _ = decide(volumes, profile, overrides)
    chosen = [v for v in session.volumes if selection.copied(v, fates[v], profile)]
    entries = bids.plan_visit(subject, "20260725", chosen, fates, decisions, agent, grid)
    return entries, decisions


def by_key(entries):
    """{series key: [(path, status, reason), ...]}"""
    found = {}
    for e in entries:
        found.setdefault(e["volume"].series["series_key"], []).append((e["path"], e["status"], e["reason"]))
    return found


def paths(entries):
    return sorted(e["path"] for e in entries if e["status"] != "omitted")


P = "sub-S001_ses-20260725"


def test_structural_and_diffusion_names():
    post = ge_t1("post", 13, contrast_agent=True, acq_rel_s=2400.0, for_hash="F1")
    adc = dict(copy.deepcopy(dwi()[0]), series_key="adc", series_number=901,
               image_type=["DERIVED", "PRIMARY", "DIFFUSION", "ADC"])
    entries, _ = plan([vol(ge_t1(), "T1"), vol(post, "T1_POST"), vol(ge_flair(), "FLAIR"), vol(ge_t2(), "T2"),
                       dwi_volume(dwi()), vol(adc, "ADC")], agent="gadolinium")
    assert paths(entries) == [f"anat/{P}_FLAIR.nii.gz", f"anat/{P}_T1w.nii.gz", f"anat/{P}_T2w.nii.gz",
                              f"anat/{P}_ce-gadolinium_T1w.nii.gz", f"dwi/{P}_ADC.nii.gz", f"dwi/{P}_dwi.nii.gz"]
    extra = {e["path"]: e["extra"] for e in entries}
    assert extra[f"anat/{P}_ce-gadolinium_T1w.nii.gz"] == {"ContrastBolusIngredient": "GADOLINIUM"}
    assert {e["status"] for e in entries} == {"standard"}


def test_contrast_agent_outside_the_bids_list_has_no_ingredient():
    post = ge_t1("post", 13, contrast_agent=True, acq_rel_s=2400.0)
    entries, _ = plan([vol(ge_t1(), "T1"), vol(post, "T1_POST")], agent="ferumoxytol")
    post_entry = next(e for e in entries if "_ce-" in e["path"])
    assert post_entry["path"] == f"anat/{P}_ce-ferumoxytol_T1w.nii.gz" and post_entry["extra"] == {}


def test_swi_bundle_uses_the_conventional_names():
    mag, vols = swi_parts()
    processed = dict(copy.deepcopy(mag), series_key="proc", series_number=12, image_type=["DERIVED", "PRIMARY", "SWI"])
    minip = dict(copy.deepcopy(mag), series_key="mip", series_number=13, image_type=["DERIVED", "PRIMARY", "MIN IP"],
                 n_positions=14, pos_min=4.0, pos_max=100.0, thick_mm=8.0, step_mm=7.4)
    entries, _ = plan(vols + [vol(processed, "SWI"), vol(minip, "SWI_MINIP")])
    found = by_key(entries)
    assert found["swi"] == [(f"anat/{P}_acq-swi_part-mag_T2starw.nii.gz", "standard", "")]
    assert found["swip"] == [(f"anat/{P}_acq-swi_part-phase_T2starw.nii.gz", "standard", "")]
    assert found["proc"] == [(f"anat/{P}_acq-swi_swi.nii.gz", "conventional", "")]
    assert found["mip"] == [(f"anat/{P}_acq-swi_minIP.nii.gz", "conventional", "")]
    phase = next(e for e in entries if "part-phase" in e["path"])
    assert phase["extra"] == {"Units": "arbitrary"}


def test_qsm_source_echoes_and_the_conventional_map():
    source = rec("qsrc", 40, tr_ms=40.0, flip_deg=15.0, orientation="AX", n_positions=60, pos_min=0.0, pos_max=59.0,
                 acq_rel_s=3000.0, echoes=[{"te_ms": t, "echo_number": i + 1, "n_instances": 60}
                                           for i, t in enumerate((4.0, 8.0, 12.0))])
    qmap = dict(copy.deepcopy(source), series_key="qmap", series_number=41, echoes=[], te_ms=None,
                image_type=["DERIVED", "PRIMARY", "QSM"])
    echoes = [vol(source, "QSM", te=t) for t in (12.0, 4.0, 8.0)]
    entries, _ = plan(echoes + [vol(qmap, "QSM")])
    assert paths(entries) == [f"anat/{P}_acq-qsm_QSM.nii.gz"] + [
        f"anat/{P}_acq-qsm_echo-{n}_MEGRE.nii.gz" for n in (1, 2, 3)]
    echo_of = {e["volume"].te: e["entities"]["echo"] for e in entries if e["suffix"] == "MEGRE"}
    assert echo_of == {4.0: 1, 8.0: 2, 12.0: 3}  # TE order: no EchoNumber in these sidecars
    assert next(e for e in entries if e["suffix"] == "QSM")["status"] == "conventional"


def test_echo_numbers_follow_echonumber_and_parts_share_them():
    r = rec("gre", 50, mr_acq="3D", tr_ms=30.0, flip_deg=15.0, orientation="AX", thick_mm=2.0, step_mm=2.0,
            n_positions=72, parts={"mag": 72, "phase": 72},
            echoes=[{"te_ms": t, "echo_number": i + 1, "n_instances": 72} for i, t in enumerate((5.0, 10.0))])
    volumes = []
    for te, number in ((5.0, 2), (10.0, 1)):  # deliberately reversed against TE order
        for part, image_type in (("mag", ["ORIGINAL", "PRIMARY", "M"]), ("phase", ["ORIGINAL", "PRIMARY", "P"])):
            v = vol(r, "GRE", te=te, image_type=image_type)
            v.sidecar["EchoNumber"] = number
            volumes.append(v)
    entries, _ = plan(volumes)
    assert paths(entries) == sorted(f"anat/{P}_echo-{n}_part-{p}_MEGRE.nii.gz" for n in (1, 2) for p in ("mag", "phase"))
    assert {(e["volume"].te, e["volume"].part): e["entities"]["echo"] for e in entries} == {
        (5.0, "mag"): 2, (5.0, "phase"): 2, (10.0, "mag"): 1, (10.0, "phase"): 1}


def test_a_multi_echo_set_without_echo_times_is_omitted_whole():
    r = rec("gre", 50, mr_acq="3D", tr_ms=30.0, flip_deg=15.0, orientation="AX", thick_mm=2.0, step_mm=2.0,
            n_positions=72, echoes=[{"te_ms": t, "echo_number": i + 1, "n_instances": 72}
                                    for i, t in enumerate((5.0, 10.0))])
    volumes = [vol(r, "GRE", te=te) for te in (5.0, 10.0)]
    del volumes[1].sidecar["EchoTime"]
    # A ranked role reviews such a set; a kept one (reduce: false) passes it on unchecked.
    entries, decisions = plan(volumes, profile=sr.load_profile({"roles": {"GRE": {"reduce": False}}}))
    assert decisions["GRE"]["outcome"] == "kept_all"
    assert {(e["status"], e["reason"]) for e in entries} == {("omitted", "missing_echo_time")}


def choose(role, candidate):
    return {role: {"choose": candidate, "none": False, "exclude": set()}}


def test_a_phase_only_acquisition_is_labelled_phase():
    gre = rec("pha", 50, mr_acq="3D", tr_ms=30.0, te_ms=20.0, flip_deg=15.0, orientation="AX", parts={"phase": 60})
    entries, decisions = plan([vol(gre, "GRE", image_type=["ORIGINAL", "PRIMARY", "P"])],
                              profile=sr.load_profile({"roles": {"GRE": {"reduce": False}}}))
    assert decisions["GRE"]["outcome"] == "kept_all"
    assert [(e["path"], e["extra"]) for e in entries] == [(f"anat/{P}_part-phase_T2starw.nii.gz",
                                                          {"Units": "arbitrary"})]
    # A structural phase-only acquisition is never eligible, but an override can still choose it.
    t1 = lambda: vol(ge_t1(parts={"phase": 60}), "T1", image_type=["ORIGINAL", "PRIMARY", "P"])
    session, *_ = decide([t1()])
    entries, _ = plan([t1()], overrides=choose("T1", session.acquisitions[0].id))
    assert [(e["path"], e["extra"]) for e in entries] == [(f"anat/{P}_part-phase_T1w.nii.gz", {"Units": "arbitrary"})]


def test_a_map_without_its_source_in_the_export_gets_its_own_acq_label():
    adc = dict(copy.deepcopy(dwi()[0]), series_key="adc", series_number=901, for_hash="F7",
               image_type=["DERIVED", "PRIMARY", "ADC"])
    volumes = lambda: [dwi_volume(dwi()), vol(adc, "ADC")]
    session, decisions, _, _ = decide(volumes())
    assert decisions["ADC"]["reasons"] == ["derived_unlinked"]
    entries, _ = plan(volumes(), overrides=choose("ADC", session.groups[0].id))
    assert paths(entries) == [f"dwi/{P}_acq-unlinked_ADC.nii.gz", f"dwi/{P}_dwi.nii.gz"]


def test_a_map_of_an_omitted_scan_is_unlinked_and_numbered_with_the_others():
    profile = sr.load_profile({"roles": {"DWI": {"reduce": False}}})
    a, b = dwi("a", 9), dwi("b", 20, acq_rel_s=1800.0, rows=128, cols=128, pixel_mm=[1.875, 1.875])
    adc = dict(copy.deepcopy(a[0]), series_key="adca", series_number=901, image_type=["DERIVED", "PRIMARY", "ADC"])
    loose = dict(adc, series_key="adcg", series_number=902, for_hash="F7")
    volumes = lambda: [dwi_volume(a, companions=[]), dwi_volume(b), vol(adc, "ADC"), vol(loose, "ADC")]
    session, *_ = decide(volumes(), profile)
    links = {g.series["series_key"]: (g.link_state, g.parent and g.parent.primary.series["series_key"])
             for g in session.groups}
    assert links == {"adca": ("linked", "a"), "adcg": ("unlinked", None)}
    entries, _ = plan(volumes(), profile=profile)
    # a is omitted, so its ADC must not share b's entities; the two unlinked ADCs then need runs.
    assert by_key(entries) == {
        "a": [("", "omitted", "missing_gradients")], "b": [(f"dwi/{P}_dwi.nii.gz", "standard", "")],
        "adca": [(f"dwi/{P}_acq-unlinked_run-1_ADC.nii.gz", "standard", "")],
        "adcg": [(f"dwi/{P}_acq-unlinked_run-2_ADC.nii.gz", "standard", "")]}


def test_structural_secondary_echo_and_localizer_are_omitted():
    # Two T2-family echoes: a PD first echo would send the acquisition to review instead (the owner's HC-08
    # ruling, round 4 finding R4-F-016; test updated with the owner's approval 2026-11-25).
    t2 = ge_t2(echoes=[{"te_ms": t, "echo_number": i + 1, "n_instances": 36} for i, t in enumerate((80.0, 101.8))],
               te_ms=None)
    loc = rec("loc", 1, image_type=["ORIGINAL", "PRIMARY", "LOCALIZER"])
    entries, _ = plan([vol(t2, "T2", te=80.0), vol(t2, "T2", te=101.8), vol(loc, "LOCALIZER")])
    status = {(e["volume"].role, e["volume"].te): (e["status"], e["reason"]) for e in entries}
    assert status[("T2", 80.0)] == ("standard", "")
    assert status[("T2", 101.8)] == ("omitted", "secondary_echo")
    assert status[("LOCALIZER", None)] == ("omitted", "role_not_exported")


def test_kept_angiography_runs_only_where_names_repeat():
    tof = rec("tof", 60, tr_ms=22.0, te_ms=3.4, flip_deg=18.0)
    mip = rec("mip", 62, image_type=["DERIVED", "PRIMARY", "MIP"])
    mrv = rec("mrv", 70, tr_ms=22.0, te_ms=3.4, flip_deg=18.0)
    entries, _ = plan([vol(tof, "MRA"), vol(dict(tof, series_key="tof2", series_number=61), "MRA"), vol(mip, "MRA"),
                       vol(mrv, "MRV")])
    assert paths(entries) == [f"anat/{P}_acq-mrv_angio.nii.gz", f"anat/{P}_rec-mip_angio.nii.gz",
                              f"anat/{P}_run-1_angio.nii.gz", f"anat/{P}_run-2_angio.nii.gz"]
    assert by_key(entries)["tof"][0][0] == f"anat/{P}_run-1_angio.nii.gz"  # series order


def test_kept_acquisitions_share_one_run_across_their_parts():
    profile = sr.load_profile({"roles": {"SWI": {"reduce": False}}})
    _, first = swi_parts("a", 10)
    _, second = swi_parts("b", 20, acq_rel_s=2500.0, for_hash="F2")
    entries, _ = plan(first + second, profile=profile)
    assert paths(entries) == sorted(f"anat/{P}_acq-swi_run-{n}_part-{p}_T2starw.nii.gz"
                                    for n in (1, 2) for p in ("mag", "phase"))
    runs = {e["volume"].series["series_key"]: e["entities"]["run"] for e in entries}
    assert runs == {"a": 1, "ap": 1, "b": 2, "bp": 2}


def test_two_outputs_of_one_kept_series_refuse_the_visit():
    tof = rec("tof", 60, tr_ms=22.0, te_ms=3.4, flip_deg=18.0)
    with pytest.raises(bids.PlanError, match="share the name"):
        plan([vol(tof, "MRA", token="a" * 16), vol(tof, "MRA", token="b" * 16)])


def test_subject_id_must_be_a_bids_label():
    with pytest.raises(bids.PlanError, match="not a BIDS label"):
        plan([vol(ge_t1(), "T1")], subject="S_001")


# ---- reverse phase-encoding field maps (4.3) -------------------------------------------------------------------

def test_reverse_b0_becomes_a_field_map_for_the_dwi():
    main = dwi(pe="j-")
    entries, _ = plan([dwi_volume(main), reverse_b0(main)])
    fmap = next(e for e in entries if e["suffix"] == "epi")
    assert fmap["path"] == f"fmap/{P}_acq-dwi_dir-j_epi.nii.gz" and fmap["status"] == "standard"
    assert fmap["extra"] == {"IntendedFor": [f"bids::sub-S001/ses-20260725/dwi/{P}_dwi.nii.gz"]}


@pytest.mark.parametrize("case, reason", [
    ("partner readout", "missing_readout_metadata"),
    ("dwi readout", "missing_readout_metadata"),
    ("echo spacing only", "missing_readout_metadata"),
    ("shifted grid", "grid_mismatch"),
    ("rotated grid", "grid_mismatch"),
    ("other frame", "grid_mismatch"),
    ("unreadable grid", "grid_mismatch"),
    ("dwi omitted", "target_not_exported"),
])
def test_field_map_checks(case, reason):
    main = dwi(pe="j-")
    target = dwi_volume(main, readout=None if case in ("dwi readout", "echo spacing only") else 0.05,
                        companions=[] if case == "dwi omitted" else None)
    if case == "echo spacing only":
        target.sidecar["EffectiveEchoSpacing"] = 0.0005
    partner = reverse_b0(main, readout=None if case == "partner readout" else 0.05)
    if case == "other frame":
        partner.series = dict(partner.series, for_hash="F9")
    shifted = (GRID[0], [[1.0, 0, 0, 0.6], [0, 1.0, 0, 0], [0, 0, 1.0, 0]])
    rotated = (GRID[0], [[0, 1.0, 0, 0], [1.0, 0, 0, 0], [0, 0, 1.0, 0]])
    other = {"shifted grid": shifted, "rotated grid": rotated, "unreadable grid": None}.get(case, GRID)
    entries, _ = plan([target, partner], grid=lambda v: other if v is partner else GRID)
    fmap = next(e for e in entries if e["volume"] is partner)
    assert (fmap["status"], fmap["reason"]) == ("omitted", reason)
    assert not any(e["extra"].get("IntendedFor") for e in entries)
    if case == "dwi omitted":
        assert next(e for e in entries if e["volume"] is target)["reason"] == "missing_gradients"


def test_same_grid_allows_less_than_half_a_voxel():
    near = (GRID[0], [[1.0, 0, 0, 0.4], [0, 1.0, 0, 0], [0, 0, 1.0, 0]])
    assert bids.same_grid(GRID, near) and not bids.same_grid(GRID, ([10, 10, 9], GRID[1]))


def test_complete_only_counts_exported_selections():
    profile = sr.load_profile(options={"modalities": ["T1", "DWI"], "complete_only": True})
    entries, decisions = plan([vol(ge_t1(), "T1"), dwi_volume(dwi(), companions=[])], profile=profile)
    assert bids.unexported_roles(entries, decisions, profile) == ["DWI"]


# ---- files ------------------------------------------------------------------------------------------------------

def test_sidecar_keeps_technical_fields_only():
    raw = json.dumps({"EchoTime": 0.03, "subject_id": "S001", "session_date": "2026-07-25", "dicom_study_date": "x",
                      "selection_role": "T1", "source_series_key": "k", "PatientName": "x"})
    assert bids.sidecar(raw, {"Units": "arbitrary"}) == {"EchoTime": 0.03, "Units": "arbitrary"}
    assert not {"subject_id", "session", "session_date", "dicom_study_date", "scan_type", "source_archive_sha256",
                "source_series_key", "PatientName", "AcquisitionTime"} & nifti.TECH_FIELDS


def write_header(path, nifti2=False, sform=None, quatern=None, pixdim=(1, 2.0, 2.0, 3.0)):
    if nifti2:
        h = bytearray(544)
        struct.pack_into("<i", h, 0, 540)
        struct.pack_into("<8q", h, 16, 3, 4, 5, 6, 1, 1, 1, 1)
        struct.pack_into("<8d", h, 104, *pixdim, 1, 1, 1, 1)
        layout = ("<2i", 344, "<6d", 352, "<12d", 400)
    else:
        h = bytearray(352)
        struct.pack_into("<i", h, 0, 348)
        struct.pack_into("<8h", h, 40, 3, 4, 5, 6, 1, 1, 1, 1)
        struct.pack_into("<8f", h, 76, *pixdim, 1, 1, 1, 1)
        layout = ("<2h", 252, "<6f", 256, "<12f", 280)
    codes, at_codes, qfmt, at_q, sfmt, at_s = layout
    struct.pack_into(codes, h, at_codes, 1 if quatern else 0, 1 if sform else 0)
    if quatern:
        struct.pack_into(qfmt, h, at_q, *quatern)
    if sform:
        struct.pack_into(sfmt, h, at_s, *sform)
    with gzip.open(path, "wb") as f:
        f.write(bytes(h))
    return path


@pytest.mark.parametrize("nifti2", [False, True])
def test_nifti_grid_reads_sform_then_qform_then_voxel_sizes(tmp_path, nifti2):
    sform = (0, 0, 2.0, 10, -2.0, 0, 0, 20, 0, 3.0, 0, 30)
    dims, rows = selection_metadata.nifti_grid(write_header(tmp_path / "s.nii.gz", nifti2, sform=sform))
    assert dims == [4, 5, 6] and rows == [list(sform[0:4]), list(sform[4:8]), list(sform[8:12])]
    # b = c = 0, d = 1: 180 degrees about z, so x and y flip.
    dims, rows = selection_metadata.nifti_grid(write_header(tmp_path / "q.nii.gz", nifti2, quatern=(0, 0, 1, 5, 6, 7)))
    assert rows == [[-2.0, 0.0, 0.0, 5.0], [0.0, -2.0, 0.0, 6.0], [0.0, 0.0, 3.0, 7.0]]
    _, rows = selection_metadata.nifti_grid(write_header(tmp_path / "p.nii.gz", nifti2))
    assert rows == [[2.0, 0.0, 0.0, 0.0], [0.0, 2.0, 0.0, 0.0], [0.0, 0.0, 3.0, 0.0]]


def test_merge_tsv_keeps_user_rows_columns_and_values(tmp_path):
    path = tmp_path / "participants.tsv"
    path.write_text("participant_id\tage\nsub-A\t40\nsub-X\t33\n", encoding="utf-8")
    bids.merge_tsv(path, "participant_id", {"sub-A": {}, "sub-B": {}}, LOG)
    assert path.read_text(encoding="utf-8") == "participant_id\tage\nsub-A\t40\nsub-X\t33\nsub-B\tn/a\n"
    sessions = tmp_path / "sessions.tsv"
    bids.merge_tsv(sessions, "session_id", {"ses-1": {"source_sessions": "1"}}, LOG)
    sessions.write_text(sessions.read_text(encoding="utf-8").replace("source_sessions", "source_sessions\tnote")
                        .replace("\t1\n", "\t1\tmine\n"), encoding="utf-8")
    bids.merge_tsv(sessions, "session_id", {"ses-1": {"source_sessions": "1,1_2"}, "ses-2": {"source_sessions": "2"}},
                   LOG)
    assert sessions.read_text(encoding="utf-8") == \
        "session_id\tsource_sessions\tnote\nses-1\t1,1_2\tmine\nses-2\t2\tn/a\n"
    odd = tmp_path / "odd.tsv"
    odd.write_text("age\tparticipant_id\n", encoding="utf-8")
    bids.merge_tsv(odd, "participant_id", {"sub-A": {}}, LOG)
    assert odd.read_text(encoding="utf-8") == "age\tparticipant_id\n"


def test_bidsignore_block_is_replaced_and_user_lines_stay(tmp_path):
    path = tmp_path / ".bidsignore"
    path.write_text("mine/\n", encoding="utf-8")
    bids._bidsignore(path)
    bids._bidsignore(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == [bids.IGNORE_START, *bids.IGNORED, bids.IGNORE_END, "mine/"]
