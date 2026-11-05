"""Shared implementation for the study-agnostic DICOM commands (Python >= 3.10).

Only the coordinator updates permanent state/reports. Workers own distinct
subjects, staging directories and output sessions. No patient identity fields
are used or copied into state. A normal rerun never traverses old DICOM trees.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import copy
import csv
import errno
import gzip
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime

from . import __version__

VERSION = 2
NAME = re.compile(r"^(?P<id>[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*)_(?P<date>[0-9]{8})(?:_(?P<part>[0-9]+))?\.zip$", re.I)
SAFE_ID = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*\Z")
SKIP_EXT = set(".jpg .jpeg .png .gif .bmp .tif .tiff .pdf .txt .xml .html .htm .csv .json .ini .log .dll .exe .ocx .config .manifest .css .js .ico .onnx .lic .profiles .mapping .bin .rlv .rlv_meta .rbv .rbv_meta .local .zip".split())
# Free-text descriptions are used in memory for classification, never copied
# to public metadata. Technical numeric fields are selected explicitly.
TECH_FIELDS = {"Modality", "Manufacturer", "ManufacturersModelName", "MagneticFieldStrength",
    "SeriesNumber", "EchoNumber", "EchoTime", "RepetitionTime", "InversionTime", "FlipAngle",
    "ImageType", "ImageOrientationPatientDICOM", "PixelSpacing", "SliceThickness",
    "SpacingBetweenSlices", "MRAcquisitionType", "ScanningSequence", "SequenceVariant",
    "PhaseEncodingDirection", "TotalReadoutTime", "EffectiveEchoSpacing", "DiffusionBValue",
    "ConversionSoftware", "ConversionSoftwareVersion"}
DICOM_TAGS = ["SOPClassUID", "SOPInstanceUID", "StudyInstanceUID", "SeriesInstanceUID",
    "SeriesNumber", "SeriesDescription", "ProtocolName", "Modality", "Rows", "Columns",
    "BitsAllocated", "SamplesPerPixel", "NumberOfFrames", "InstanceNumber", "StudyDate"]


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(".tmp_" + uuid.uuid4().hex[:12])
    try:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def signature(path):
    s = Path(path).stat()
    return [s.st_size, s.st_mtime_ns]


def sha256(path, stop=None):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            if stop is not None and stop.is_set():
                raise InterruptedError("Preparation interrupted")
            h.update(block)
    return h.hexdigest()


def owned(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"Refusing operation outside owned directory: {path}")
    return path


def remove_tree(path, root):
    path = owned(path, root)
    if path.exists():
        shutil.rmtree(path)


def publish_tree(stage, destination, ownership_root):
    """Copy to a sibling, then swap; retain the previous version until committed."""
    destination = owned(destination, ownership_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep swap names short: nested files may already approach Windows limits.
    incoming = destination.with_name(".incoming_" + uuid.uuid4().hex[:12])
    previous = destination.with_name(".previous_" + uuid.uuid4().hex[:12])
    try:
        try:
            Path(stage).rename(incoming)
        except OSError as e:
            if e.errno != errno.EXDEV and getattr(e, "winerror", None) != 17:
                raise
            shutil.copytree(stage, incoming)
        if destination.exists():
            destination.rename(previous)
        try:
            incoming.rename(destination)
        except BaseException:
            if previous.exists():
                previous.rename(destination)
            raise
        remove_tree(previous, ownership_root)
    finally:
        remove_tree(incoming, ownership_root)


def parse_name(name, legacy=False):
    if legacy:
        m = re.fullmatch(r"(.+)_([0-9]{6})\.zip", name, re.I)
        if m:
            old = m[2]
            d = datetime(2000 + int(old[4:]), int(old[:2]), int(old[2:4]))
            name = f"{m[1]}_{d:%Y%m%d}.zip"
    m = NAME.fullmatch(name)
    if not m:
        raise ValueError("Expected ID_YYYYMMDD.zip or ID_YYYYMMDD_2.zip")
    datetime.strptime(m["date"], "%Y%m%d")
    part = m["part"]
    if part and (int(part) < 2 or str(int(part)) != part):
        raise ValueError("Additional session suffix must be an integer >= 2, without leading zeros")
    label = m["date"] + ("_" + part if part else "")
    return m["id"], m["date"], label


def load_roster(path, subject_column="subject_id", date_column="scan_date"):
    pairs, ids, warnings = set(), set(), []
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not subject_column or not date_column or subject_column == date_column:
            raise ValueError("Subject and date column names must be nonempty and distinct")
        if len(reader.fieldnames or []) != len(set(reader.fieldnames or [])):
            raise ValueError("Reference CSV contains duplicate column names")
        if not {subject_column, date_column}.issubset(reader.fieldnames or []):
            raise ValueError(f"Reference CSV must contain {subject_column!r} and {date_column!r}")
        for line, row in enumerate(reader, 2):
            sid, raw = (row.get(subject_column) or "").strip(), (row.get(date_column) or "").strip()
            if not sid and not raw:
                continue
            if not SAFE_ID.fullmatch(sid):
                warnings.append(f"Reference row {line}: invalid {subject_column}; row ignored")
                continue
            ids.add(sid)
            if not raw:
                continue
            try:
                date_format = "%Y%m%d" if re.fullmatch(r"[0-9]{8}", raw) else "%Y-%m-%d" if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw) else "%m/%d/%Y"
                d = datetime.strptime(raw, date_format)
                pairs.add((sid, d.strftime("%Y%m%d")))
            except ValueError:
                warnings.append(f"Reference row {line}: invalid {date_column}; row ignored")
    if not pairs:
        raise ValueError("Reference CSV contains no valid subject/date pairs")
    return ids, pairs, warnings


class ProjectLock:
    """OS lock releases on process exit, including crashes; file itself persists."""
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b" ")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.file.close()
            raise RuntimeError("Another pipeline command is running for this project") from e
        return self

    def __exit__(self, *args):
        self.file.close()


class State:
    """Compact index plus crash-recoverable, small per-result journals."""
    def __init__(self, report_root):
        self.root = Path(report_root) / ".state"
        self.path = self.root / "index.json"
        self.data = read_json(self.path) if self.path.exists() else {"version": VERSION, "sessions": {}, "sources": {}, "issues": {}}
        if self.data.get("version") != VERSION:
            raise ValueError("Unsupported state version; preserve .state and migrate explicitly")
        self.pending = self.root / "pending"
        self.last_save = time.monotonic()
        for p in sorted(self.pending.glob("*.json")):
            self.apply(read_json(p))

    def apply(self, event):
        if event.get("session") is not None:
            self.data["sessions"][event["key"]] = event["session"]
        if event.get("source"):
            self.data["sources"][event["source"]["path"]] = event["source"]
        for source in event.get("sources", []):
            self.data["sources"][source["path"]] = source
        for key in event.get("clear_issues", []):
            self.data["issues"].pop(key, None)
        for issue in event.get("issues", []):
            self.data["issues"][issue["key"]] = issue

    def checkpoint(self, event):
        atomic_json(self.pending / (f"{time.time_ns():x}_" + uuid.uuid4().hex[:4] + ".json"), event)
        self.apply(event)
        if time.monotonic() - self.last_save > 10:
            self.flush()

    def flush(self):
        atomic_json(self.path, self.data)
        for p in self.pending.glob("*.json"):
            p.unlink()
        self.last_save = time.monotonic()


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


def fast_valid(entry, kind, audit=False):
    """O(directories), never O(DICOM instances), unless explicitly audited."""
    try:
        if entry.get("version") != VERSION or not entry.get("receipt"):
            return False
        if signature(entry["receipt"]) != entry["receipt_signature"]:
            return False
        for path, expected in entry["directories"].items():
            if Path(path).stat().st_mtime_ns != expected:
                return False
        if audit:
            inventory = read_json(entry["inventory"])
            for item in inventory["files"]:
                p = Path(entry["root"]) / item["name"]
                if signature(p)[0] != item["size"] or sha256(p) != item["sha256"]:
                    return False
        return True
    except (OSError, KeyError, ValueError):
        return False


def receipt_info(root, receipt, inventory):
    dirs = {str(p): p.stat().st_mtime_ns for p in root.rglob("*") if p.is_dir()}
    dirs[str(root)] = root.stat().st_mtime_ns
    return {"version": VERSION, "root": str(root), "receipt": str(receipt),
            "receipt_signature": signature(receipt), "directories": dirs, "inventory": str(inventory)}


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
    p.add_argument("--dry-run", action="store_true", help="Read-only plan; writes no files and performs no hashes/conversions")
    p.add_argument("--audit", action="store_true", help="Explicitly hash cached outputs; deliberately slower")
    p.add_argument("--retry-failed", action="store_true", help="Retry unchanged failed/partial work")
    p.add_argument("--allow-unlisted-dates", action="store_true", help="Allow known IDs with dates absent from the reference")
    if phase == "prepare":
        p.add_argument("--input-root", type=Path, help="DicomUploads root (contains Uploads and SubjectsSorted)")
        p.add_argument("--output-root", type=Path, help="DicomExtracted root")
        p.add_argument("--force-reextract", action="store_true")
        p.add_argument("--import-legacy", action="store_true", help="Also import DicomUploads/Subjects and accept validated six-digit dates there")
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
    return args


def classify(description, protocol="", image_type=None):
    text = f" {description or ''} {protocol or ''} ".upper()
    image_tokens = set(str(x).upper() for x in (image_type.split("\\") if isinstance(image_type, str) else image_type or []))
    if any(x in text for x in ("LOCALIZER", "SCOUT", "SURVEY")):
        return "LOCALIZER"
    if "EADC" in text:
        return "EADC"
    if "ADC" in text:
        return "ADC"
    if "QSM" in text:
        return "QSM"
    if any(x in text for x in ("SWI", "SWAN")):
        if any(x in text for x in ("PHA", "PHASE")) or image_tokens & {"P", "PHASE"}:
            return "SWI_PHASE"
        return "SWI"
    if any(x in text for x in ("MIN IP", "MINIP")):
        return "SWI_MINIP"
    for label, words in [("FLAIR", ("FLAIR",)), ("T1", ("T1", "MPRAGE", "BRAVO", "SPGR", "THRIVE")),
            ("T2", ("T2",)), ("DWI", ("DWI", "DIFFUSION", "TRACE", "DTI")),
            ("MRA", ("MRA", "TOF")), ("MRV", ("MRV",)), ("GRE", ("GRE",)),
            ("PERFUSION", ("PERFUSION", "DSC", "DCE", "ASL"))]:
        if any(word in text for word in words):
            return "T1_POST" if label == "T1" and any(w in text for w in ("POST", "GAD", "+C", "CONTRAST")) else label
    return "OTHER"


def discover(cfg):
    result = []
    roots = [(cfg.uploads / "SubjectsSorted", False), (cfg.uploads / "Uploads", False)]
    if cfg.import_legacy:
        roots.append((cfg.uploads / "Subjects", True))
    for root, legacy in roots:
        for folder, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(folder) / d).is_symlink())
            for name in sorted(files):
                if name.lower().endswith(".zip"):
                    result.append((Path(folder) / name, legacy))
    return result


def extract_archive(source, stage, stop=None):
    """Stream only DICOM objects, including extensionless objects; verify CRCs.

    Original member paths are never used as destination paths (ZIP traversal
    and duplicate-member overwrites cannot escape the staging directory).
    """
    import pydicom
    dicom_dir = stage / "dicom"
    dicom_dir.mkdir(parents=True)
    files, series, study_keys, dates = [], {}, set(), set()
    skipped, pixel_bytes = 0, 0
    with zipfile.ZipFile(source) as archive:
        for member in archive.infolist():
            if stop is not None and stop.is_set():
                raise InterruptedError("Preparation interrupted")
            if member.is_dir():
                continue
            parts = member.filename.replace("\\", "/").split("/")
            if "ddv" in [p.lower() for p in parts] or Path(member.filename).suffix.lower() in SKIP_EXT:
                skipped += 1
                continue
            with archive.open(member) as f:
                magic = f.read(132)[128:132] == b"DICM"
                f.seek(0)
                try:
                    ds = pydicom.dcmread(f, force=not magic, stop_before_pixels=True, specific_tags=DICOM_TAGS)
                except Exception as e:
                    if magic or Path(member.filename).suffix.lower() == ".dcm":
                        raise ValueError("Unreadable DICOM header in archive") from e
                    skipped += 1
                    continue
                if not ds.get("SOPClassUID") or not (ds.get("SeriesInstanceUID") or ds.get("Modality")):
                    skipped += 1
                    continue
                if str(ds.get("SOPClassUID")) == "1.2.840.10008.1.3.10":
                    skipped += 1
                    continue
                filename = f"{len(files)+1:08d}.dcm"
                dest = dicom_dir / filename
                f.seek(0)
                digest, size = hashlib.sha256(), 0
                with dest.open("wb") as out:
                    for block in iter(lambda: f.read(1024 * 1024), b""):
                        if stop is not None and stop.is_set():
                            raise InterruptedError("Preparation interrupted")
                        digest.update(block)
                        size += len(block)
                        out.write(block)
                files.append({"name": "dicom/" + filename, "size": size, "sha256": digest.hexdigest()})
                try:
                    expected_pixels = int(ds.get("Rows", 0)) * int(ds.get("Columns", 0)) * int(ds.get("SamplesPerPixel", 1)) * int(ds.get("BitsAllocated", 16)) // 8 * int(ds.get("NumberOfFrames", 1))
                    syntax = getattr(ds.file_meta, "TransferSyntaxUID", None)
                    if syntax and not syntax.is_compressed and not syntax.is_deflated and size < expected_pixels:
                        raise ValueError("Uncompressed DICOM is smaller than its declared pixel dimensions")
                except (AttributeError, TypeError):
                    expected_pixels = 0
                uid = str(ds.get("SeriesInstanceUID", ""))
                if not uid:
                    raise ValueError("DICOM missing SeriesInstanceUID; held for review")
                key = hashlib.sha256(uid.encode()).hexdigest()[:20]
                row = series.setdefault(key, {"series_key": key, "series_number": str(ds.get("SeriesNumber", "")),
                    "scan_type": classify(str(ds.get("SeriesDescription", "")), str(ds.get("ProtocolName", ""))), "instances": 0})
                row["instances"] += 1
                if ds.get("StudyInstanceUID"):
                    study_keys.add(hashlib.sha256(str(ds.StudyInstanceUID).encode()).hexdigest())
                if ds.get("StudyDate"):
                    dates.add(str(ds.StudyDate))
                try:
                    pixel_bytes += expected_pixels
                except (ValueError, TypeError):
                    pass
    if not files:
        raise ValueError("No DICOM objects found in archive")
    content = hashlib.sha256("\n".join(sorted(f"{r['sha256']}:{r['size']}" for r in files)).encode()).hexdigest()
    return {"files": files, "series": list(series.values()), "n_dicom": len(files), "n_series": len(series),
            "content_sha256": content, "dicom_bytes": sum(x["size"] for x in files),
            "pixel_bytes": pixel_bytes, "skipped_files": skipped, "study_count": len(study_keys), "header_dates": sorted(dates)}


def move_archive(source, destination, cfg, fingerprint):
    if source == destination:
        return
    owned(source, cfg.uploads)
    owned(destination, cfg.uploads / "SubjectsSorted")
    if signature(source) != fingerprint:
        raise ValueError("Upload changed while being processed; retry after transfer completes")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError("Canonical ZIP already exists; refusing to overwrite it")
    # Both locations are inside DicomUploads. Refuse cross-device moves rather
    # than silently copying/deleting sources on a differently mounted filesystem.
    source.rename(destination)


def prepare_one(candidate, previous, cfg, stop=None):
    source, sid, date, label, fingerprint = candidate
    key, issue_key = f"{sid}/{label}", "prepare:" + str(source)
    active_source = source
    stage = Path(tempfile.mkdtemp(prefix="prepare_", dir=cfg.work))
    event = {"key": key, "clear_issues": [issue_key], "issues": [], "outcome": "prepared"}
    digest = ""
    try:
        digest = sha256(source, stop)
        if signature(source) != fingerprint:
            raise ValueError("Upload changed during hashing; wait for transfer completion")
        expected_root = cfg.dicoms / "Subjects" / sid / label
        valid_previous = previous and previous.get("status") == "ready" and previous.get("extraction", {}).get("root") == str(expected_root) and fast_valid(previous["extraction"], "extract", cfg.audit)
        if valid_previous and previous["archive_sha256"] == digest and (not cfg.force_reextract or str(source) != previous.get("archive")):
            event["outcome"] = "duplicate"
        else:
            inventory = extract_archive(source, stage, stop)
            if signature(source) != fingerprint:
                raise ValueError("Upload changed during extraction; wait for transfer completion")
            replacement = previous and str(source) == previous.get("archive")
            if valid_previous and digest != previous["archive_sha256"] and not replacement:
                if inventory["content_sha256"] == previous["content_sha256"]:
                    event["outcome"] = "duplicate"
                else:
                    raise ValueError("Different DICOM contents for an existing session; ZIP retained in Uploads. Use an explicit _2 session or resolve the conflict")
            else:
                destination = cfg.dicoms / "Subjects" / sid / label
                if destination.exists() and not (destination / ".pipeline_complete.json").exists():
                    raise ValueError("Extracted destination is not owned by this pipeline; refusing replacement")
                canonical = cfg.uploads / "SubjectsSorted" / sid / f"{sid}_{label}.zip"
                move_archive(source, canonical, cfg, fingerprint)
                active_source = canonical
                atomic_json(stage / ".inventory.json", inventory)
                atomic_json(stage / ".pipeline_complete.json", {"version": VERSION, "archive_sha256": digest, "receipt_id": uuid.uuid4().hex})
                publish_tree(stage, destination, cfg.dicoms)
                extraction = receipt_info(destination, destination / ".pipeline_complete.json", destination / ".inventory.json")
                entry = {"subject_id": sid, "date": date, "label": label, "status": "ready", "version": VERSION,
                    "archive": str(canonical), "archive_signature": signature(canonical), "archive_sha256": digest,
                    "content_sha256": inventory["content_sha256"], "n_dicom": inventory["n_dicom"],
                    "n_series": inventory["n_series"], "dicom_bytes": inventory["dicom_bytes"],
                    "pixel_bytes": inventory["pixel_bytes"], "extraction": extraction,
                    "conversion": previous.get("conversion", {}) if previous else {}}
                event["session"] = entry
                event["sources"] = [{"path": str(canonical), "signature": signature(canonical), "key": key,
                    "outcome": "prepared", "against": digest, "version": VERSION}]
                event["note"] = f"{inventory['n_dicom']} DICOMs, {inventory['n_series']} series; {inventory['skipped_files']} viewer/non-DICOM files omitted"
                if inventory["header_dates"] and inventory["header_dates"] != [date]:
                    event["issues"].append(problem("date:" + key, "prepare", sid, label, "Header dates differ from filename; filename date retained", canonical))
                else:
                    event["clear_issues"].append("date:" + key)
        event["source"] = {"path": str(source), "signature": fingerprint, "key": key, "outcome": event["outcome"],
                            "against": (event.get("session") or previous)["archive_sha256"], "version": VERSION}
    except Exception as e:
        interrupted = isinstance(e, InterruptedError)
        event.update(outcome="interrupted" if interrupted else "failed", note=str(e))
        issue_key = "prepare:" + str(active_source)
        event["clear_issues"].append(issue_key)
        event["issues"] = [problem(issue_key, "prepare", sid, label, str(e), active_source)]
        event["source"] = {"path": str(active_source), "signature": fingerprint, "key": key, "outcome": "interrupted" if interrupted else "failed",
                            "against": previous.get("archive_sha256", "") if previous else "", "version": VERSION}
        if not previous:
            event["session"] = {"subject_id": sid, "date": date, "label": label, "status": "interrupted" if interrupted else "failed", "version": VERSION}
    finally:
        remove_tree(stage, cfg.work)
    return event


def plan_prepare(cfg, state, ids, pairs, logger):
    tasks, counts = {}, {"cached": 0, "held": 0}
    for source, legacy in discover(cfg):
        issue_key = "prepare:" + str(source)
        sid = label = ""
        try:
            if source.is_symlink():
                raise ValueError("Symbolic-link ZIPs are not accepted")
            sid, date, label = parse_name(source.name, legacy)
            if sid not in ids or ((sid, date) not in pairs and not cfg.allow_unlisted_dates):
                raise ValueError("Subject/date absent from reference CSV; update reference or use --allow-unlisted-dates for known IDs")
            key = f"{sid}/{label}"
            fingerprint = signature(source)
            prior = state.data["sessions"].get(key, {})
            cached = state.data["sources"].get(str(source), {})
            unchanged = cached.get("signature") == fingerprint and cached.get("version") == VERSION
            valid = prior.get("status") == "ready" and prior.get("extraction", {}).get("root") == str(cfg.dicoms / "Subjects" / sid / label) and fast_valid(prior["extraction"], "extract", cfg.audit)
            if not cfg.force_reextract and unchanged:
                if valid and str(source) == prior.get("archive") and fingerprint == prior.get("archive_signature"):
                    counts["cached"] += 1
                    continue
                if cached.get("against", "") == prior.get("archive_sha256", "") and cached.get("outcome") in ("duplicate", "failed"):
                    if (cached["outcome"] == "duplicate" and valid) or (cached["outcome"] == "failed" and not cfg.retry_failed):
                        counts["cached"] += 1
                        continue
            tasks.setdefault(sid, []).append((source, sid, date, label, fingerprint))
        except (OSError, ValueError) as e:
            counts["held"] += 1
            logger.warning("Held %s: %s", source, e)
            if not cfg.dry_run:
                state.checkpoint({"issues": [problem(issue_key, "prepare", sid, label, str(e), source)]})
    # Removed/renamed incoming conflicts must not remain permanently unresolved.
    if not cfg.dry_run:
        stale = [k for k, v in state.data["issues"].items() if k.startswith("prepare:") and v.get("path") and not Path(v["path"]).exists()]
        if stale:
            state.checkpoint({"clear_issues": stale})
    return tasks, counts


def sanitized_nifti(source, destination):
    """Validate the full stream, clear text/extensions, and gzip once.

    dcm2niix writes uncompressed staging files. Geometric/numeric header fields
    and pixel bytes are preserved; public output has no copied free-text header.
    """
    opener = gzip.open if str(source).endswith(".gz") else open
    with opener(source, "rb") as src:
        first = src.read(4)
        if len(first) != 4:
            raise ValueError("Truncated NIfTI header")
        endian = "<" if struct.unpack("<i", first)[0] in (348, 540) else ">"
        size = struct.unpack(endian + "i", first)[0]
        if size not in (348, 540):
            raise ValueError("Invalid NIfTI header size")
        header = bytearray(first + src.read(size - 4))
        if len(header) != size:
            raise ValueError("Truncated NIfTI header")
        if size == 348:
            if header[344:348] != b"n+1\0":
                raise ValueError("Expected single-file NIfTI-1")
            dims = struct.unpack_from(endian + "8h", header, 40)
            bitpix = struct.unpack_from(endian + "h", header, 72)[0]
            raw_offset = struct.unpack_from(endian + "f", header, 108)[0]
            if not math.isfinite(raw_offset) or raw_offset != int(raw_offset):
                raise ValueError("Invalid NIfTI voxel offset")
            offset = int(raw_offset)
            for a, b in [(4, 32), (148, 252), (328, 344)]:
                header[a:b] = b"\0" * (b-a)
        else:
            if header[4:12] != b"n+2\0\r\n\x1a\n":
                raise ValueError("Expected single-file NIfTI-2")
            dims = struct.unpack_from(endian + "8q", header, 16)
            bitpix = struct.unpack_from(endian + "h", header, 14)[0]
            offset = struct.unpack_from(endian + "q", header, 168)[0]
            for a, b in [(240, 344), (508, 524)]:
                header[a:b] = b"\0" * (b-a)
        if not 1 <= dims[0] <= 7 or any(d <= 0 for d in dims[1:dims[0]+1]) or bitpix <= 0:
            raise ValueError("Invalid NIfTI dimensions/bit depth")
        if not size + 4 <= offset <= 16 * 1024 * 1024:
            raise ValueError("Invalid or unusually large NIfTI extension area")
        padding = src.read(offset-size)
        if len(padding) != offset-size:
            raise ValueError("Truncated NIfTI extension area")
        required = (math.prod(dims[1:dims[0]+1]) * bitpix + 7) // 8
        count = 0
        with destination.open("wb") as raw_out:
            with gzip.GzipFile(filename="", fileobj=raw_out, mode="wb", mtime=0, compresslevel=6) as out:
                out.write(header)
                out.write(b"\0" * len(padding))
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    count += len(block)
                    out.write(block)
        if count < required:
            destination.unlink(missing_ok=True)
            raise ValueError("Truncated NIfTI pixel data")


def clean_metadata(metadata):
    """Keep technical fields, never patient identifiers, dates or free text."""
    result = {}
    for key in TECH_FIELDS:
        value = metadata.get(key)
        if value is None:
            continue
        # No nested dictionaries/unknown metadata containers are propagated.
        if isinstance(value, (str, int, float, bool)) or (isinstance(value, list) and all(isinstance(x, (str, int, float, bool)) for x in value)):
            try:
                json.dumps(value, allow_nan=False)
                result[key] = value
            except ValueError:
                pass
    return result


def organize_conversion(raw, output, sid, label, archive_digest):
    output.mkdir()
    files, errors, handled, volumes = [], [], set(), 0
    for nii in sorted(p for p in raw.iterdir() if p.name.endswith((".nii", ".nii.gz"))):
        stem = nii.name[:-7] if nii.name.endswith(".nii.gz") else nii.stem
        meta_path = raw / (stem + ".json")
        metadata = {}
        try:
            metadata = read_json(meta_path)
            if not isinstance(metadata, dict):
                raise ValueError("sidecar is not a JSON object")
        except (OSError, ValueError) as e:
            errors.append("Missing/invalid JSON sidecar for volume " + hashlib.sha256(stem.encode()).hexdigest()[:12])
        scan = classify(metadata.get("SeriesDescription"), metadata.get("ProtocolName"), metadata.get("ImageType"))
        directory = output / scan
        directory.mkdir(exist_ok=True)
        token = hashlib.sha256(stem.encode()).hexdigest()[:16]
        base = f"{sid}_{label}_{scan}_{token}"
        dest = directory / (base + ".nii.gz")
        image_valid = False
        try:
            if dest.exists():
                raise ValueError("Colliding converter output name; original retained")
            sanitized_nifti(nii, dest)
            image_valid = True
            enriched = clean_metadata(metadata)
            enriched.update(subject_id=sid, session=label, session_date=label[:8], scan_type=scan,
                            source_archive_sha256=archive_digest,
                            source_series_key=hashlib.sha256(stem.split("_", 2)[1].encode()).hexdigest()[:20] if stem.startswith("series_") and "_" in stem[7:] else token)
            atomic_json(directory / (base + ".json"), enriched)
            handled.update([nii.name, meta_path.name])
            volumes += 1
            for suffix in (".bval", ".bvec"):
                companion = raw / (stem + suffix)
                if companion.exists():
                    # Diffusion companions must contain only finite numbers.
                    values = companion.read_text().split()
                    if not values or not all(math.isfinite(float(x)) for x in values):
                        raise ValueError("Invalid diffusion companion values")
                    shutil.copyfile(companion, directory / (base + suffix))
                    handled.add(companion.name)
        except Exception as e:
            if not image_valid and dest.exists() and nii.name not in handled:
                # Never publish a failed/truncated gzip as a usable volume.
                # A collision belongs to the prior valid volume, so preserve it.
                if "Colliding converter output" not in str(e):
                    dest.unlink()
            errors.append(f"Volume {token}: {type(e).__name__}: {e}")
    unhandled = [p.relative_to(raw).as_posix() for p in raw.rglob("*") if p.is_file() and p.relative_to(raw).as_posix() not in handled]
    if unhandled:
        errors.append(f"{len(unhandled)} unresolved converter artifacts retained in working directory")
    for p in sorted(output.rglob("*")):
        if p.is_file():
            files.append({"name": p.relative_to(output).as_posix(), "size": p.stat().st_size, "sha256": sha256(p)})
    return volumes, files, errors


def redact_diagnostic(text):
    lines = []
    for line in text.splitlines():
        if re.search(r"patient.?name|patient.?id|accession|birth.?date|physician|institution|station.?name|serial.?number", line, re.I):
            line = "[identifying metadata diagnostic omitted]"
        lines.append(line[:2000])
    return "\n".join(lines)


def convert_one(entry, cfg, stop):
    sid, label = entry["subject_id"], entry["label"]
    key, issue_key = f"{sid}/{label}", "convert:" + f"{sid}/{label}"
    updated = copy.deepcopy(entry)
    event = {"key": key, "session": updated, "clear_issues": [issue_key], "issues": [], "outcome": "converted"}
    stage = Path(tempfile.mkdtemp(prefix="convert_", dir=cfg.work))
    raw = stage / "raw"
    raw.mkdir()
    preserve = False
    try:
        if not fast_valid(entry["extraction"], "extract", cfg.audit):
            raise ValueError("Extracted input changed or is missing; run preparation first")
        needed = max(entry.get("pixel_bytes", 0) * 2, entry.get("dicom_bytes", 0) * 2)
        if shutil.disk_usage(stage).free < needed:
            raise ValueError("Insufficient free staging space for this session")
        binary = cfg.dcm2niix_bin
        command = ([sys.executable, binary] if binary.endswith(".py") else [binary]) + [
            "-z", "n", "-b", "y", "-ba", "y", "-c", "", "-d", "9", "-f", "series_%j_%s_%e",
            "-o", str(raw), str(Path(entry["extraction"]["root"]) / "dicom")]
        env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
        started = time.monotonic()
        reason = ""
        with (stage / "converter.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
            try:
                while process.poll() is None:
                    if stop.wait(0.25) or time.monotonic() - started > cfg.timeout:
                        reason = "Interrupted" if stop.is_set() else "Converter timed out"
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        break
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        event["detail"] = redact_diagnostic((stage / "converter.log").read_text(encoding="utf-8", errors="replace"))
        (stage / "converter.log").write_text(event["detail"], encoding="utf-8")
        if process.returncode != 0:
            reason = reason or f"dcm2niix exited with code {process.returncode}"
        n_volumes, file_list, errors = organize_conversion(raw, stage / "organized", sid, label, entry["archive_sha256"])
        if not n_volumes:
            errors.append("No valid NIfTI volumes produced")
        if reason:
            errors.insert(0, reason)
        complete = not errors
        organized = stage / "organized"
        atomic_json(organized / ".inventory.json", {"files": file_list})
        atomic_json(organized / ".pipeline_complete.json", {"version": VERSION, "complete": complete,
            "source_sha256": entry["content_sha256"], "receipt_id": uuid.uuid4().hex})
        destination = cfg.niftis / "Subjects" / sid / label if complete else cfg.niftis / ".pending" / sid / label / stage.name
        if complete and destination.exists() and not (destination / ".pipeline_complete.json").exists():
            raise ValueError("NIfTI destination is not owned by this pipeline; refusing replacement")
        publish_tree(organized, destination, cfg.niftis)
        conversion = receipt_info(destination, destination / ".pipeline_complete.json", destination / ".inventory.json")
        conversion.update(status="converted" if complete else "interrupted" if stop.is_set() else "partial", output=str(destination), n_volumes=n_volumes,
                          source_sha256=entry["content_sha256"])
        updated["conversion"] = conversion
        if errors:
            preserve = True
            event.update(outcome="interrupted" if stop.is_set() else "failed", note="; ".join(errors) + f"; working files retained: {stage}")
            event["issues"] = [problem(issue_key, "convert", sid, label, event["note"], destination)]
        else:
            event["note"] = f"{n_volumes} volumes; all image/JSON/diffusion companions published"
    except Exception as e:
        preserve = True
        event.update(outcome="interrupted" if stop.is_set() else "failed", note=f"{type(e).__name__}: {e}; working files retained: {stage}")
        updated["conversion"] = {"status": "interrupted" if stop.is_set() else "failed", "source_sha256": entry["content_sha256"], "output": str(stage)}
        event["issues"] = [problem(issue_key, "convert", sid, label, event["note"], stage)]
    finally:
        if not preserve:
            remove_tree(stage, cfg.work)
    return event


def plan_convert(cfg, state, ids, pairs, logger):
    tasks, counts = {}, {"cached": 0, "held": 0}
    blocked = {(i.get("subject"), i.get("session")) for k, i in state.data["issues"].items()
               if i.get("stage") == "prepare" and not k.startswith("date:")}
    for key, entry in sorted(state.data["sessions"].items()):
        if entry.get("status") != "ready":
            continue
        sid, label, date = entry["subject_id"], entry["label"], entry["date"]
        reason = ""
        if sid not in ids or ((sid, date) not in pairs and not cfg.allow_unlisted_dates):
            reason = "Subject/date absent from current reference"
        elif entry.get("extraction", {}).get("root") != str(cfg.dicoms / "Subjects" / sid / label):
            reason = "Prepared cache belongs to a different DICOM root; run preparation with the intended --output-root"
        elif (sid, label) in blocked:
            reason = "Unresolved preparation problem; see issues.csv"
        elif not fast_valid(entry["extraction"], "extract", cfg.audit):
            reason = "Extracted input changed or missing; run preparation"
        if reason:
            counts["held"] += 1
            logger.warning("Held %s: %s", key, reason)
            if not cfg.dry_run:
                state.checkpoint({"issues": [problem("convert:" + key, "convert", sid, label, reason)]})
            continue
        conv = entry.get("conversion", {})
        same = conv.get("source_sha256") == entry["content_sha256"]
        if not cfg.force_reconvert and same:
            if conv.get("status") == "converted" and conv.get("root") == str(cfg.niftis / "Subjects" / sid / label) and fast_valid(conv, "convert", cfg.audit):
                counts["cached"] += 1
                if not cfg.dry_run and "convert:" + key in state.data["issues"]:
                    state.checkpoint({"clear_issues": ["convert:" + key]})
                continue
            if conv.get("status") in ("failed", "partial") and not cfg.retry_failed:
                counts["cached"] += 1
                continue
        tasks.setdefault(sid, []).append(copy.deepcopy(entry))
    return tasks, counts


def process_subject(items, cfg, phase, session_state, events, stop):
    local_sessions = session_state
    for item in items:
        if stop.is_set():
            break
        if phase == "prepare":
            key = f"{item[1]}/{item[3]}"
            event = prepare_one(item, local_sessions.get(key), cfg, stop)
            if event.get("session"):
                local_sessions[key] = event["session"]
        else:
            event = convert_one(item, cfg, stop)
        events.put(event)


def main(phase, argv=None):
    args = config_parser(phase).parse_args(argv)
    try:
        cfg = configure(args, phase)
        ids, pairs, roster_warnings = load_roster(cfg.reference, cfg.subject_column, cfg.date_column)
        # One lock shared by both commands, even if a custom report root is used.
        if cfg.dry_run:
            return run(cfg, phase, ids, pairs, roster_warnings)
        with ProjectLock(cfg.project_root / ".mri_dicom_pipeline.lock"):
            return run(cfg, phase, ids, pairs, roster_warnings)
    except (ValueError, RuntimeError, OSError) as e:
        logger = logging.getLogger("mri_dicom_pipeline")
        if logger.handlers:
            logger.error("RUN ABORTED: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    finally:
        logger = logging.getLogger("mri_dicom_pipeline")
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()


def run(cfg, phase, ids, pairs, roster_warnings):
    logger = setup_log(cfg.reports, phase, cfg.dry_run)
    started = time.monotonic()
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    logger.info("RUN %s START stage=%s workers=%s dry_run=%s", run_id, phase, cfg.workers, cfg.dry_run)
    if not cfg.dry_run:
        logger.info("Main log: %s | Details: %s", cfg.reports / "pipeline.log", cfg.reports / f"latest_{phase}.log")
        logger.info("Current status: %s | Problems: %s", cfg.reports / "session_status.csv", cfg.reports / "issues.csv")
    for warning in roster_warnings:
        logger.warning(warning)
    state = State(cfg.reports)
    if phase == "prepare":
        if not cfg.import_legacy and (cfg.uploads / "Subjects").is_dir() and not state.data["sessions"]:
            logger.warning("Legacy DicomUploads/Subjects is not scanned by default. Preview migration with --import-legacy --dry-run.")
        tasks, counts = plan_prepare(cfg, state, ids, pairs, logger)
    else:
        tasks, counts = plan_convert(cfg, state, ids, pairs, logger)
    if cfg.limit:
        remaining, limited = cfg.limit, {}
        for sid, items in tasks.items():
            if remaining:
                limited[sid] = items[:remaining]
                remaining -= len(limited[sid])
        tasks = limited
    pending = sum(len(items) for items in tasks.values())
    logger.info("Plan: %s work items across %s subjects; %s cached; %s held. Cached checks did not read old DICOM headers.", pending, len(tasks), counts["cached"], counts["held"])
    if cfg.dry_run:
        for sid, items in tasks.items():
            for item in items:
                logger.info("WOULD %s %s/%s", phase, sid, item[3] if phase == "prepare" else item["label"])
        return 2 if counts["held"] else 0
    if phase == "convert" and pending and not (Path(cfg.dcm2niix_bin).is_file() or shutil.which(cfg.dcm2niix_bin)):
        raise ValueError("dcm2niix not found; load its module/environment or pass --dcm2niix-bin")
    if phase == "prepare" and pending:
        try:
            import pydicom
        except ImportError as e:
            raise ValueError("pydicom is missing; install mri-dicom-pipeline before preparation") from e
    cfg.work.mkdir(parents=True, exist_ok=True)
    events, stop = queue.Queue(), threading.Event()
    counts.update(completed=0, duplicate=0, failed=0)
    last_progress, last_report = time.monotonic(), time.monotonic()

    def accept(event):
        state.checkpoint(event)
        outcome = event["outcome"]
        counts["duplicate" if outcome == "duplicate" else "failed" if outcome in ("failed", "interrupted") else "completed"] += 1
        logger.log(logging.ERROR if outcome in ("failed", "interrupted") else logging.INFO, "%s %s: %s", outcome.upper(), event["key"], event.get("note", "identical archive/content; skipped"))
        if event.get("detail"):
            # Converter verbosity belongs only in the latest detailed log.
            for handler in logger.handlers:
                if isinstance(handler, logging.FileHandler) and not isinstance(handler, RotatingFileHandler):
                    record = logger.makeRecord(logger.name, logging.INFO, "", 0, "%s", (event["detail"],), None)
                    handler.handle(record)

    pool = futures.ThreadPoolExecutor(max_workers=cfg.workers)
    jobs = [pool.submit(process_subject, items, cfg, phase, copy.deepcopy({k: v for k, v in state.data["sessions"].items() if k.startswith(sid + "/")}), events, stop) for sid, items in tasks.items()]
    interrupted = False
    try:
        while any(not job.done() for job in jobs) or not events.empty():
            try:
                accept(events.get(timeout=0.5))
            except queue.Empty:
                pass
            if time.monotonic() - last_progress >= 30:
                logger.info("Progress: %s/%s work items finished; %ss elapsed", counts["completed"] + counts["duplicate"] + counts["failed"], pending, int(time.monotonic()-started))
                last_progress = time.monotonic()
            if time.monotonic() - last_report >= 15:
                reports(state, cfg, pairs)
                last_report = time.monotonic()
        for job in jobs:
            job.result()
    except KeyboardInterrupt:
        interrupted = True
        logger.warning("Stopping workers; completed sessions remain checkpointed")
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        while not events.empty():
            accept(events.get_nowait())
        state.flush()
        reports(state, cfg, pairs)
    unresolved = sum(not k.startswith("date:") for k in state.data["issues"])
    outcome = "interrupted" if interrupted else "issues" if unresolved or counts["failed"] or counts["held"] else "ok"
    elapsed = round(time.monotonic()-started, 2)
    history = cfg.reports / "run_history.csv"
    fields = ["run_id", "stage", "outcome", "seconds", "completed", "cached", "duplicate", "failed", "held", "unresolved"]
    with history.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if f.tell() == 0:
            writer.writeheader()
        writer.writerow(dict(run_id=run_id, stage=phase, outcome=outcome, seconds=elapsed, unresolved=unresolved, **counts))
    logger.info("RUN %s END outcome=%s completed=%s cached=%s duplicates=%s failed=%s held=%s unresolved=%s elapsed=%ss", run_id, outcome, counts["completed"], counts["cached"], counts["duplicate"], counts["failed"], counts["held"], unresolved, elapsed)
    logger.info("Read %s first; unresolved problems: %s", cfg.reports / "pipeline.log", cfg.reports / "issues.csv")
    return 130 if interrupted else 2 if outcome != "ok" else 0
