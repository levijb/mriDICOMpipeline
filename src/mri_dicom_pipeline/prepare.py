"""Preparation: discover, hash, extract and publish session ZIPs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import uuid
import zipfile

from . import classify, config, fsutil, identity, receipts, reports


SKIP_EXT = set(".jpg .jpeg .png .gif .bmp .tif .tiff .pdf .txt .xml .html .htm .csv .json .ini .log .dll .exe .ocx .config .manifest .css .js .ico .onnx .lic .profiles .mapping .bin .rlv .rlv_meta .rbv .rbv_meta .local .zip".split())


DICOM_TAGS = ["SOPClassUID", "SOPInstanceUID", "StudyInstanceUID", "SeriesInstanceUID",
    "SeriesNumber", "SeriesDescription", "ProtocolName", "Modality", "Rows", "Columns",
    "BitsAllocated", "SamplesPerPixel", "NumberOfFrames", "InstanceNumber", "StudyDate"]


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
                    "scan_type": classify.classify(str(ds.get("SeriesDescription", "")), str(ds.get("ProtocolName", ""))), "instances": 0})
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
    digest = ""
    try:
        digest = fsutil.sha256(source, stop)
        if fsutil.signature(source) != fingerprint:
            raise ValueError("Upload changed during hashing; wait for transfer completion")
        expected_root = cfg.dicoms / "Subjects" / sid / label
        valid_previous = previous and previous.get("status") == "ready" and previous.get("extraction", {}).get("root") == str(expected_root) and receipts.fast_valid(previous["extraction"], "extract", cfg.audit)
        if valid_previous and previous["archive_sha256"] == digest and (not cfg.force_reextract or str(source) != previous.get("archive")):
            event["outcome"] = "duplicate"
        else:
            inventory = extract_archive(source, stage, stop)
            if fsutil.signature(source) != fingerprint:
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
                fsutil.atomic_json(stage / ".inventory.json", inventory)
                fsutil.atomic_json(stage / ".pipeline_complete.json", {"version": config.VERSION, "archive_sha256": digest, "receipt_id": uuid.uuid4().hex})
                fsutil.publish_tree(stage, destination, cfg.dicoms)
                extraction = receipts.receipt_info(destination, destination / ".pipeline_complete.json", destination / ".inventory.json")
                entry = {"subject_id": sid, "date": date, "label": label, "status": "ready", "version": config.VERSION,
                    "archive": str(canonical), "archive_signature": fsutil.signature(canonical), "archive_sha256": digest,
                    "content_sha256": inventory["content_sha256"], "n_dicom": inventory["n_dicom"],
                    "n_series": inventory["n_series"], "dicom_bytes": inventory["dicom_bytes"],
                    "pixel_bytes": inventory["pixel_bytes"], "extraction": extraction,
                    "conversion": previous.get("conversion", {}) if previous else {}}
                event["session"] = entry
                event["sources"] = [{"path": str(canonical), "signature": fsutil.signature(canonical), "key": key,
                    "outcome": "prepared", "against": digest, "version": config.VERSION}]
                event["note"] = f"{inventory['n_dicom']} DICOMs, {inventory['n_series']} series; {inventory['skipped_files']} viewer/non-DICOM files omitted"
                if inventory["header_dates"] and inventory["header_dates"] != [date]:
                    event["issues"].append(reports.problem("date:" + key, "prepare", sid, label, "Header dates differ from filename; filename date retained", canonical))
                else:
                    event["clear_issues"].append("date:" + key)
        event["source"] = {"path": str(source), "signature": fingerprint, "key": key, "outcome": event["outcome"],
                            "against": (event.get("session") or previous)["archive_sha256"], "version": config.VERSION}
    except Exception as e:
        interrupted = isinstance(e, InterruptedError)
        event.update(outcome="interrupted" if interrupted else "failed", note=str(e))
        issue_key = "prepare:" + str(active_source)
        event["clear_issues"].append(issue_key)
        event["issues"] = [reports.problem(issue_key, "prepare", sid, label, str(e), active_source)]
        event["source"] = {"path": str(active_source), "signature": fingerprint, "key": key, "outcome": "interrupted" if interrupted else "failed",
                            "against": previous.get("archive_sha256", "") if previous else "", "version": config.VERSION}
        if not previous:
            event["session"] = {"subject_id": sid, "date": date, "label": label, "status": "interrupted" if interrupted else "failed", "version": config.VERSION}
    finally:
        fsutil.remove_tree(stage, cfg.work)
    return event


def plan_prepare(cfg, state, ids, pairs, logger):
    tasks, counts = {}, {"cached": 0, "held": 0}
    for source, legacy in discover(cfg):
        issue_key = "prepare:" + str(source)
        sid = label = ""
        try:
            if source.is_symlink():
                raise ValueError("Symbolic-link ZIPs are not accepted")
            sid, date, label = identity.parse_name(source.name, legacy)
            if sid not in ids or ((sid, date) not in pairs and not cfg.allow_unlisted_dates):
                raise ValueError("Subject/date absent from reference CSV; update reference or use --allow-unlisted-dates for known IDs")
            key = f"{sid}/{label}"
            fingerprint = fsutil.signature(source)
            prior = state.data["sessions"].get(key, {})
            cached = state.data["sources"].get(str(source), {})
            unchanged = cached.get("signature") == fingerprint and cached.get("version") == config.VERSION
            valid = prior.get("status") == "ready" and prior.get("extraction", {}).get("root") == str(cfg.dicoms / "Subjects" / sid / label) and receipts.fast_valid(prior["extraction"], "extract", cfg.audit)
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
                state.checkpoint({"issues": [reports.problem(issue_key, "prepare", sid, label, str(e), source)]})
    # Removed/renamed incoming conflicts must not remain permanently unresolved.
    if not cfg.dry_run:
        stale = [k for k, v in state.data["issues"].items() if k.startswith("prepare:") and v.get("path") and not Path(v["path"]).exists()]
        if stale:
            state.checkpoint({"clear_issues": stale})
    return tasks, counts
