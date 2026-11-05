"""Command-line options and storage-root configuration."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import uuid

from . import __version__, identity


SCHEMA_VERSION = 3


def config_parser(phase):
    p = argparse.ArgumentParser(description=("Sort, extract and cache DICOM ZIPs" if phase == "prepare" else "Convert prepared sessions with parallel subjects on one node"))
    p.add_argument("--project-root", type=Path, default=Path.cwd(), help="Study data directory; default: current working directory")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--reference-csv", type=Path, help="Session reference; default: PROJECT_ROOT/sessions.csv")
    p.add_argument("--subject-column", default="subject_id", help="Subject ID column in the reference CSV (default: subject_id)")
    p.add_argument("--date-column", default="scan_date", help="Acquisition date column in the reference CSV (default: scan_date)")
    p.add_argument("--report-root", type=Path)
    p.add_argument("--workers", type=int)
    p.add_argument("--limit", type=int, help="Maximum new/changed sessions to process, after cache checks")
    p.add_argument("--dry-run", action="store_true", help="Plan only: no project writes or converter calls; hashes files only when combined with --audit")
    p.add_argument("--audit", action="store_true", help="Explicitly hash cached outputs; prepare also re-hashes canonical ZIPs; deliberately slower")
    p.add_argument("--session", action="append", metavar="SUBJECT/SESSION", help="Limit work to this session; repeatable")
    p.add_argument("--retry-failed", action="store_true", help="Retry unchanged failed/partial work")
    p.add_argument("--allow-unlisted-dates", action="store_true", help="Allow known IDs with dates absent from the reference")
    if phase == "prepare":
        p.add_argument("--input-root", type=Path, help="DicomUploads root (contains Uploads and SubjectsSorted)")
        p.add_argument("--output-root", type=Path, help="DicomExtracted root")
        p.add_argument("--force-reextract", action="store_true")
        p.add_argument("--import-legacy", action="store_true", help="Also import DicomUploads/Subjects and accept validated six-digit dates there")
        p.add_argument("--accept-multi-study", action="append", metavar="SUBJECT/SESSION",
                       help="Accept this session's ZIP although it holds more than one study; repeatable")
    else:
        p.add_argument("--dicom-root", type=Path, help="DicomExtracted root")
        p.add_argument("--output-root", type=Path, help="Niftis root")
        p.add_argument("--staging-root", type=Path, help="Optional node-local scratch; default is project/.work")
        p.add_argument("--dcm2niix-bin", default="dcm2niix")
        p.add_argument("--timeout", type=int, default=14400, help="Seconds per session")
        p.add_argument("--force-reconvert", action="store_true")
    return p


def configure(args, phase):
    args.project_root = args.project_root.resolve()
    args.reports = (args.report_root or args.project_root / "Reports").resolve()
    args.reference = (args.reference_csv or args.project_root / "sessions.csv").resolve()
    args.uploads = (getattr(args, "input_root", None) or args.project_root / "DicomUploads").resolve()
    args.dicoms = ((args.output_root if phase == "prepare" else args.dicom_root) or args.project_root / "DicomExtracted").resolve()
    args.niftis = ((args.output_root if phase == "convert" else None) or args.project_root / "Niftis").resolve()
    args.work = (getattr(args, "staging_root", None) or args.project_root / ".work").resolve()
    # These roots must never contain one another. Work may be node-local.
    roots = [args.uploads, args.dicoms, args.niftis, args.reports, args.work]
    for i, a in enumerate(roots):
        for b in roots[i+1:]:
            if a == b or a.is_relative_to(b) or b.is_relative_to(a):
                raise ValueError(f"Storage roots overlap: {a} and {b}")
    allocated = int(os.environ.get("SLURM_CPUS_PER_TASK", "1"))
    args.workers = args.workers if args.workers is not None else min(4, allocated)
    if args.workers < 1 or (args.limit is not None and args.limit < 1):
        raise ValueError("workers and limit must be positive")
    if phase == "convert" and args.timeout < 1:
        raise ValueError("timeout must be positive")
    if "SLURM_CPUS_PER_TASK" in os.environ and args.workers > allocated:
        raise ValueError("workers exceeds SLURM_CPUS_PER_TASK")
    args.sessions = {"/".join(identity.parse_key(v)) for v in args.session} if args.session else None
    args.accept_multi_study = {"/".join(identity.parse_key(v)) for v in getattr(args, "accept_multi_study", None) or []}
    # Every root the command touches is locked (Q13).
    args.lock_roots = ([args.uploads, args.dicoms, args.reports, args.work] if phase == "prepare"
                       else [args.dicoms, args.niftis, args.reports, args.work])
    args.run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    return args
