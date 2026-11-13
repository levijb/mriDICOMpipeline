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


def reports(state, cfg, pairs, logger):
    """Rewrite session_status.csv and issues.csv.

    A file that cannot be replaced, for example because a spreadsheet program
    holds it open on Windows, is logged and rewritten at the next report write;
    the run continues (Q2).
    """
    rows = []
    sessions, issues = state.data["sessions"], state.data["issues"]
    held = {f"{issue.get('subject')}/{issue.get('session')}" for key, issue in issues.items() if key.startswith("prepare:")}
    keys = {f"{sid}/{date}" for sid, date in pairs} | set(sessions)
    for key in sorted(keys):
        sid, label = key.split("/", 1)
        entry = sessions.get(key, {})
        conv = entry.get("conversion", {})
        coverage, attempt = conv.get("coverage", {}), entry.get("last_attempt", {})
        conversion_status = conv.get("status", "not_converted")
        if conv.get("accepted_errors"):
            conversion_status = "accepted_partial"
        if conv and conv.get("source_sha256") != entry.get("content_sha256"):
            conversion_status = "needs_conversion"
        if "output:" + key in issues:
            conversion_status = "output_modified"
        rows.append({"subject_id": sid, "session": label,
            "prepare_status": "held" if key in held else entry.get("status", "awaiting_upload"),
            "dicom_instances": entry.get("n_dicom", ""),
            "series": entry.get("n_series", ""), "conversion_status": conversion_status,
            "converted_volumes": conv.get("n_volumes", ""), "archive": entry.get("archive", ""),
            "nifti_folder": conv.get("output", ""),
            "required_series": ";".join(coverage.get("required", [])),
            "converted_series": ";".join(coverage.get("converted", [])),
            "excused_series": ";".join(coverage.get("excused", [])),
            "missing_series": ";".join(coverage.get("missing", [])),
            "accepted_errors": "; ".join(conv.get("accepted_errors", [])),
            "last_attempt_status": attempt.get("status", ""), "last_attempt_folder": attempt.get("output", "")})
    for path, data, fields in [
            (cfg.reports / "session_status.csv", rows, list(rows[0]) if rows else ["subject_id", "session"]),
            (cfg.reports / "issues.csv", sorted(issues.values(), key=lambda x: x["key"]),
             ["key", "stage", "subject", "session", "reason", "path"])]:
        try:
            write_csv(path, data, fields)
        except OSError as e:
            logger.warning("Could not write %s (is it open in another program?); it is rewritten at the next "
                           "report write: %s", path, e)


HISTORY_FIELDS = ["run_id", "stage", "outcome", "seconds", "completed", "cached", "duplicate", "failed", "held",
                  "unresolved", "reconciled", "notes"]


def append_history(cfg, row, logger):
    """Append one row to run_history.csv. Every locked invocation writes one, including early aborts (R16)."""
    path = cfg.reports / "run_history.csv"
    try:
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS, extrasaction="ignore")
            if f.tell() == 0:
                writer.writeheader()
            writer.writerow(row)
    except OSError as e:
        logger.error("Could not append to %s (is it open in another program?): %s; the row was %s", path, e, row)


def problem(key, phase, sid, label, reason, path=""):
    return {"key": key, "stage": phase, "subject": sid, "session": label, "reason": reason, "path": str(path)}


# An issue is blocking when it counts toward the unresolved total that makes a
# run exit with code 2. Nonblocking issues are reported only: header dates that
# differ from the file name, a canonical ZIP missing from SubjectsSorted, and
# uneven slice spacing in a converted series.
NONBLOCKING_PREFIXES = ("date:", "missing:", "spacing:")


def unresolved(state):
    return sum(not key.startswith(NONBLOCKING_PREFIXES) for key in state.data["issues"])
