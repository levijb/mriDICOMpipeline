"""Sentinels (plan 3.2) and a scanner over outputs (plan 2.1 item 9; R4-PRV-01/02/06, R4-OR-09).

The scanner reads every file as bytes and also, for ``.gz`` files, the decompressed bytes (NIfTI
header text such as descrip and aux_file lives there). It reports which needles each file holds.
"""

from __future__ import annotations

import gzip
import os

from r4lib import dicom_builder

PREFIX = "R4SENT_"
UID_ROOT = dicom_builder.UID_ROOT
FIELD_SENTINELS = tuple(dicom_builder.sentinel_value(k) for k in dicom_builder.SENTINEL_FIELDS
                        if k != "PatientBirthDate")
PATH_SENTINELS = ("R4SENT_FOLDER", "R4SENT_ZIP", "R4SENT_MEMBER/")
DATE_SENTINEL = dicom_builder.SENTINEL_DATE


def needles_for_prv01():
    """Every field sentinel, the member-path sentinel and the UID root; not R4SENT_FOLDER or
    R4SENT_ZIP, which R4-PRV-02 checks (plan 3.2 expected exceptions)."""
    return FIELD_SENTINELS + ("R4SENT_MEMBER/", UID_ROOT)


def _contents(path, max_bytes=None):
    with open(path, "rb") as f:
        data = f.read() if max_bytes is None else f.read(max_bytes)
    out = [data]
    if path.lower().endswith(".gz"):
        try:
            with gzip.open(path, "rb") as g:
                out.append(g.read() if max_bytes is None else g.read(max_bytes))
        except (OSError, EOFError):
            pass
    return out


def scan_file(path, needles, max_bytes=None):
    found = set()
    encoded = [(n, n.encode("utf-8")) for n in needles]
    for blob in _contents(path, max_bytes):
        for text, raw in encoded:
            if raw in blob:
                found.add(text)
    return sorted(found)


def scan_tree(root, needles, skip=lambda rel: False, max_bytes=None):
    """{relative path: [needles found]} for every file below ``root`` that holds one."""
    hits = {}
    for dirpath, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if skip(rel):
                continue
            found = scan_file(full, needles, max_bytes)
            if found:
                hits[rel] = found
    return hits
