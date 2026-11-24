"""D-13 calibration helpers (R4-OR-05, R4-OR-06): locate the vendor datasets, index their DICOM
series and their reference sidecars, and compare.

Downloaded data is untrusted: it is only read (pydicom, NiBabel, json), never executed, and no
interpreter runs from inside its folders. Reports name series by SeriesNumber and file stem and
hold technical fields only.
"""

from __future__ import annotations

import glob
import json
import os
from collections import defaultdict

import pydicom

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DCM2NIIX_PART_TOKENS = {"MAGNITUDE", "PHASE", "REAL", "IMAGINARY"}
SPEC_PATH = os.path.join(HERE, "expected", "vendor_datasets.json")


def spec():
    with open(SPEC_PATH, encoding="utf-8") as f:
        return json.load(f)


def dataset_root(entry):
    """The extracted <repo>-<commit> folder, or None when the dataset is not present."""
    base = os.environ.get("R4_VENDOR_DIR") or spec()["local_root"]
    name = entry["repo"].split("/")[1]
    path = os.path.join(base, name, "extracted", f"{name}-{entry['commit']}")
    return path if os.path.isdir(path) else None


def _tm(text):
    t = str(text or "").replace(":", "")
    try:
        return int(t[0:2]) * 3600 + int(t[2:4]) * 60 + float(t[4:] or 0)
    except ValueError:
        return None


def dicom_series(root):
    """{SeriesInstanceUID: {"sn", "desc", "acq_s", "paths", "folders"}} for every MR image object
    below In/. Descriptions stay in memory for matching and are never written out."""
    out = {}
    for path in sorted(glob.glob(os.path.join(root, "In", "**", "*"), recursive=True)):
        if not os.path.isfile(path):
            continue
        try:
            ds = pydicom.dcmread(path, stop_before_pixels=True)
        except Exception:  # noqa: BLE001 - non-DICOM files (txt, pdf) are skipped
            continue
        if "Rows" not in ds or ds.get("Modality") != "MR" or ds.get("SeriesNumber") is None:
            continue
        uid = str(ds.SeriesInstanceUID)
        rec = out.setdefault(uid, {"sn": int(ds.SeriesNumber), "desc": str(ds.get("SeriesDescription") or ""),
                                   "acq_s": None, "paths": [], "folders": set()})
        rec["paths"].append(path)
        rec["folders"].add(os.path.dirname(path))
        t = _tm(ds.get("AcquisitionTime"))
        if t is not None and (rec["acq_s"] is None or t < rec["acq_s"]):
            rec["acq_s"] = t
    return out


def match_series(side, series):
    """The SeriesInstanceUID a reference sidecar came from, or (None, reason). Matched on
    SeriesNumber (dcm2niix adds 1000 to a series it splits), then SeriesDescription, then the
    earliest AcquisitionTime within 1 s."""
    sn = side.get("SeriesNumber")
    if sn is None:
        return None, "sidecar without SeriesNumber"
    sn = int(sn)
    base = sn - 1000 if sn >= 1000 else sn
    cands = [u for u, r in series.items() if r["sn"] == base]
    if len(cands) > 1:
        cands = [u for u in cands if series[u]["desc"] == str(side.get("SeriesDescription") or "")] or cands
    if len(cands) > 1 and side.get("AcquisitionTime"):
        t = _tm(side["AcquisitionTime"])
        cands = [u for u in cands if series[u]["acq_s"] is not None and t is not None and abs(series[u]["acq_s"] - t) <= 1.0] or cands
    if len(cands) != 1:
        return None, f"{len(cands)} candidate series"
    return cands[0], ("split by the converter" if sn >= 1000 else "")


def ref_files(root):
    """[(stem, sidecar dict, nifti path or None)] for every reference sidecar."""
    out = []
    for js in sorted(glob.glob(os.path.join(root, "Ref", "*.json"))):
        stem = os.path.splitext(os.path.basename(js))[0]
        with open(js, encoding="utf-8") as f:
            side = json.load(f)
        nii = os.path.join(root, "Ref", stem + ".nii")
        out.append((stem, side, nii if os.path.exists(nii) else None))
    return out


def run(header_reader, pixel_sampler, datasets=None):
    """R4-OR-05 and R4-OR-06 over every present dataset. Returns a coded report: references are
    named '<series number>#<index>', never by file name."""
    report = []
    for entry in datasets or spec()["datasets"]:
        root = dataset_root(entry)
        item = {"repo": entry["repo"], "commit": entry["commit"], "present": root is not None, "reader": [], "sampler": []}
        report.append(item)
        if root is None:
            continue
        series = dicom_series(root)
        folder_records = {}
        records = {}
        for uid, info in series.items():
            key = header_reader.series_key_of(uid)
            for folder in info["folders"]:
                if folder not in folder_records:
                    folder_records[folder] = header_reader.read_session(folder)["series"]
                if key in folder_records[folder]:
                    records[uid] = folder_records[folder][key]
        by_uid = defaultdict(list)
        for index, (stem, side, nii) in enumerate(ref_files(root)):
            code = f"{side.get('SeriesNumber')}#{index}"
            uid, why = match_series(side, series)
            if uid is None or uid not in records:
                item["reader"].append({"ref": code, "stem": stem, "status": "unmatched", "why": why or "no reader record"})
                continue
            by_uid[uid].append((code, side, nii, why, stem))
            results, notes = compare_reader_to_sidecar(records[uid], side)
            item["reader"].append({"ref": code, "stem": stem, "status": "compared", "results": results, "notes": notes})
        for uid, refs in sorted(by_uid.items(), key=lambda kv: series[kv[0]]["sn"]):
            codes = [r[0] for r in refs]
            stems = [r[4] for r in refs]
            if any(r[3] for r in refs):
                item["sampler"].append({"refs": codes, "stems": stems, "status": "not compared", "reason": "the converter split the series into several files"})
                continue
            niis = [r for r in refs if r[2]]
            if len(niis) == 1:
                niftis = [{"path": niis[0][2], "echo_ms": None}]
            else:
                tes = [r[1].get("EchoTime") for r in niis]
                if None in tes or len(set(tes)) != len(tes):
                    item["sampler"].append({"refs": codes, "stems": stems, "status": "not compared", "reason": "several files share an echo time (for example magnitude and phase)"})
                    continue
                niftis = [{"path": r[2], "echo_ms": r[1]["EchoTime"] * 1000.0} for r in niis]
            result = pixel_sampler.sample_series(series[uid]["paths"], niftis, header_reader.series_key_of(uid))
            result.pop("first_mismatches", None) if result.get("status") in ("matched", "under-sampled") else None
            item["sampler"].append({"refs": codes, "stems": stems, **result})
    return report


def accepted_reader_differences():
    """{(repo, reference stem, field)} the owner accepted (vendor_datasets.json)."""
    return {(a["repo"], a["reference"], a["field"]) for a in spec()["accepted_discrepancies"] if a["test"] == "R4-OR-05"}


def reader_mismatches(report, accepted=None):
    accepted = accepted_reader_differences() if accepted is None else accepted
    out = []
    for item in report:
        for ref in item["reader"]:
            if ref["status"] != "compared":
                out.append((item["repo"], ref["stem"], "unmatched", ref.get("why")))
                continue
            for field, result in ref["results"].items():
                if result == "mismatch" and (item["repo"], ref["stem"], field) not in accepted:
                    out.append((item["repo"], ref["stem"], field, result))
    return out


def sampler_failures(report):
    """Unexplained sampler results, and named expectations that did not hold."""
    expected = {(e["repo"], e["reference"]): e["status"] for e in spec()["expected_sampler_statuses"]}
    out = []
    for item in report:
        for s in item["sampler"]:
            want = [expected[(item["repo"], st)] for st in s.get("stems", []) if (item["repo"], st) in expected]
            if want:
                if s["status"] != want[0]:
                    out.append((item["repo"], s.get("stems"), s["status"], f"expected {want[0]}"))
            elif s["status"] in ("mismatched", "volume order differs"):
                out.append((item["repo"], s.get("stems"), s["status"], "unexplained"))
    found = {(item["repo"], st) for item in report for s in item["sampler"] for st in s.get("stems", [])}
    for key in expected:
        if any(item["repo"] == key[0] and item["present"] for item in report) and key not in found:
            out.append((key[0], [key[1]], "absent", "expected status not reached"))
    return out


def _close(a, b, tol):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


def compare_reader_to_sidecar(rec, side):
    """R4-OR-05 field comparison: reader record against one reference sidecar.

    Returns (results, notes): results maps field -> 'equal' | 'mismatch' | 'not_in_reference' |
    'not_comparable'. The reader gives times in ms, sidecars in s."""
    res = {}
    if rec.get("mixed") or rec.get("multiframe"):
        why = "mixed series" if rec.get("mixed") else "enhanced multi-frame (5.1: unsupported)"
        for field in ("RepetitionTime", "EchoTime", "InversionTime", "FlipAngle", "ImageType", "EchoNumber"):
            res[field] = "not_comparable"
        notes = [why]
    else:
        notes = []
        tr = side.get("RepetitionTime")
        res["RepetitionTime"] = "not_in_reference" if tr is None else ("equal" if _close(rec.get("tr_ms"), tr * 1000, 0.01) else "mismatch")
        te = side.get("EchoTime")
        if te is None:
            res["EchoTime"] = "not_in_reference"
        elif rec.get("echoes"):
            res["EchoTime"] = "equal" if any(_close(e["te_ms"], te * 1000, 0.01) for e in rec["echoes"]) else "mismatch"
        else:
            res["EchoTime"] = "equal" if _close(rec.get("te_ms"), te * 1000, 0.01) else "mismatch"
        ti = side.get("InversionTime")
        if ti is None:
            res["InversionTime"] = "not_in_reference"
        else:
            res["InversionTime"] = "equal" if _close(rec.get("ti_ms"), ti * 1000, 0.01) else "mismatch"
        fa = side.get("FlipAngle")
        res["FlipAngle"] = "not_in_reference" if fa is None else ("equal" if _close(rec.get("flip_deg"), fa, 0.01) else "mismatch")
        it = side.get("ImageType")
        if it is None:
            res["ImageType"] = "not_in_reference"
        else:
            # dcm2niix appends its own part token (MAGNITUDE, PHASE, REAL, IMAGINARY) to the
            # DICOM ImageType values; that token is compared only when the DICOM holds it.
            mine = set(rec.get("image_type") or [])
            ref_tokens = {str(t).upper() for t in it}
            ref_tokens = {t for t in ref_tokens if t not in DCM2NIIX_PART_TOKENS or t in mine}
            res["ImageType"] = "equal" if ref_tokens == mine else "mismatch"
        en = side.get("EchoNumber")
        if en is None:
            res["EchoNumber"] = "not_in_reference"
        elif rec.get("echoes"):
            res["EchoNumber"] = "equal" if any(e.get("echo_number") == int(en) for e in rec["echoes"]) else "mismatch"
        else:
            res["EchoNumber"] = "not_comparable"
            notes.append("single-echo series: the 5.1 record keeps no echo number")
    man = side.get("Manufacturer")
    if man is None:
        res["Manufacturer"] = "not_in_reference"
    else:
        res["Manufacturer"] = "equal" if str(rec.get("manufacturer") or "").upper() == str(man).split()[0].upper() else "mismatch"
    fs = side.get("MagneticFieldStrength")
    res["MagneticFieldStrength"] = "not_in_reference" if fs is None else ("equal" if _close(rec.get("field_t"), fs, 0.001) else "mismatch")
    return res, notes
