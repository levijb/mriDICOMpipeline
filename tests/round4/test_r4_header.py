"""TG-HDR: reading DICOM headers for selection (plan section 5, TG-HDR; R-HDR-1 to R-HDR-5).

Each catalog exam is prepared and converted with the real dcm2niix and selected report-only. The
series records `select` cached (Reports/.selection_cache/<subject>/<session>.json, scan-selection.md
7.3) are compared field by field with the independent header reader (oracles/header_reader.py,
frozen at G1) under plan 3.4, using the reader's own comparator. Evidence: facts_synthetic.csv in
R4_EVIDENCE_DIR, one row per series and field with both values and the result.
"""

from __future__ import annotations

import csv
import json
import os

import pytest

from oracles import gradients as grad_oracle
from r4lib import catalog, e2e

FACT_FAMILIES = ("structural", "diffusion", "susceptibility", "hard_cases", "geometry", "kept_roles", "visits")


def _exams(families=FACT_FAMILIES):
    out = []
    for name, data in catalog.load_all(families).items():
        for exam in data["exams"]:
            out.append((name, exam["id"]))
    return out


def _session_dirs(conv):
    base = os.path.join(conv.root, "DicomExtracted", "Subjects")
    for subject in sorted(os.listdir(base)):
        for label in sorted(os.listdir(os.path.join(base, subject))):
            yield subject, label, os.path.join(base, subject, label)


def _cache_doc(conv, subject, label):
    path = os.path.join(conv.root, "Reports", ".selection_cache", subject, f"{label}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def normalized_select(rec):
    """select's record with one reading applied: 5.1 gives `echoes` for multi-echo series only, so an
    empty list and an absent list state the same thing."""
    rec = dict(rec)
    if rec.get("echoes") == []:
        rec["echoes"] = None
    return rec


def compare_exam(conv, header_reader):
    """[(series uid, field, result, reader value, select value)] for every series of the exam."""
    uid_of = {e2e.series_key(conv.exam, s["uid_series"]): s["uid_series"] for s in e2e.all_series(conv.exam)}
    rows = []
    for subject, label, folder in _session_dirs(conv):
        mine = header_reader.read_session(folder)["series"]
        theirs = _cache_doc(conv, subject, label)["series"]
        software_by_key = software_values(folder)
        for key in sorted(set(mine) | set(theirs)):
            uid = uid_of.get(key, key[:8])
            if key not in theirs or key not in mine:
                rows.append((uid, "<series>", "absent", key in mine, key in theirs))
                continue
            for item in header_reader.compare_series(mine[key], normalized_select(theirs[key])):
                if item["field"] == "software":
                    item = software_item(mine[key], theirs[key], software_by_key.get(key))
                rows.append((uid, item["field"], item["result"], item["reader"], item["select"]))
    return rows


def software_values(folder):
    """{series key: SoftwareVersions values in DICOM order} from one object per series (pydicom)."""
    import hashlib

    import pydicom

    out = {}
    for dirpath, _d, files in os.walk(folder):
        for f in sorted(files):
            try:
                ds = pydicom.dcmread(os.path.join(dirpath, f), stop_before_pixels=True,
                                     specific_tags=["SeriesInstanceUID", "SoftwareVersions"])
            except Exception:  # noqa: BLE001 - receipts and inventories are not DICOM
                continue
            uid = str(ds.get("SeriesInstanceUID", ""))
            key = hashlib.sha256(uid.encode("ascii")).hexdigest()[:20]
            if uid and key not in out:
                value = ds.get("SoftwareVersions")
                out[key] = [] if value is None else ([str(v) for v in value] if value.__class__.__name__ ==
                                                     "MultiValue" else [str(value)])
    return out


def software_item(reader_rec, select_rec, values):
    """The owner's reading of `software` (R4-F-010, 2026-11-25): the SoftwareVersions values as one string,
    joined as the cache stores them (each value stripped and upper-cased, joined with a backslash), compared
    exactly. The expected string is built from the DICOM in value order (the reader keeps a sorted set,
    which is checked to hold the same values)."""
    expected = "\\".join(v.strip().upper() for v in (values or []) if v.strip())
    got = select_rec.get("software") or ""
    reader_set = sorted(str(v).strip().upper() for v in (reader_rec.get("software") or []))
    same_values = reader_set == sorted(x for x in expected.split("\\") if x)
    result = "equal" if got == expected and same_values else "mismatch"
    return {"field": "software", "result": result, "reader": expected, "select": got}


def _write_facts(exam_id, rows):
    path = e2e.evidence_dir()
    if not path:
        return
    target = os.path.join(path, "facts_synthetic.csv")
    new = not os.path.exists(target)
    with open(target, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["exam", "series", "field", "select", "reader", "result"])
        for uid, field, result, reader, sel in rows:
            w.writerow([exam_id, uid, field, json.dumps(sel, default=str), json.dumps(reader, default=str), result])


@pytest.mark.r4real
@pytest.mark.parametrize("cat_name,exam_id", _exams())
def test_r4_hdr_01_every_field_against_the_header_reader(real_exam, header_reader, cat_name, exam_id):
    """R4-HDR-01: equal within plan 3.4 tolerances. A field the reader marks unclear is listed in the
    evidence for the owner (oracle rule 6), not counted as a mismatch."""
    conv = real_exam(cat_name, exam_id)
    conv.standard()
    rows = compare_exam(conv, header_reader)
    _write_facts(exam_id, rows)
    bad = [r for r in rows if r[2] in ("mismatch", "absent")]
    assert not bad, json.dumps(bad, indent=1, default=str)


@pytest.mark.r4real
@pytest.mark.parametrize("cat_name,exam_id", [(c, e) for c, e, _ in e2e.exam_cases({"R-HDR-2"})])
def test_r4_hdr_02_missing_fields_stay_missing(real_exam, cat_name, exam_id):
    conv = real_exam(cat_name, exam_id)
    conv.standard()
    want = conv.exam["expected"]["R-HDR-2"]["outcome"]
    problems = []
    for uid, fields in want["null_fields"].items():
        rec = e2e.cache_series(conv, conv.exam, int(uid))
        for field in fields:
            if rec is None or rec.get(field) is not None:
                problems.append(f"{uid}: {field} = {None if rec is None else rec.get(field)!r}, expected null")
        if want.get("image_type_without_text") and rec and rec.get("has_image_type_text"):
            problems.append(f"{uid}: has_image_type_text true")
    assert not problems, problems


@pytest.mark.r4real
@pytest.mark.parametrize("cat_name,exam_id", [(c, e) for c, e, _ in e2e.exam_cases({"R-HDR-3"})])
def test_r4_hdr_03_echoes_parts_and_mixed_series(real_exam, cat_name, exam_id):
    conv = real_exam(cat_name, exam_id)
    conv.standard()
    want = conv.exam["expected"]["R-HDR-3"]["outcome"]
    problems = []

    def rec(uid):
        return e2e.cache_series(conv, conv.exam, int(uid)) or {}

    for uid, value in (want.get("mixed") or {}).items():
        if bool(rec(uid).get("mixed")) is not value:
            problems.append(f"{uid}: mixed {rec(uid).get('mixed')!r}, expected {value}")
    for uid, value in (want.get("n_stacks") or {}).items():
        if rec(uid).get("n_stacks") != value:
            problems.append(f"{uid}: n_stacks {rec(uid).get('n_stacks')!r}, expected {value}")
    if "mixed_record_fields" in want:
        for uid, value in want["mixed"].items():
            if value and set(rec(uid)) != set(want["mixed_record_fields"]):
                problems.append(f"{uid}: mixed record fields {sorted(rec(uid))}")
    for uid in want.get("te_ms_null", []):
        if rec(uid).get("te_ms") is not None:
            problems.append(f"{uid}: te_ms {rec(uid).get('te_ms')!r}, expected null")
    echo_specs = dict(want.get("echoes") or {})
    for uid, spec in want.items():
        if uid.isdigit():
            echo_specs[uid] = spec.get("echoes")
            if "te_ms" in spec and rec(uid).get("te_ms") != spec["te_ms"]:
                problems.append(f"{uid}: te_ms {rec(uid).get('te_ms')!r}, expected {spec['te_ms']!r}")
            if "mixed" in spec and bool(rec(uid).get("mixed")) is not spec["mixed"]:
                problems.append(f"{uid}: mixed {rec(uid).get('mixed')!r}")
            if "parts" in spec and {k: v for k, v in (rec(uid).get("parts") or {}).items() if v} != spec["parts"]:
                problems.append(f"{uid}: parts {rec(uid).get('parts')!r}, expected {spec['parts']}")
    for uid, echoes in echo_specs.items():
        if echoes is None:
            continue
        got = sorted(([e["te_ms"], e["echo_number"], e["n_instances"]] for e in rec(uid).get("echoes") or []))
        if len(got) != len(echoes) or any(abs(g[0] - w[0]) > 0.01 or g[1:] != w[1:] for g, w in zip(got, sorted(echoes))):
            problems.append(f"{uid}: echoes {got}, expected {echoes}")
    assert not problems, problems


@pytest.mark.r4real
def test_r4_hdr_04_cache_holds_no_identifying_value(real_exam):
    conv = real_exam("hard_cases", "HC-10-SENTINELS")
    conv.standard()
    want = conv.exam["expected"]["R-HDR-4"]["outcome"]
    import r4_header_reader  # loaded by the header_reader fixture's loader

    found = {}
    for path in e2e.cache_files(conv):
        text = open(path, encoding="utf-8").read()
        hits = [s for s in want["cache_forbidden_text"] if s in text]
        if hits:
            found[os.path.basename(path)] = hits
    assert not found, found
    if want.get("desc_tokens_within_vocabulary"):
        outside = set()
        for doc in e2e.load_cache(conv).values():
            for rec in doc["series"].values():
                outside |= set(rec.get("desc_tokens") or []) - set(r4_header_reader.VOCABULARY)
        assert not outside, sorted(outside)


@pytest.fixture
def _reader_module(header_reader):
    return header_reader


@pytest.mark.r4real
@pytest.mark.parametrize("cat_name,exam_id", _exams(("structural", "diffusion", "susceptibility", "kept_roles")))
def test_r4_hdr_05_volume_records_against_nibabel(real_exam, _reader_module, cat_name, exam_id):
    """R4-HDR-05: each cached volume's dims equal NiBabel's header, its companions match the
    published files by suffix, and its gradient statistics equal oracles/gradients.py."""
    import nibabel as nib

    conv = real_exam(cat_name, exam_id)
    conv.standard()
    problems = []
    for doc in e2e.load_cache(conv).values():
        for rel, vol in e2e.cache_volumes(doc):
            path = os.path.join(conv.root, "Niftis", *rel.split("/"))
            hdr = nib.load(path).header
            dims = [int(x) for x in hdr["dim"]]
            if [int(x) for x in vol.get("dims", [])] != dims:
                problems.append(f"{rel}: dims {vol.get('dims')} != nibabel {dims}")
            stem = path[:-7]
            folder = os.path.dirname(path)
            on_disk = sorted(n[len(os.path.basename(stem)):] for n in os.listdir(folder)
                             if n.startswith(os.path.basename(stem)) and n != os.path.basename(path)
                             and not n.endswith(".json"))
            listed = sorted(os.path.basename(c["name"])[len(os.path.basename(stem)):] for c in vol.get("companions", []))
            if on_disk != listed:
                problems.append(f"{rel}: companions {listed} != files {on_disk}")
            if os.path.exists(stem + ".bval"):
                want = grad_oracle.stats_from_files(stem + ".bval", stem + ".bvec", dims[4] if dims[0] >= 4 else 1)
                got = vol.get("gradients") or {}
                for key in ("b0", "shells", "b_max", "n_dirs", "mismatch"):
                    if got.get(key) != want.get(key):
                        problems.append(f"{rel}: gradients {key} {got.get(key)!r} != oracle {want.get(key)!r}")
    exp = conv.exam["expected"].get("R-HDR-5")
    if exp:
        run = conv.standard()[0]
        for uid, spec in exp["outcome"].items():
            for r in e2e.rows_for(run, uid):
                vol = e2e.cache_volume(run, r)
                kinds = sorted({c["kind"] for c in vol.get("companions", [])})
                if kinds != sorted(spec["companions"]):
                    problems.append(f"{uid}: companion kinds {kinds}, expected {spec['companions']}")
                if vol["dims"][4] != spec["dims4"]:
                    problems.append(f"{uid}: dims4 {vol['dims'][4]}, expected {spec['dims4']}")
                for key, value in spec["gradients"].items():
                    if (vol.get("gradients") or {}).get(key) != value:
                        problems.append(f"{uid}: gradients {key} {(vol.get('gradients') or {}).get(key)!r}, expected {value!r}")
    assert not problems, problems
