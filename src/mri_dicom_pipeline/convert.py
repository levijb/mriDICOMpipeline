"""Conversion: run dcm2niix, organize its output and publish sessions."""

from __future__ import annotations

import copy
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from . import classify, config, fsutil, nifti, receipts, reports


def organize_conversion(raw, output, sid, label, archive_digest):
    output.mkdir()
    files, errors, handled, volumes = [], [], set(), 0
    for nii in sorted(p for p in raw.iterdir() if p.name.endswith((".nii", ".nii.gz"))):
        stem = nii.name[:-7] if nii.name.endswith(".nii.gz") else nii.stem
        meta_path = raw / (stem + ".json")
        metadata = {}
        try:
            metadata = fsutil.read_json(meta_path)
            if not isinstance(metadata, dict):
                raise ValueError("sidecar is not a JSON object")
        except (OSError, ValueError) as e:
            errors.append("Missing/invalid JSON sidecar for volume " + hashlib.sha256(stem.encode()).hexdigest()[:12])
        scan = classify.classify(metadata.get("SeriesDescription"), metadata.get("ProtocolName"), metadata.get("ImageType"))
        directory = output / scan
        directory.mkdir(exist_ok=True)
        token = hashlib.sha256(stem.encode()).hexdigest()[:16]
        base = f"{sid}_{label}_{scan}_{token}"
        dest = directory / (base + ".nii.gz")
        image_valid = False
        try:
            if dest.exists():
                raise ValueError("Colliding converter output name; original retained")
            nifti.sanitized_nifti(nii, dest)
            image_valid = True
            enriched = nifti.clean_metadata(metadata)
            enriched.update(subject_id=sid, session=label, session_date=label[:8], scan_type=scan,
                            source_archive_sha256=archive_digest,
                            source_series_key=hashlib.sha256(stem.split("_", 2)[1].encode()).hexdigest()[:20] if stem.startswith("series_") and "_" in stem[7:] else token)
            fsutil.atomic_json(directory / (base + ".json"), enriched)
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
            files.append({"name": p.relative_to(output).as_posix(), "size": p.stat().st_size, "sha256": fsutil.sha256(p)})
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
        if not receipts.fast_valid(entry["extraction"], "extract", cfg.audit):
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
        fsutil.atomic_json(organized / ".inventory.json", {"files": file_list})
        fsutil.atomic_json(organized / ".pipeline_complete.json", {"version": config.SCHEMA_VERSION, "complete": complete,
            "source_sha256": entry["content_sha256"], "receipt_id": uuid.uuid4().hex})
        destination = cfg.niftis / "Subjects" / sid / label if complete else cfg.niftis / ".pending" / sid / label / stage.name
        if complete and destination.exists() and not (destination / ".pipeline_complete.json").exists():
            raise ValueError("NIfTI destination is not owned by this pipeline; refusing replacement")
        fsutil.publish_tree(organized, destination, cfg.niftis)
        conversion = receipts.receipt_info(destination, destination / ".pipeline_complete.json", destination / ".inventory.json")
        conversion.update(status="converted" if complete else "interrupted" if stop.is_set() else "partial", output=str(destination), n_volumes=n_volumes,
                          source_sha256=entry["content_sha256"])
        updated["conversion"] = conversion
        if errors:
            preserve = True
            event.update(outcome="interrupted" if stop.is_set() else "failed", note="; ".join(errors) + f"; working files retained: {stage}")
            event["issues"] = [reports.problem(issue_key, "convert", sid, label, event["note"], destination)]
        else:
            event["note"] = f"{n_volumes} volumes; all image/JSON/diffusion companions published"
    except Exception as e:
        preserve = True
        event.update(outcome="interrupted" if stop.is_set() else "failed", note=f"{type(e).__name__}: {e}; working files retained: {stage}")
        updated["conversion"] = {"status": "interrupted" if stop.is_set() else "failed", "source_sha256": entry["content_sha256"], "output": str(stage)}
        event["issues"] = [reports.problem(issue_key, "convert", sid, label, event["note"], stage)]
    finally:
        if not preserve:
            fsutil.remove_tree(stage, cfg.work)
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
        elif not receipts.fast_valid(entry["extraction"], "extract", cfg.audit):
            reason = "Extracted input changed or missing; run preparation"
        if reason:
            counts["held"] += 1
            logger.warning("Held %s: %s", key, reason)
            if not cfg.dry_run:
                state.checkpoint({"issues": [reports.problem("convert:" + key, "convert", sid, label, reason)]})
            continue
        conv = entry.get("conversion", {})
        same = conv.get("source_sha256") == entry["content_sha256"]
        if not cfg.force_reconvert and same:
            if conv.get("status") == "converted" and conv.get("root") == str(cfg.niftis / "Subjects" / sid / label) and receipts.fast_valid(conv, "convert", cfg.audit):
                counts["cached"] += 1
                if not cfg.dry_run and "convert:" + key in state.data["issues"]:
                    state.checkpoint({"clear_issues": ["convert:" + key]})
                continue
            if conv.get("status") in ("failed", "partial") and not cfg.retry_failed:
                counts["cached"] += 1
                continue
        tasks.setdefault(sid, []).append(copy.deepcopy(entry))
    return tasks, counts
