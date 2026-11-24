"""Temporary round 4 projects (plan 2.1 item 5): roster, ZIPs from the catalog, commands in child
processes, and the CSVs a test reads back.

Commands always run as ``sys.executable -m mri_dicom_pipeline <command> --project-root <root>``
in a child process. Nothing here runs against a path outside the temporary project.
"""

from __future__ import annotations

import csv
import io
import os
import struct
import subprocess
import sys
import zipfile

from r4lib import catalog, dicom_builder

LIVE_PROJECT = os.path.normcase(os.path.abspath(r"D:\mri_project"))


def _refuse_live(path):
    p = os.path.normcase(os.path.abspath(str(path)))
    if p == LIVE_PROJECT or p.startswith(LIVE_PROJECT + os.sep):
        raise RuntimeError("round 4 test helpers never touch the live project")


class Result:
    def __init__(self, code, stdout, stderr):
        self.code, self.stdout, self.stderr = code, stdout, stderr


class R4Project:
    def __init__(self, root):
        _refuse_live(root)
        self.root = os.path.abspath(str(root))
        os.makedirs(os.path.join(self.root, "DicomUploads", "Uploads"), exist_ok=True)

    # -------------------------------------------------------------- inputs
    def write_roster(self, rows):
        with open(os.path.join(self.root, "sessions.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["subject_id", "scan_date"])
            w.writerows(rows)

    def zip_bytes(self, exam, session):
        """The ZIP of one catalog session (expanded series), honouring same_objects_as and
        member_order."""
        if session.get("same_objects_as"):
            source = next(s for s in exam["sessions"] if s.get("key", s["label"]) == session["same_objects_as"])
            return dicom_builder.session_zip_bytes(exam, source, source["series"],
                                                   sentinels=any(x.get("sentinels") for x in source["series"]),
                                                   member_order=session.get("member_order"))
        return dicom_builder.session_zip_bytes(exam, session, session["series"],
                                               sentinels=any(x.get("sentinels") for x in session["series"]),
                                               member_order=session.get("member_order"))

    def zip_target(self, session):
        if session.get("upload_path"):
            return os.path.join(self.root, "DicomUploads", *session["upload_path"].split("/"))
        return os.path.join(self.root, "DicomUploads", "Uploads", session["zip_name"])

    def place(self, exam, session, data=None):
        path = self.zip_target(session)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = self.zip_bytes(exam, session) if data is None else data
        corrupt = session.get("corrupt")
        if corrupt in ("truncate", "truncate_then_complete"):
            data = data[: int(len(data) * 0.7)]
        elif corrupt == "bad_crc":
            data = corrupt_crc(data)
        elif corrupt == "encryption_flag":
            data = set_encryption_flag(data)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def place_exam(self, exam, keys=None):
        self.write_roster(exam["roster_rows"])
        out = []
        for session in exam["sessions"]:
            if keys is None or session.get("key", session["label"]) in keys:
                out.append(self.place(exam, session))
        return out

    # -------------------------------------------------------------- commands
    def run(self, command, *args, env=None, timeout=600):
        argv = [sys.executable, "-m", "mri_dicom_pipeline", command, "--project-root", self.root, *map(str, args)]
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout, cwd=self.root)
        return Result(proc.returncode, proc.stdout, proc.stderr)

    # -------------------------------------------------------------- outputs
    def read_csv(self, *parts):
        path = os.path.join(self.root, *parts)
        if not os.path.exists(path):
            return []
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def issues(self):
        return self.read_csv("Reports", "issues.csv")

    def session_status(self):
        return self.read_csv("Reports", "session_status.csv")


def load_exam(cat_name, exam_id):
    data = catalog.load_file(cat_name)
    return next(e for e in data["exams"] if e["id"] == exam_id)


# ---------------------------------------------------------------------------- ZIP damage (R4-NAM-10)

def _first_local_header(data):
    if data[:4] != b"PK\x03\x04":
        raise ValueError("not a ZIP local header")
    name_len, extra_len = struct.unpack("<HH", data[26:30])
    return 30 + name_len + extra_len


def corrupt_crc(data):
    """Flip one byte inside the first member's compressed data: the structure stays valid, the
    CRC check fails."""
    start = _first_local_header(data)
    b = bytearray(data)
    b[start + 10] ^= 0xFF
    return bytes(b)


def set_encryption_flag(data):
    """Set general-purpose bit 0 (encrypted) on every local and central header (APPNOTE 4.4.4)."""
    b = bytearray(data)
    i = 0
    while True:
        i = b.find(b"PK\x03\x04", i)
        if i < 0:
            break
        flags = struct.unpack_from("<H", b, i + 6)[0] | 1
        struct.pack_into("<H", b, i + 6, flags)
        i += 4
    i = 0
    while True:
        i = b.find(b"PK\x01\x02", i)
        if i < 0:
            break
        flags = struct.unpack_from("<H", b, i + 8)[0] | 1
        struct.pack_into("<H", b, i + 8, flags)
        i += 4
    return bytes(b)
