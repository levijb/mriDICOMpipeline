"""Logs, CSV reports and issue records."""

from __future__ import annotations

import csv
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import uuid


def setup_log(reports, phase, dry_run):
    logger = logging.getLogger("mri_dicom_pipeline")
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y%m%d %H:%M:%S")
    handlers = [logging.StreamHandler(sys.stdout)]
    if not dry_run:
        reports.mkdir(parents=True, exist_ok=True)
        handlers += [RotatingFileHandler(reports / "pipeline.log", maxBytes=10_000_000, backupCount=3, encoding="utf-8"),
                     logging.FileHandler(reports / f"latest_{phase}.log", mode="w", encoding="utf-8")]
    for handler in handlers:
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


def write_csv(path, rows, fields):
    temp = path.with_name(".tmp_" + uuid.uuid4().hex[:12])
    try:
        with temp.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def reports(state, cfg, pairs):
    rows = []
    sessions = state.data["sessions"]
    keys = {f"{sid}/{date}" for sid, date in pairs} | set(sessions)
    for key in sorted(keys):
        sid, label = key.split("/", 1)
        entry = sessions.get(key, {})
        conv = entry.get("conversion", {})
        conversion_status = conv.get("status", "not_converted")
        if conv and conv.get("source_sha256") != entry.get("content_sha256"):
            conversion_status = "needs_conversion"
        rows.append({"subject_id": sid, "session": label,
            "prepare_status": entry.get("status", "awaiting_upload"), "dicom_instances": entry.get("n_dicom", ""),
            "series": entry.get("n_series", ""), "conversion_status": conversion_status,
            "converted_volumes": conv.get("n_volumes", ""), "archive": entry.get("archive", ""),
            "nifti_folder": conv.get("output", "")})
    write_csv(cfg.reports / "session_status.csv", rows, list(rows[0]) if rows else ["subject_id", "session"])
    write_csv(cfg.reports / "issues.csv", sorted(state.data["issues"].values(), key=lambda x: x["key"]),
              ["key", "stage", "subject", "session", "reason", "path"])


def problem(key, phase, sid, label, reason, path=""):
    return {"key": key, "stage": phase, "subject": sid, "session": label, "reason": reason, "path": str(path)}


# An issue is blocking when it counts toward the unresolved total that makes a
# run exit with code 2. Nonblocking issues are reported only: header dates that
# differ from the file name, and a canonical ZIP missing from SubjectsSorted.
NONBLOCKING_PREFIXES = ("date:", "missing:")


def unresolved(state):
    return sum(not key.startswith(NONBLOCKING_PREFIXES) for key in state.data["issues"])
