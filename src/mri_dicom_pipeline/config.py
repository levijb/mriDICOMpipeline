"""Command-line options and storage-root configuration."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import re
import uuid

from . import __version__, identity


SCHEMA_VERSION = 3


def config_parser(phase):
    p = argparse.ArgumentParser(description={"prepare": "Sort, extract and cache DICOM ZIPs",
                                             "convert": "Convert prepared sessions with parallel subjects on one node",
                                             "select": "Choose one acquisition per modality folder for each converted "
                                                       "session"}[phase])
    p.add_argument("--project-root", type=Path, default=Path.cwd(), help="Study data directory; default: current working directory")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--reference-csv", type=Path, help="Session reference; default: PROJECT_ROOT/sessions.csv")
    p.add_argument("--subject-column", default="subject_id", help="Subject ID column in the reference CSV (default: subject_id)")
    p.add_argument("--date-column", default="scan_date", help="Acquisition date column in the reference CSV (default: scan_date)")
    p.add_argument("--report-root", type=Path)
    p.add_argument("--dry-run", action="store_true", help="Plan only: no project writes or converter calls; hashes files only when combined with --audit")
    p.add_argument("--audit", action="store_true", help="Explicitly hash cached outputs; prepare also re-hashes canonical ZIPs; deliberately slower")
    p.add_argument("--session", action="append", metavar="SUBJECT/SESSION", help="Limit work to this session; repeatable")
    if phase != "select":
        p.add_argument("--workers", type=int)
        p.add_argument("--limit", type=int, help="Maximum new/changed sessions to process, after cache checks")
        p.add_argument("--retry-failed", action="store_true", help="Retry unchanged failed/partial work")
        p.add_argument("--allow-unlisted-dates", action="store_true", help="Allow known IDs with dates absent from the reference")
    if phase == "prepare":
        p.add_argument("--input-root", type=Path, help="DicomUploads root (contains Uploads and SubjectsSorted)")
        p.add_argument("--output-root", type=Path, help="DicomExtracted root")
        p.add_argument("--force-reextract", action="store_true")
        p.add_argument("--accept-multi-study", action="append", metavar="SUBJECT/SESSION",
                       help="Accept this session's ZIP although it holds more than one study; repeatable")
        p.add_argument("--accept-date-mismatch", action="append", metavar="SUBJECT/SESSION",
                       help="Accept this session's ZIP although its DICOM StudyDate is more than --date-tolerance days "
                            "from the ZIP-name date; repeatable")
        p.add_argument("--date-tolerance", type=int, default=2, metavar="DAYS",
                       help="Largest gap in days between the DICOM StudyDate and the session date that only warns; "
                            "a larger gap holds the ZIP, and an unnamed ZIP takes a listed date this close "
                            "(default 2)")
    elif phase == "convert":
        p.add_argument("--dicom-root", type=Path, help="DicomExtracted root")
        p.add_argument("--output-root", type=Path, help="Niftis root")
        p.add_argument("--staging-root", type=Path, help="Optional node-local scratch; default is project/.work")
        p.add_argument("--dcm2niix-bin", default="dcm2niix")
        p.add_argument("--timeout", type=int, default=14400, help="Seconds per session")
        p.add_argument("--force-reconvert", action="store_true")
        p.add_argument("--accept-partial", action="append", metavar="SUBJECT/SESSION",
                       help="After reviewing its issue, publish this session although some of its conversion "
                            "failed; the errors are recorded; repeatable")
    else:
        p.add_argument("--dicom-root", type=Path, help="DicomExtracted root")
        p.add_argument("--nifti-root", type=Path, help="Niftis root (read only)")
        p.add_argument("--output-root", type=Path,
                       help="Folder for the selection reports and, with --publish, the selected sessions; "
                            "default: PROJECT_ROOT/Selected, or PROJECT_ROOT/BIDS with --bids")
        p.add_argument("--subjects", type=Path, metavar="CSV",
                       help="Only select sessions of subjects listed in this CSV (column --subject-column)")
        p.add_argument("--profile", type=Path, help="JSON profile merged over the built-in selection profile")
        p.add_argument("--overrides", type=Path,
                       help="Reviewer decisions; default: PROJECT_ROOT/selection_overrides.csv if present")
        p.add_argument("--publish", action="store_true",
                       help="Also copy the selected sessions into the output root")
        p.add_argument("--modalities", metavar="LIST",
                       help="Comma-separated folders to select and copy, for example T1,FLAIR; default: all")
        p.add_argument("--extras", action="store_true",
                       help="Also copy the scans that were not selected, into OTHER/<modality>/")
        p.add_argument("--complete-only", action="store_true",
                       help="Publish a session only when every modality in --modalities has a selection")
        p.add_argument("--3d-only", dest="three_d_only", action="store_true",
                       help="Only 3D scans for T1, T1_POST, T2, FLAIR, SWI, QSM and GRE")
        p.add_argument("--isotropic", action="store_true",
                       help="Only scans whose native voxel sides are within 20%% of each other")
        p.add_argument("--max-voxel-mm", type=float, metavar="MM",
                       help="Only scans whose every native voxel side is at most MM, for example 1.2")
        p.add_argument("--min-directions", type=int, metavar="N",
                       help="Only diffusion scans with at least N directions in one shell, for example 30")
        p.add_argument("--no-fallback", action="store_true",
                       help="Only scans in a preferred tier (no 2D T1, no FLAIR or T2 thicker than 3 mm)")
        p.add_argument("--field-strength", metavar="LIST",
                       help="Only sessions from scanners of these field strengths in tesla, for example 3 or 1.5,3")
        p.add_argument("--manufacturer", metavar="LIST",
                       help="Only sessions from these manufacturers, for example GE or GE,SIEMENS")
        p.add_argument("--bids", action="store_true",
                       help="Name and arrange the selection as a BIDS dataset instead of the Selected/ layout; "
                            "--publish copies the files")
        p.add_argument("--bids-contrast-agent", metavar="AGENT", type=str.lower,
                       help="With --bids: the contrast agent of post-contrast T1w files (ce-AGENT), for example "
                            "gadolinium; default: the agent the BIDS root recorded, else unknown")
    return p


def configure(args, phase):
    args.project_root = args.project_root.resolve()
    args.reports = (args.report_root or args.project_root / "Reports").resolve()
    args.reference = (args.reference_csv or args.project_root / "sessions.csv").resolve()
    args.uploads = (getattr(args, "input_root", None) or args.project_root / "DicomUploads").resolve()
    args.dicoms = ((args.output_root if phase == "prepare" else args.dicom_root) or args.project_root / "DicomExtracted").resolve()
    args.niftis = ((args.output_root if phase == "convert" else getattr(args, "nifti_root", None))
                   or args.project_root / "Niftis").resolve()
    args.work = (getattr(args, "staging_root", None) or args.project_root / ".work").resolve()
    # These roots must never contain one another. Work may be node-local.
    roots = [args.uploads, args.dicoms, args.niftis, args.reports, args.work]
    if phase == "select":
        if args.bids_contrast_agent is not None and not args.bids:
            raise ValueError("--bids-contrast-agent needs --bids")
        if args.bids_contrast_agent is not None and not re.fullmatch(r"[a-z0-9]+", args.bids_contrast_agent):
            raise ValueError("--bids-contrast-agent must be letters and digits only, for example gadolinium")
        args.output = (args.output_root or args.project_root / ("BIDS" if args.bids else "Selected")).resolve()
        args.overrides = (args.overrides or args.project_root / "selection_overrides.csv").resolve()
        roots.append(args.output)
    for i, a in enumerate(roots):
        for b in roots[i+1:]:
            if a == b or a.is_relative_to(b) or b.is_relative_to(a):
                raise ValueError(f"Storage roots overlap: {a} and {b}")
    if phase == "prepare" and args.date_tolerance < 0:
        raise ValueError("date-tolerance must be 0 or more days")
    if phase == "select":
        args.workers, args.limit = 1, None
    else:
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
    args.accept_date_mismatch = {"/".join(identity.parse_key(v)) for v in getattr(args, "accept_date_mismatch", None) or []}
    args.accept_partial = {"/".join(identity.parse_key(v)) for v in getattr(args, "accept_partial", None) or []}
    # Every root the command touches is locked (Q13).
    args.lock_roots = ([args.uploads, args.dicoms, args.reports, args.work] if phase == "prepare"
                       else [args.dicoms, args.niftis, args.reports, args.output] if phase == "select"
                       else [args.dicoms, args.niftis, args.reports, args.work])
    args.run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    return args
