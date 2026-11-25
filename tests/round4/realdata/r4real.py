"""Shared helpers for the round 4 real-data scripts (phase 3, R4-VAL-01 to R4-VAL-06).

Read-only toward the project: files are only opened for reading and folders only listed. Every script
refuses an output folder (evidence or private) inside the project. Evidence names visits and sessions
by the codes in <private>/visit_codes.json (r4_codes.py); anything holding subject IDs, dates or
paths stays in the private folder. Printed output holds counts and codes only: no series description, name,
UID or date.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROUND4 = os.path.dirname(HERE)
if ROUND4 not in sys.path:
    sys.path.insert(0, ROUND4)


def inside(path, root):
    p, r = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return p == r or p.startswith(r + os.sep)


def check_outputs(project, *outputs):
    for out in outputs:
        if out and inside(out, project):
            raise SystemExit(f"refused: an output folder lies inside the project ({os.path.basename(out)})")


class Codes:
    def __init__(self, private):
        path = os.path.join(private, "visit_codes.json")
        if not os.path.exists(path):
            raise SystemExit("visit_codes.json is missing in the private folder; run r4_codes.py first")
        with open(path, encoding="utf-8") as f:
            table = json.load(f)
        self.sessions, self.visits, self.subjects = table["sessions"], table["visits"], table["subjects"]

    def session(self, subject, label):
        return self.sessions.get(f"{subject}/{label}", "?")

    def visit(self, subject, date):
        return self.visits.get(f"{subject}/{date[:8]}", "?")


def converted_sessions(project):
    """[(subject, label, extraction folder, conversion folder)] of every converted session (state index)."""
    with open(os.path.join(project, "Reports", ".state", "index.json"), encoding="utf-8") as f:
        index = json.load(f)
    out = []
    for _key, entry in sorted(index.get("sessions", {}).items()):
        conv = entry.get("conversion") or {}
        if conv.get("status") != "converted":
            continue
        sid, label = entry["subject_id"], entry["label"]
        out.append((sid, label, os.path.join(project, "DicomExtracted", "Subjects", sid, label),
                    os.path.join(project, "Niftis", "Subjects", sid, label)))
    return out


def cache_doc(project, subject, label):
    path = os.path.join(project, "Reports", ".selection_cache", subject, f"{label}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cache_volumes(doc):
    vols = doc.get("volumes", {})
    return list(vols.items()) if isinstance(vols, dict) else [(v.get("relpath", ""), v) for v in vols]


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def sidecar(project, source):
    path = os.path.join(project, "Niftis", *source.split("/"))
    path = path[:-7] + ".json" if path.endswith(".nii.gz") else path
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def series_key(uid):
    return hashlib.sha256(str(uid).encode("ascii")).hexdigest()[:20]


def wilson(k, n, z=1.959964):
    """(rate, low, high) with the 95% Wilson score interval; None when n is 0."""
    if not n:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return {"k": k, "n": n, "rate": round(p, 4), "low": round(max(0.0, centre - half), 4),
            "high": round(min(1.0, centre + half), 4)}


def dicom_index(folder, want_software=True):
    """{series key: {"paths": [...], "software": [values in DICOM order]}} for one extraction folder.

    Reads only SeriesInstanceUID and SoftwareVersions (pydicom, header only). Files that are not DICOM
    (receipts, inventories) are skipped."""
    import pydicom

    out = {}
    for dirpath, _d, files in os.walk(folder):
        for name in sorted(files):
            path = os.path.join(dirpath, name)
            try:
                ds = pydicom.dcmread(path, stop_before_pixels=True,
                                     specific_tags=["SeriesInstanceUID", "SoftwareVersions"])
            except Exception:  # noqa: BLE001 - not DICOM
                continue
            uid = ds.get("SeriesInstanceUID")
            if not uid:
                continue
            item = out.setdefault(series_key(uid), {"paths": [], "software": None})
            item["paths"].append(path)
            if want_software and item["software"] is None:
                value = ds.get("SoftwareVersions")
                if value is None:
                    item["software"] = []
                elif value.__class__.__name__ == "MultiValue":
                    item["software"] = [str(v) for v in value]
                else:
                    item["software"] = [str(value)]
    return out
