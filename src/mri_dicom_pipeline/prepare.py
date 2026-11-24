"""Preparation: discover, hash, extract and publish session ZIPs."""

from __future__ import annotations

from datetime import datetime
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid
import zipfile

from . import classify, config, fsutil, identity, receipts, recovery, reports


SKIP_EXT = set(".jpg .jpeg .png .gif .bmp .tif .tiff .pdf .txt .xml .html .htm .csv .json .ini .log .dll .exe .ocx .config .manifest .css .js .ico .onnx .lic .profiles .mapping .bin .rlv .rlv_meta .rbv .rbv_meta .local .zip".split())


DICOM_TAGS = ["SOPClassUID", "SOPInstanceUID", "StudyInstanceUID", "SeriesInstanceUID",
    "SeriesNumber", "SeriesDescription", "ProtocolName", "Modality", "Rows", "Columns",
    "BitsAllocated", "SamplesPerPixel", "NumberOfFrames", "InstanceNumber", "StudyDate", "ImageType"]

DICOMDIR = "1.2.840.10008.1.3.10"  # media directory; always omitted

# Series with at least one object of these classes must convert (Q6, Q19).
MR_IMAGE_SOP_CLASSES = {
    "1.2.840.10008.5.1.4.1.1.4",    # MR Image Storage
    "1.2.840.10008.5.1.4.1.1.4.1",  # Enhanced MR Image Storage
    "1.2.840.10008.5.1.4.1.1.4.3",  # Enhanced MR Color Image Storage
    "1.2.840.10008.5.1.4.1.1.4.4",  # Legacy Converted Enhanced MR Image Storage
}


# A StudyDate this many days or more from the ZIP-name date is held rather than
# only reported; a smaller difference is a date: warning. Set by the user.
DEFAULT_DATE_TOLERANCE = 2  # days; --date-tolerance


def days_apart(header_dates, date):
    """Largest gap in days between the ZIP-name date and a header StudyDate; StudyDates that are not valid YYYYMMDD are skipped."""
    named, gaps = datetime.strptime(date, "%Y%m%d"), [0]
    for value in header_dates:
        if len(value) != 8 or not value.isdigit():
            continue  # strptime would read 202611 as 1 January 2026
        try:
            gaps.append(abs((datetime.strptime(value, "%Y%m%d") - named).days))
        except ValueError:
            pass
    return max(gaps)


UNLISTED = ("Subject/date absent from reference CSV; update reference or use --allow-unlisted-dates "
            "for known IDs")
AUDIT_HOLD = "Accepted canonical ZIP changed (found by --audit); the prepared session and its outputs are unchanged"


class HoldError(ValueError):
    """The ZIP is held for review: nothing is published and no session entry changes."""


class ZipChanged(HoldError):
    """An accepted canonical ZIP's bytes changed (Q3). Its issue clears only once the bytes match again."""


def hold_issue(issue_key, sid, label, error, path):
    """The prepare: issue for an error; a changed-ZIP hold is marked so that plain runs check it by hash."""
    issue = reports.problem(issue_key, "prepare", sid, label, str(error), path)
    if isinstance(error, ZipChanged):
        issue["zip_changed"] = True
    return issue


def discover(cfg):
    """ZIP paths under SubjectsSorted (accepted, canonical) and Uploads (incoming)."""
    result = []
    for root in (cfg.uploads / "SubjectsSorted", cfg.uploads / "Uploads"):
        for folder, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (Path(folder) / d).is_symlink())
            for name in sorted(files):
                if name.lower().endswith(".zip"):
                    result.append(Path(folder) / name)
    return result


def check_free_space(source, stage_root, dicom_root):
    """Refuse to extract a ZIP that would not fit with a reserve to spare (Q15).

    The reserve is the smaller of 5% of the filesystem or 50 GiB. The DICOM
    root is checked too when it is on a different filesystem.
    """
    with zipfile.ZipFile(source) as archive:
        needed = sum(member.file_size for member in archive.infolist() if not member.is_dir())
    roots = [Path(stage_root)]
    if os.stat(dicom_root).st_dev != os.stat(stage_root).st_dev:
        roots.append(Path(dicom_root))
    for root in roots:
        usage = shutil.disk_usage(root)
        reserve = min(usage.total * 5 // 100, 50 * 1024 ** 3)
        if needed + reserve > usage.free:
            raise ValueError(f"Not enough free space on {root}: the ZIP needs {needed} bytes plus a reserve of "
                             f"{reserve} bytes; {usage.free} bytes are free")


def extract_archive(source, stage, stop=None, accept_multi_study=False):
    """Stream only DICOM objects, including extensionless objects; verify CRCs.

    Original member paths are never used as destination paths (ZIP traversal
    and duplicate-member overwrites cannot escape the staging directory).
    Byte-identical repeats of an object are stored once; a repeat with other
    bytes, or more than one study without acceptance, holds the ZIP (Q20).
    """
    import pydicom
    dicom_dir = stage / "dicom"
    dicom_dir.mkdir(parents=True)
    files, series, study_keys, dates, seen = [], {}, set(), set(), {}
    skipped, pixel_bytes, repeated = 0, 0, 0
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        for number, member in enumerate(members, 1):
            if stop is not None and stop.is_set():
                raise InterruptedError("Preparation interrupted")
            if member.is_dir():
                continue
            parts = member.filename.replace("\\", "/").split("/")
            if "ddv" in [p.lower() for p in parts] or Path(member.filename).suffix.lower() in SKIP_EXT:
                skipped += 1
                continue
            # Where a refused object is, without its name: member paths can carry patient names (P8).
            where = f"member {number:,} of {len(members):,}, {member.file_size:,} bytes"
            with archive.open(member) as f:
                head = f.read(132)
                magic = head[128:132] == b"DICM"
                named = Path(member.filename).suffix.lower() == ".dcm"
                if not magic and head[:2] not in (b"\x02\x00", b"\x08\x00", b"\x00\x02", b"\x00\x08"):
                    # Without the mark, a DICOM object starts with group 0002 or 0008 (either byte
                    # order). Parsing other bytes is pointless, and slow for zeros (SB-22).
                    if named:
                        raise ValueError(f"Unreadable DICOM header in archive ({where})")
                    skipped += 1
                    continue
                f.seek(0)
                try:
                    ds = pydicom.dcmread(f, force=not magic, stop_before_pixels=True, specific_tags=DICOM_TAGS)
                except Exception as e:
                    if magic or named:
                        raise ValueError(f"Unreadable DICOM header in archive ({where})") from e
                    skipped += 1
                    continue
                meta = getattr(ds, "file_meta", None) or {}
                media_class = str(meta.get("MediaStorageSOPClassUID", ""))
                if DICOMDIR in (str(ds.get("SOPClassUID", "")), media_class):
                    skipped += 1
                    continue
                if not ds.get("SOPClassUID"):
                    # Left out like other non-image files, unless it looks like an object cut off inside
                    # its header: a DICM file without a complete file meta group, or an MR image class in
                    # the file meta with no SOP Class UID in the data set (SB-18, SB-26).
                    if magic and (not media_class or not meta.get("TransferSyntaxUID")
                                  or media_class in MR_IMAGE_SOP_CLASSES):
                        raise ValueError(f"Unreadable DICOM header in archive ({where})")
                    skipped += 1
                    continue
                if not (ds.get("SeriesInstanceUID") or ds.get("Modality")):
                    skipped += 1
                    continue
                if str(ds.SOPClassUID) in MR_IMAGE_SOP_CLASSES and not (ds.get("Rows") and ds.get("Columns")):
                    raise ValueError(f"Unreadable DICOM header in archive: an image object without Rows or "
                                     f"Columns ({where})")
                sop_uid = str(ds.get("SOPInstanceUID", ""))
                if not sop_uid:
                    raise ValueError(f"DICOM object missing SOPInstanceUID ({where})")
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
                fingerprint = (digest.hexdigest(), size)
                if sop_uid in seen:
                    if seen[sop_uid] != fingerprint:
                        raise HoldError("Two objects share a SOP Instance UID but have different bytes; "
                                        "split the ZIP or upload a corrected export as a new _N session")
                    # Names come from the inventory length, so numbering stays continuous.
                    dest.unlink()
                    repeated += 1
                    continue
                seen[sop_uid] = fingerprint
                files.append({"name": "dicom/" + filename, "size": size, "sha256": fingerprint[0]})
                try:
                    expected_pixels = int(ds.get("Rows", 0)) * int(ds.get("Columns", 0)) * int(ds.get("SamplesPerPixel", 1)) * int(ds.get("BitsAllocated", 16)) // 8 * int(ds.get("NumberOfFrames", 1))
                    syntax = getattr(ds.file_meta, "TransferSyntaxUID", None)
                    if syntax and not syntax.is_compressed and not syntax.is_deflated and size < expected_pixels:
                        raise ValueError(f"Uncompressed DICOM is smaller than its declared pixel dimensions ({where})")
                except (AttributeError, TypeError):
                    expected_pixels = 0
                uid = str(ds.get("SeriesInstanceUID", ""))
                if not uid:
                    raise ValueError(f"DICOM object missing SeriesInstanceUID ({where})")
                key = hashlib.sha256(uid.encode()).hexdigest()[:20]
                row = series.setdefault(key, {"series_key": key, "series_number": str(ds.get("SeriesNumber", "")),
                    "scan_type": classify.classify(str(ds.get("SeriesDescription", "")), str(ds.get("ProtocolName", "")),
                                                   ds.get("ImageType")),
                    "instances": 0, "sop_classes": set()})
                row["instances"] += 1
                row["sop_classes"].add(str(ds.SOPClassUID))
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
    if len(study_keys) > 1 and not accept_multi_study:
        raise HoldError(f"The ZIP holds {len(study_keys)} studies; split it into separate _N sessions, "
                        "or accept it with --accept-multi-study SUBJECT/SESSION")
    for row in series.values():
        row["sop_classes"] = sorted(row["sop_classes"])
        row["required"] = any(sop_class in MR_IMAGE_SOP_CLASSES for sop_class in row["sop_classes"])
    content = hashlib.sha256("\n".join(sorted(f"{r['sha256']}:{r['size']}" for r in files)).encode()).hexdigest()
    return {"files": files, "series": list(series.values()), "n_dicom": len(files), "n_series": len(series),
            "content_sha256": content, "dicom_bytes": sum(x["size"] for x in files),
            "pixel_bytes": pixel_bytes, "skipped_files": skipped, "study_count": len(study_keys),
            "study_keys": sorted(study_keys), "header_dates": sorted(dates), "repeated_identical": repeated}


def move_archive(source, destination, cfg, fingerprint):
    """Move an accepted ZIP to its canonical path; return whether it moved (False when already there)."""
    if source == destination:
        return False
    fsutil.owned(source, cfg.uploads)
    fsutil.owned(destination, cfg.uploads / "SubjectsSorted")
    if fsutil.signature(source) != fingerprint:
        raise ValueError("Upload changed while being processed; retry after transfer completes")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError("Canonical ZIP already exists; refusing to overwrite it")
    # Both locations are inside DicomUploads. Refuse cross-device moves rather
    # than silently copying/deleting sources on a differently mounted filesystem.
    fsutil.retry_locked(lambda: source.rename(destination))
    return True


def _plain_folder(path):
    """A real folder: not a link, and on Windows not a junction or another reparse point."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT


def _remove_empty(folder):
    """Delete the empty folders in folder, deepest first, then folder itself if it is left empty; return the count."""
    if not _plain_folder(folder):
        return 0
    try:
        with os.scandir(folder) as entries:
            names = [entry.path for entry in entries if not entry.name.startswith(".")]
    except OSError:
        return 0
    removed = sum(_remove_empty(name) for name in names)  # files and links count 0
    try:
        # rmdir deletes only an empty folder: anything that arrived meanwhile keeps it.
        fsutil.retry_locked(lambda: os.rmdir(folder))
    except OSError:
        return removed  # not empty, held open, or already gone
    return removed + 1


def remove_emptied_folders(cfg, moved, logger):
    """Delete the folders in Uploads that moving ZIPs out of them left empty (owner request, 2026-11-24).

    moved: the Uploads paths this run moved ZIPs from. In each top-level Uploads folder they came from,
    every empty folder is deleted, deepest first, then the top-level folder itself if nothing is left.
    A folder holding any file (a held ZIP, desktop.ini, notes) stays, as do hidden folders, links and
    Uploads itself.
    """
    uploads, tops = cfg.uploads / "Uploads", set()
    for path in map(Path, moved):
        parts = path.relative_to(uploads).parts if path.is_relative_to(uploads) else ()
        if len(parts) > 1:  # a ZIP directly in Uploads leaves no folder behind
            tops.add(uploads / parts[0])
    for top in sorted(tops):
        removed = _remove_empty(top)
        if removed:
            logger.info("Removed %s empty folder%s left in %s after its ZIPs moved to SubjectsSorted",
                        removed, "" if removed == 1 else "s", top)


def prepare_one(candidate, previous, cfg, stop=None):
    source, sid, date, label, fingerprint = candidate
    key, issue_key = f"{sid}/{label}", "prepare:" + str(source)
    active_source = source
    stage = Path(tempfile.mkdtemp(prefix="prepare_", dir=cfg.work))
    event = {"key": key, "clear_issues": [issue_key], "issues": [], "outcome": "prepared"}
    try:
        digest = fsutil.sha256(source, stop)
        if fsutil.signature(source) != fingerprint:
            raise ValueError("Upload changed during hashing; wait for transfer completion")
        destination = cfg.dicoms / "Subjects" / sid / label
        canonical = cfg.uploads / "SubjectsSorted" / sid / f"{sid}_{label}.zip"
        if canonical == source:
            # On Windows the same file can be spelled with other letter cases (S001_20260725.ZIP, or a
            # folder s001). Record the spelling discovery lists, so later runs find the ZIP cached.
            canonical = source
        valid_previous = (previous and previous.get("status") == "ready"
                          and previous.get("extraction", {}).get("root") == str(destination)
                          and receipts.fast_valid(previous["extraction"], "extract", cfg.audit))
        # As paths: on Windows a canonical ZIP renamed in letter case is still the same file.
        is_canonical = bool(previous) and bool(previous.get("archive")) and source == Path(previous["archive"])
        found, recorded = receipts.receipt_id(destination), (previous or {}).get("extraction", {}).get("receipt_id")
        if is_canonical and digest != previous.get("archive_sha256"):
            # Q3: an accepted canonical ZIP is immutable.
            raise ZipChanged("Accepted canonical ZIP changed; the prepared session and its outputs are unchanged. "
                            "Restore the original ZIP, or upload the new export as a new _N session")
        if is_canonical and valid_previous and not cfg.force_reextract:
            # The same bytes with a new size or mtime (for example restored from a backup).
            event.update(outcome="refresh", session=dict(previous, archive=str(source), archive_signature=fingerprint),
                         note="canonical ZIP unchanged; its new file signature is recorded")
        elif found is not None and found != recorded and not cfg.force_reextract:
            # A published extraction that state does not record (Q12 adoption rule, D6).
            if not receipts.adoptable(destination, "extract", digest):
                raise HoldError("The extraction on disk does not match this ZIP and was left untouched")
            if move_archive(source, canonical, cfg, fingerprint):
                event["moved_from"] = str(source)  # its emptied folders are deleted after the run
            active_source = canonical
            inventory = fsutil.read_json(destination / receipts.INVENTORY)
            event.update(outcome="adopted", note="published extraction adopted after checking every listed file",
                         session=receipts.prepared_entry(sid, date, label, canonical, digest, inventory, destination,
                                                         previous.get("conversion", {}) if previous else {}))
            event["sources"] = [{"path": str(canonical), "signature": fsutil.signature(canonical), "key": key,
                                 "outcome": "prepared", "against": digest, "version": config.SCHEMA_VERSION}]
        elif valid_previous and previous["archive_sha256"] == digest and not (cfg.force_reextract and is_canonical):
            event["outcome"] = "duplicate"
        else:
            check_free_space(source, cfg.work, cfg.dicoms)
            accept = key in cfg.accept_multi_study or bool((previous or {}).get("accepted_multi_study"))
            try:
                inventory = extract_archive(source, stage, stop, accept)
            except (zipfile.BadZipFile, RuntimeError) as e:
                # zipfile's messages name the member, and member paths can carry patient names (P8).
                raise ValueError(f"Damaged or unreadable ZIP ({type(e).__name__}); member names are not "
                                 "reported") from None
            if fsutil.signature(source) != fingerprint:
                raise ValueError("Upload changed during extraction; wait for transfer completion")
            if valid_previous and not is_canonical:
                if inventory["content_sha256"] != previous["content_sha256"]:
                    raise HoldError("Different DICOM contents for an existing session; ZIP retained in Uploads. "
                                    "Use an explicit _2 session or resolve the conflict")
                event["outcome"] = "duplicate"
            else:
                if destination.exists() and not (destination / receipts.RECEIPT).exists():
                    raise ValueError("Extracted destination is not owned by this pipeline; refusing replacement")
                gap = days_apart(inventory["header_dates"], date)
                accepted = key in cfg.accept_date_mismatch or (previous or {}).get("archive_sha256") == digest
                if gap > cfg.date_tolerance and not accepted:
                    raise HoldError(f"DICOM StudyDate {', '.join(inventory['header_dates'])} is {gap} days from the "
                                    f"ZIP-name date {date} for {sid} (more than the {cfg.date_tolerance}-day "
                                    "tolerance); correct the ZIP name, or accept it with "
                                    "--accept-date-mismatch SUBJECT/SESSION")
                if stop is not None and stop.is_set():
                    raise InterruptedError("Preparation interrupted")
                if move_archive(source, canonical, cfg, fingerprint):
                    event["moved_from"] = str(source)
                active_source = canonical
                receipt_id = uuid.uuid4().hex
                fsutil.atomic_json(stage / receipts.INVENTORY, inventory)
                fsutil.atomic_json(stage / receipts.RECEIPT, {"version": config.SCHEMA_VERSION, "archive_sha256": digest,
                                                             "receipt_id": receipt_id, "run_id": cfg.run_id})
                incoming, aside = fsutil.swap_names(destination)
                intent = recovery.write_intent(cfg.reports, stage="prepare", key=key, run_id=cfg.run_id,
                    stage_dir=str(stage), destination=str(destination), incoming=str(incoming),
                    previous=str(aside), ownership_root=str(cfg.dicoms), receipt_id=receipt_id,
                    expected_digest=digest, attempt=False, subject_id=sid, date=date, label=label,
                    archive=str(canonical))
                cleaned = fsutil.publish_tree(stage, destination, cfg.dicoms, incoming, aside)
                if not cleaned:
                    # Later runs only retry deleting the previous folder; they never roll it back (SB-16).
                    recovery.mark_cleanup_only(cfg.reports, intent)
                event["session"] = receipts.prepared_entry(sid, date, label, canonical, digest, inventory, destination,
                                                           previous.get("conversion", {}) if previous else {})
                # An open intent makes later runs retry deleting the previous folder.
                event["intents"] = [intent] if cleaned else []
                event["sources"] = [{"path": str(canonical), "signature": fsutil.signature(canonical), "key": key,
                    "outcome": "prepared", "against": digest, "version": config.SCHEMA_VERSION}]
                event["note"] = (f"{inventory['n_dicom']} DICOMs, {inventory['n_series']} series; "
                                 f"{inventory['skipped_files']} viewer/non-DICOM files omitted"
                                 + (f"; {inventory['repeated_identical']} repeated objects stored once"
                                    if inventory["repeated_identical"] else "")
                                 + ("" if cleaned else f"; previous folder {aside} not deleted yet, later runs retry"))
                if inventory["header_dates"] and inventory["header_dates"] != [date]:
                    event["issues"].append(reports.problem("date:" + key, "prepare", sid, label, f"Header StudyDate {', '.join(inventory['header_dates'])} differs from the ZIP-name date {date} for {sid}; ZIP-name date retained", canonical))
                else:
                    event["clear_issues"].append("date:" + key)
        event["source"] = {"path": str(source), "signature": fingerprint, "key": key, "outcome": event["outcome"],
                            "against": (event.get("session") or previous)["archive_sha256"], "version": config.SCHEMA_VERSION}
    except Exception as e:
        held, interrupted = isinstance(e, HoldError), isinstance(e, InterruptedError)
        outcome = "held" if held else "interrupted" if interrupted else "failed"
        event.update(outcome=outcome, note=str(e))
        event.pop("session", None)
        issue_key = "prepare:" + str(active_source)
        event["clear_issues"].append(issue_key)
        event["issues"] = [hold_issue(issue_key, sid, label, e, active_source)]
        event["source"] = {"path": str(active_source), "signature": fingerprint, "key": key, "outcome": outcome,
                            "against": previous.get("archive_sha256", "") if previous else "", "version": config.SCHEMA_VERSION}
        if not previous and not held:
            event["session"] = {"subject_id": sid, "date": date, "label": label, "status": outcome, "version": config.SCHEMA_VERSION}
    finally:
        fsutil.remove_tree(stage, cfg.work)
    return event


def plan_prepare(cfg, state, ids, pairs, logger, skip=frozenset(), held=None):
    """Work items per subject.

    skip: unnamed ZIPs the naming step held or (in a dry run) would rename.
    held: a list that collects (subject, path, reason) for each held ZIP, for the dry-run summary.
    """
    tasks, counts = {}, {"cached": 0, "held": 0}
    held = [] if held is None else held
    recovering = recovery.blocked_keys(state)
    discovered = discover(cfg)

    def hold(reason):
        counts["held"] += 1
        held.append((sid, source, reason))

    for source in discovered:
        if source in skip:
            continue
        issue_key = "prepare:" + str(source)
        sid = label = ""
        try:
            if source.is_symlink():
                raise ValueError("Symbolic-link ZIPs are not accepted")
            sid, date, label = identity.parse_name(source.name)
            key = f"{sid}/{label}"
            if cfg.sessions is not None and key not in cfg.sessions:
                continue
            fingerprint = fsutil.signature(source)
            prior = state.data["sessions"].get(key, {})
            cached = state.data["sources"].get(str(source), {})
            unchanged = cached.get("signature") == fingerprint and cached.get("version") == config.SCHEMA_VERSION
            valid = prior.get("status") == "ready" and prior.get("extraction", {}).get("root") == str(cfg.dicoms / "Subjects" / sid / label) and receipts.fast_valid(prior["extraction"], "extract", cfg.audit)
            if not cfg.force_reextract and unchanged:
                if valid and str(source) == prior.get("archive") and fingerprint == prior.get("archive_signature"):
                    issue = state.data["issues"].get(issue_key)
                    if cfg.audit or (issue or {}).get("zip_changed"):
                        # --audit catches an edit that kept the size and mtime (Q3), in dry runs too (P7).
                        # An open changed-ZIP hold is checked the same way until the bytes match again.
                        if fsutil.sha256(source) != prior.get("archive_sha256"):
                            if (issue or {}).get("zip_changed"):
                                hold(issue.get("reason", ""))
                                continue
                            raise ZipChanged(AUDIT_HOLD)
                    if issue and not cfg.dry_run:
                        # Left by a failed attempt that kept the valid extraction (for example a
                        # --force-reextract that failed), or a changed-ZIP hold whose bytes match again.
                        state.checkpoint({"clear_issues": [issue_key]})
                    counts["cached"] += 1
                    continue
                if cached.get("outcome") == "held" and key not in cfg.accept_multi_study | cfg.accept_date_mismatch:
                    # Not hashed again until the ZIP changes; its issue stays open.
                    hold(state.data["issues"].get(issue_key, {}).get("reason", "held at an earlier run; see issues.csv"))
                    continue
                if cached.get("against", "") == prior.get("archive_sha256", "") and cached.get("outcome") in ("duplicate", "failed"):
                    if (cached["outcome"] == "duplicate" and valid) or (cached["outcome"] == "failed" and not cfg.retry_failed):
                        counts["cached"] += 1
                        continue
            # Only ZIPs about to be worked on are checked against the roster: an issue on a cached ZIP
            # could never clear, and convert checks the roster again before converting.
            if sid not in ids or ((sid, date) not in pairs and not cfg.allow_unlisted_dates):
                raise ValueError(UNLISTED)
            if key in recovering:
                # A folder crash recovery would not touch waits for a person (Q12).
                hold(f"unresolved crash-recovery issue for {key}; see issues.csv")
                logger.warning("Held %s: unresolved crash-recovery issue for %s; see issues.csv", source, key)
                continue
            tasks.setdefault(sid, []).append((source, sid, date, label, fingerprint))
        except (OSError, ValueError) as e:
            hold(str(e))
            logger.warning("Held %s: %s", source, e)
            if not cfg.dry_run:
                state.checkpoint({"issues": [hold_issue(issue_key, sid, label, e, source)]})
    if not cfg.dry_run:
        # Removed/renamed incoming conflicts must not remain permanently unresolved.
        stale = [k for k, v in state.data["issues"].items() if k.startswith("prepare:") and v.get("path") and not Path(v["path"]).exists()]
        # R18: a prepared session whose canonical ZIP is not listed gets a nonblocking issue.
        # normcase: on Windows a canonical ZIP renamed in letter case is still listed.
        listed, missing = {os.path.normcase(path) for path in discovered}, []
        for key, entry in state.data["sessions"].items():
            issue = "missing:" + key
            if entry.get("status") != "ready" or not entry.get("archive"):
                continue
            found = os.path.normcase(entry["archive"]) in listed
            if found and issue in state.data["issues"]:
                stale.append(issue)
            elif not found and issue not in state.data["issues"]:
                missing.append(reports.problem(issue, "prepare", entry["subject_id"], entry["label"],
                                               "Canonical ZIP is missing; the extracted DICOM objects are intact",
                                               entry["archive"]))
        if stale or missing:
            state.checkpoint({"clear_issues": stale, "issues": missing})
    return tasks, counts


def dry_run_summary(tasks, counts, held, names):
    """The block that ends prepare --dry-run: what would happen, then everything a person should look at.

    held: [(subject, path, reason)]; names: [(subject, path, new name, note)] proposed by the naming step.
    """
    rule = "=" * 72
    checks = [n for n in names if n[3]]
    lines = ["", rule, "DRY RUN SUMMARY (nothing was changed)", rule, "",
             f"  {len(names):>5}  unnamed ZIPs would be named and prepared",
             f"  {sum(len(items) for items in tasks.values()):>5}  named ZIPs would be prepared",
             f"  {counts['cached']:>5}  already prepared (no work)",
             f"  {len(held):>5}  HELD: not prepared until fixed (listed below)"]
    if held:
        lines += ["", "", f"HELD ({len(held)})"]
        for sid, path, reason in sorted(held, key=lambda h: (h[0], str(h[1]))):
            lines += ["", f"  {sid or '?'}  {path}", f"        {reason}"]
    if checks:
        lines += ["", "", f"CHECK THESE NAMES ({len(checks)}: near dates, extra sessions, re-exports)"]
        for sid, path, name, note in sorted(checks, key=lambda n: (n[0], n[2])):
            lines += ["", f"  {sid}  {path}", f"        -> {name}: {note}"]
    lines += ["", "", "Run without --dry-run to name and prepare everything that is not held.", rule]
    return "\n".join(lines)
