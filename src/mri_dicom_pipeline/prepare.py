"""Preparation: discover, hash, extract and publish session ZIPs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import uuid
import zipfile

from . import classify, config, fsutil, identity, receipts, recovery, reports


SKIP_EXT = set(".jpg .jpeg .png .gif .bmp .tif .tiff .pdf .txt .xml .html .htm .csv .json .ini .log .dll .exe .ocx .config .manifest .css .js .ico .onnx .lic .profiles .mapping .bin .rlv .rlv_meta .rbv .rbv_meta .local .zip".split())


DICOM_TAGS = ["SOPClassUID", "SOPInstanceUID", "StudyInstanceUID", "SeriesInstanceUID",
    "SeriesNumber", "SeriesDescription", "ProtocolName", "Modality", "Rows", "Columns",
    "BitsAllocated", "SamplesPerPixel", "NumberOfFrames", "InstanceNumber", "StudyDate", "ImageType"]

# Series with at least one object of these classes must convert (Q6, Q19).
MR_IMAGE_SOP_CLASSES = {
    "1.2.840.10008.5.1.4.1.1.4",    # MR Image Storage
    "1.2.840.10008.5.1.4.1.1.4.1",  # Enhanced MR Image Storage
    "1.2.840.10008.5.1.4.1.1.4.3",  # Enhanced MR Color Image Storage
    "1.2.840.10008.5.1.4.1.1.4.4",  # Legacy Converted Enhanced MR Image Storage
}


class HoldError(ValueError):
    """The ZIP is held for review: nothing is published and no session entry changes."""


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
                sop_uid = str(ds.get("SOPInstanceUID", ""))
                if not sop_uid:
                    raise ValueError("DICOM missing SOPInstanceUID; held for review")
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
                        raise ValueError("Uncompressed DICOM is smaller than its declared pixel dimensions")
                except (AttributeError, TypeError):
                    expected_pixels = 0
                uid = str(ds.get("SeriesInstanceUID", ""))
                if not uid:
                    raise ValueError("DICOM missing SeriesInstanceUID; held for review")
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
            "header_dates": sorted(dates), "repeated_identical": repeated}


def move_archive(source, destination, cfg, fingerprint):
    if source == destination:
        return
    fsutil.owned(source, cfg.uploads)
    fsutil.owned(destination, cfg.uploads / "SubjectsSorted")
    if fsutil.signature(source) != fingerprint:
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
    try:
        digest = fsutil.sha256(source, stop)
        if fsutil.signature(source) != fingerprint:
            raise ValueError("Upload changed during hashing; wait for transfer completion")
        destination = cfg.dicoms / "Subjects" / sid / label
        canonical = cfg.uploads / "SubjectsSorted" / sid / f"{sid}_{label}.zip"
        valid_previous = (previous and previous.get("status") == "ready"
                          and previous.get("extraction", {}).get("root") == str(destination)
                          and receipts.fast_valid(previous["extraction"], "extract", cfg.audit))
        is_canonical = bool(previous) and str(source) == previous.get("archive")
        found, recorded = receipts.receipt_id(destination), (previous or {}).get("extraction", {}).get("receipt_id")
        if is_canonical and digest != previous.get("archive_sha256"):
            # Q3: an accepted canonical ZIP is immutable.
            raise HoldError("Accepted canonical ZIP changed; the prepared session and its outputs are unchanged. "
                            "Restore the original ZIP, or upload the new export as a new _N session")
        if is_canonical and valid_previous and not cfg.force_reextract:
            # The same bytes with a new size or mtime (for example restored from a backup).
            event.update(outcome="refresh", session=dict(previous, archive_signature=fingerprint),
                         note="canonical ZIP unchanged; its new file signature is recorded")
        elif found is not None and found != recorded and not cfg.force_reextract:
            # A published extraction that state does not record (Q12 adoption rule, D6).
            if not receipts.adoptable(destination, "extract", digest):
                raise HoldError("The extraction on disk does not match this ZIP and was left untouched")
            move_archive(source, canonical, cfg, fingerprint)
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
            inventory = extract_archive(source, stage, stop, accept)
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
                if stop is not None and stop.is_set():
                    raise InterruptedError("Preparation interrupted")
                move_archive(source, canonical, cfg, fingerprint)
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
                fsutil.publish_tree(stage, destination, cfg.dicoms, incoming, aside)
                event["session"] = receipts.prepared_entry(sid, date, label, canonical, digest, inventory, destination,
                                                           previous.get("conversion", {}) if previous else {})
                event["intents"] = [intent]
                event["sources"] = [{"path": str(canonical), "signature": fsutil.signature(canonical), "key": key,
                    "outcome": "prepared", "against": digest, "version": config.SCHEMA_VERSION}]
                event["note"] = (f"{inventory['n_dicom']} DICOMs, {inventory['n_series']} series; "
                                 f"{inventory['skipped_files']} viewer/non-DICOM files omitted"
                                 + (f"; {inventory['repeated_identical']} repeated objects stored once"
                                    if inventory["repeated_identical"] else ""))
                if inventory["header_dates"] and inventory["header_dates"] != [date]:
                    event["issues"].append(reports.problem("date:" + key, "prepare", sid, label, "Header dates differ from filename; filename date retained", canonical))
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
        event["issues"] = [reports.problem(issue_key, "prepare", sid, label, str(e), active_source)]
        event["source"] = {"path": str(active_source), "signature": fingerprint, "key": key, "outcome": outcome,
                            "against": previous.get("archive_sha256", "") if previous else "", "version": config.SCHEMA_VERSION}
        if not previous and not held:
            event["session"] = {"subject_id": sid, "date": date, "label": label, "status": outcome, "version": config.SCHEMA_VERSION}
    finally:
        fsutil.remove_tree(stage, cfg.work)
    return event


def plan_prepare(cfg, state, ids, pairs, logger):
    tasks, counts = {}, {"cached": 0, "held": 0}
    recovering = recovery.blocked_keys(state)
    discovered = discover(cfg)
    for source, legacy in discovered:
        issue_key = "prepare:" + str(source)
        sid = label = ""
        try:
            if source.is_symlink():
                raise ValueError("Symbolic-link ZIPs are not accepted")
            sid, date, label = identity.parse_name(source.name, legacy)
            key = f"{sid}/{label}"
            if cfg.sessions is not None and key not in cfg.sessions:
                continue
            if sid not in ids or ((sid, date) not in pairs and not cfg.allow_unlisted_dates):
                raise ValueError("Subject/date absent from reference CSV; update reference or use --allow-unlisted-dates for known IDs")
            fingerprint = fsutil.signature(source)
            prior = state.data["sessions"].get(key, {})
            cached = state.data["sources"].get(str(source), {})
            unchanged = cached.get("signature") == fingerprint and cached.get("version") == config.SCHEMA_VERSION
            valid = prior.get("status") == "ready" and prior.get("extraction", {}).get("root") == str(cfg.dicoms / "Subjects" / sid / label) and receipts.fast_valid(prior["extraction"], "extract", cfg.audit)
            if not cfg.force_reextract and unchanged:
                if valid and str(source) == prior.get("archive") and fingerprint == prior.get("archive_signature"):
                    if cfg.audit:
                        # Catches an edit that kept the size and mtime (Q3); runs in dry runs too (P7).
                        if fsutil.sha256(source) != prior.get("archive_sha256"):
                            raise HoldError("Accepted canonical ZIP changed (found by --audit); the prepared "
                                            "session and its outputs are unchanged")
                        if issue_key in state.data["issues"] and not cfg.dry_run:
                            state.checkpoint({"clear_issues": [issue_key]})
                    counts["cached"] += 1
                    continue
                if cached.get("outcome") == "held" and key not in cfg.accept_multi_study:
                    # Not hashed again until the ZIP changes; its issue stays open.
                    counts["held"] += 1
                    continue
                if cached.get("against", "") == prior.get("archive_sha256", "") and cached.get("outcome") in ("duplicate", "failed"):
                    if (cached["outcome"] == "duplicate" and valid) or (cached["outcome"] == "failed" and not cfg.retry_failed):
                        counts["cached"] += 1
                        continue
            if key in recovering:
                # A folder crash recovery would not touch waits for a person (Q12).
                counts["held"] += 1
                logger.warning("Held %s: unresolved crash-recovery issue for %s; see issues.csv", source, key)
                continue
            tasks.setdefault(sid, []).append((source, sid, date, label, fingerprint))
        except (OSError, ValueError) as e:
            counts["held"] += 1
            logger.warning("Held %s: %s", source, e)
            if not cfg.dry_run:
                state.checkpoint({"issues": [reports.problem(issue_key, "prepare", sid, label, str(e), source)]})
    if not cfg.dry_run:
        # Removed/renamed incoming conflicts must not remain permanently unresolved.
        stale = [k for k, v in state.data["issues"].items() if k.startswith("prepare:") and v.get("path") and not Path(v["path"]).exists()]
        # R18: a prepared session whose canonical ZIP is not listed gets a nonblocking issue.
        listed, missing = {str(path) for path, _ in discovered}, []
        for key, entry in state.data["sessions"].items():
            issue = "missing:" + key
            if entry.get("status") != "ready" or not entry.get("archive"):
                continue
            if entry["archive"] in listed and issue in state.data["issues"]:
                stale.append(issue)
            elif entry["archive"] not in listed and issue not in state.data["issues"]:
                missing.append(reports.problem(issue, "prepare", entry["subject_id"], entry["label"],
                                               "Canonical ZIP is missing; the extracted DICOM objects are intact",
                                               entry["archive"]))
        if stale or missing:
            state.checkpoint({"clear_issues": stale, "issues": missing})
    return tasks, counts
