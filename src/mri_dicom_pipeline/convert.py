"""Conversion: run dcm2niix, organize its output and publish sessions."""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from . import __version__, classify, config, fsutil, nifti, receipts, recovery, reports

# A converter that died from one of these signals was stopped, not broken:
# Slurm sends SIGTERM to every process of the job at its time limit (Q12).
STOP_RETURN_CODES = set() if os.name == "nt" else {-signal.SIGTERM, -signal.SIGKILL}
# Options passed to dcm2niix before "-o OUTPUT INPUT"; recorded in provenance (Q8).
CONVERTER_OPTIONS = ["-z", "n", "-b", "y", "-ba", "y", "-c", "", "-d", "9", "-f", "series_%j_%s_%e"]
CONTROL_FILES = {receipts.RECEIPT, receipts.INVENTORY}
IDENTIFYING = re.compile(r"patient.?name|patient.?id|accession|birth.?date|physician|institution|station.?name|serial.?number", re.I)
# dcm2niix saves each DICOM overlay plane as <volume>_ROI<n>.nii, a mask with
# no sidecar of its own. It is published next to its volume, like .bval/.bvec.
OVERLAY = re.compile(r"(.+)_(ROI\d+)")


def _nifti_stem(path):
    return path.name[:-7] if path.name.endswith(".nii.gz") else path.stem


def organize_conversion(raw, output, sid, label, archive_digest):
    """Validate, gzip and file the converter output by scan type.

    Returns (volumes, files, errors, converted series keys, converter versions).
    """
    output.mkdir()
    files, errors, handled, volumes, converted_keys, versions = [], [], set(), 0, set(), set()
    niftis = sorted(p for p in raw.iterdir() if p.name.endswith((".nii", ".nii.gz")))
    stems = {_nifti_stem(p) for p in niftis}
    overlays = {p: m for p in niftis if (m := OVERLAY.fullmatch(_nifti_stem(p))) and m[1] in stems}
    for nii in niftis:
        if nii in overlays:
            continue
        stem = _nifti_stem(nii)
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
            nifti.validate_and_gzip(nii, dest)
            image_valid = True
            series_key = hashlib.sha256(stem.split("_", 2)[1].encode()).hexdigest()[:20] if stem.startswith("series_") and "_" in stem[7:] else token
            enriched = nifti.clean_metadata(metadata)
            enriched.update(subject_id=sid, session=label, session_date=label[:8], scan_type=scan,
                            source_archive_sha256=archive_digest, source_series_key=series_key)
            fsutil.atomic_json(directory / (base + ".json"), enriched)
            handled.update([nii.name, meta_path.name])
            volumes += 1
            converted_keys.add(series_key)
            if metadata.get("ConversionSoftwareVersion"):
                versions.add(str(metadata["ConversionSoftwareVersion"]))
            for suffix in (".bval", ".bvec"):
                companion = raw / (stem + suffix)
                if companion.exists():
                    nifti.check_companion(companion)
                    shutil.copyfile(companion, directory / (base + suffix))
                    handled.add(companion.name)
            for overlay, match in overlays.items():
                if match[1] == stem:
                    nifti.validate_and_gzip(overlay, directory / f"{base}_{match[2]}.nii.gz")
                    handled.add(overlay.name)
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
    return volumes, files, errors, converted_keys, versions


def redact_line(text):
    """One converter output line, with identifying diagnostics omitted and long lines cut."""
    return ("[identifying metadata diagnostic omitted]" if IDENTIFYING.search(text) else text)[:2000]


def _signal_converter(process, kill):
    """Stop the converter: its whole process group on POSIX, the process itself on Windows (Q2)."""
    if os.name == "nt":
        process.kill() if kill else process.terminate()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def run_converter(command, log_path, env, stop, timeout):
    """Run the converter, redacting its output line by line on the way to log_path (P8).

    On POSIX the converter gets its own process group, so stopping it also
    stops the processes it started. Returns (return code, reason): reason is
    "stopped" after a stop or a SIGTERM/SIGKILL death, "timeout" after the
    time limit, and "" when the converter ended by itself.
    """
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                               start_new_session=os.name != "nt")

    def copy_redacted():
        with open(log_path, "w", encoding="utf-8") as log:
            for line in process.stdout:
                log.write(redact_line(line.decode("utf-8", errors="replace").rstrip("\r\n")) + "\n")
                log.flush()

    reader = threading.Thread(target=copy_redacted, daemon=True)
    reader.start()
    started, reason = time.monotonic(), ""
    try:
        while process.poll() is None:
            if stop.wait(0.25) or time.monotonic() - started > timeout:
                reason = "stopped" if stop.is_set() else "timeout"
                _signal_converter(process, kill=False)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _signal_converter(process, kill=True)
                    process.wait()
                break
    finally:
        if process.poll() is None:
            _signal_converter(process, kill=True)
            process.wait()
        if os.name != "nt":
            # Remove any helper the converter left running in its group.
            _signal_converter(process, kill=True)
    # A helper that outlives the converter on Windows may hold the pipe open.
    reader.join(timeout=10)
    if not reason and process.returncode in STOP_RETURN_CODES:
        reason = "stopped"
    return process.returncode, reason


def complete_record(entry, canonical):
    """The session's complete conversion at the configured NIfTI root, or None (Q7)."""
    conversion = entry.get("conversion") or {}
    return conversion if conversion.get("status") == "converted" and conversion.get("root") == str(canonical) else None


def files_outside_inventory(folder):
    """Files in a published folder that its inventory does not list (sorted, relative)."""
    listed = {item["name"] for item in fsutil.read_json(Path(folder) / receipts.INVENTORY)["files"]} | CONTROL_FILES
    found = (p.relative_to(folder).as_posix() for p in Path(folder).rglob("*") if p.is_file())
    return sorted(name for name in found if name not in listed)


def revalidate(record, folder):
    """A fresh record for a complete folder restored with new mtimes, or None (Q7).

    The receipt must be the recorded one, every inventory file must hash to its
    recorded value, and no other files may exist.
    """
    try:
        if receipts.receipt_id(folder) != record.get("receipt_id") or files_outside_inventory(folder):
            return None
        for item in fsutil.read_json(folder / receipts.INVENTORY)["files"]:
            path = folder / item["name"]
            if path.stat().st_size != item["size"] or fsutil.sha256(path) != item["sha256"]:
                return None
        return dict(record, **receipts.receipt_info(folder, folder / receipts.RECEIPT, folder / receipts.INVENTORY))
    except (OSError, ValueError, KeyError):
        return None


def _record_failure(event, updated, last_good, status, output, note, path, record, cfg):
    """Record a failed, partial or interrupted attempt, keeping a complete conversion (Q7, P4)."""
    sid, label = updated["subject_id"], updated["label"]
    event.update(outcome="interrupted" if status == "interrupted" else "failed", note=note)
    event["issues"] = [reports.problem(event["clear_issues"][0], "convert", sid, label, note, path)]
    if last_good:
        updated["last_attempt"] = {"status": status, "output": output, "run_id": cfg.run_id, "reason": note}
    else:
        updated["conversion"] = record


def convert_one(entry, cfg, stop):
    sid, label = entry["subject_id"], entry["label"]
    key, issue_key = f"{sid}/{label}", "convert:" + f"{sid}/{label}"
    updated = copy.deepcopy(entry)
    event = {"key": key, "session": updated, "clear_issues": [issue_key], "issues": [], "outcome": "converted"}
    canonical = cfg.niftis / "Subjects" / sid / label
    last_good = complete_record(entry, canonical)
    stage = Path(tempfile.mkdtemp(prefix="convert_", dir=cfg.work))
    raw = stage / "raw"
    raw.mkdir()
    preserve = False
    try:
        if not receipts.fast_valid(entry["extraction"], "extract", cfg.audit):
            raise ValueError("Extracted input changed or is missing; run preparation first")
        if last_good:
            # An explicit reconversion never deletes files people added (Q7).
            extra = files_outside_inventory(canonical)
            if extra:
                raise ValueError("Explicit reconversion refused; files not in inventory: " + ", ".join(extra[:20]))
        required, excused = receipts.required_series(fsutil.read_json(entry["extraction"]["inventory"]))
        needed = max(entry.get("pixel_bytes", 0) * 2, entry.get("dicom_bytes", 0) * 2)
        if shutil.disk_usage(stage).free < needed:
            raise ValueError("Insufficient free staging space for this session")
        binary = cfg.dcm2niix_bin
        command = ([sys.executable, binary] if binary.endswith(".py") else [binary]) + CONVERTER_OPTIONS + [
            "-o", str(raw), str(Path(entry["extraction"]["root"]) / "dicom")]
        env = dict(os.environ, OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
        returncode, reason = run_converter(command, stage / "converter.log", env, stop, cfg.timeout)
        event["detail"] = (stage / "converter.log").read_text(encoding="utf-8", errors="replace")
        if reason == "stopped":
            # After a stop nothing is organized or published; the stage is kept (Q12).
            preserve = True
            note = f"Stopped during conversion; working files retained: {stage}"
            _record_failure(event, updated, last_good, "interrupted", str(stage), note, stage,
                            {"status": "interrupted", "source_sha256": entry["content_sha256"], "output": str(stage)}, cfg)
            return event
        errors = []
        if returncode != 0:
            errors.append("Converter timed out" if reason == "timeout" else f"dcm2niix exited with code {returncode}")
        organized = stage / "organized"
        n_volumes, file_list, organize_errors, converted_keys, versions = organize_conversion(
            raw, organized, sid, label, entry["archive_sha256"])
        errors += organize_errors
        if not n_volumes:
            errors.append("No valid NIfTI volumes produced")
        coverage = receipts.coverage(required, excused, converted_keys)
        errors += [f"Missing required series: {series_key}" for series_key in coverage["missing"]]
        complete = not errors
        provenance = {"package_version": __version__, "run_id": cfg.run_id,
                      "converter": str(Path(shutil.which(binary) or binary).resolve()),
                      "arguments": list(CONVERTER_OPTIONS), "converter_versions": sorted(versions)}
        receipt_id = uuid.uuid4().hex
        fsutil.atomic_json(organized / receipts.INVENTORY, {"files": file_list})
        fsutil.atomic_json(organized / receipts.RECEIPT, {"version": config.SCHEMA_VERSION, "complete": complete,
            "source_sha256": entry["content_sha256"], "receipt_id": receipt_id, "run_id": cfg.run_id,
            "n_volumes": n_volumes, "provenance": provenance, "coverage": coverage})
        destination = canonical if complete else cfg.niftis / ".pending" / sid / label / stage.name
        if complete and destination.exists() and not (destination / receipts.RECEIPT).exists():
            raise ValueError("NIfTI destination is not owned by this pipeline; refusing replacement")
        incoming, aside = fsutil.swap_names(destination)
        intent = recovery.write_intent(cfg.reports, stage="convert", key=key, run_id=cfg.run_id,
            stage_dir=str(stage), destination=str(destination), incoming=str(incoming), previous=str(aside),
            ownership_root=str(cfg.niftis), receipt_id=receipt_id, expected_digest=entry["content_sha256"],
            attempt=not complete)
        fsutil.publish_tree(organized, destination, cfg.niftis, incoming, aside)
        record = receipts.conversion_record(destination, "converted" if complete else "partial", n_volumes,
                                            entry["content_sha256"], provenance, coverage)
        if complete:
            updated["conversion"] = record
            updated.pop("last_attempt", None)
            event["clear_issues"].append("output:" + key)
            event["note"] = f"{n_volumes} volumes; all required series and companions published"
        else:
            preserve = True
            note = "; ".join(errors) + f"; working files retained: {stage}"
            _record_failure(event, updated, last_good, "partial", str(destination), note, destination, record, cfg)
        event["intents"] = [intent]
    except Exception as e:
        preserve = True
        status = "interrupted" if stop.is_set() else "failed"
        note = f"{type(e).__name__}: {e}; working files retained: {stage}"
        _record_failure(event, updated, last_good, status, str(stage), note, stage,
                        {"status": status, "source_sha256": entry["content_sha256"], "output": str(stage)}, cfg)
    finally:
        if not preserve:
            fsutil.remove_tree(stage, cfg.work)
    return event


def plan_convert(cfg, state, ids, pairs, logger):
    tasks, counts = {}, {"cached": 0, "held": 0}
    issues = state.data["issues"]
    blocked = {(i.get("subject"), i.get("session")) for k, i in issues.items()
               if i.get("stage") == "prepare" and not k.startswith(reports.NONBLOCKING_PREFIXES)}
    recovering = recovery.blocked_keys(state)

    def save(event):
        if not cfg.dry_run:
            state.checkpoint(event)

    for key, entry in sorted(state.data["sessions"].items()):
        if cfg.sessions is not None and key not in cfg.sessions:
            continue
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
        elif key in recovering:
            reason = "Unresolved crash-recovery problem; see issues.csv"
        elif not receipts.fast_valid(entry["extraction"], "extract", cfg.audit):
            reason = "Extracted input changed or missing; run preparation"
        if reason:
            counts["held"] += 1
            logger.warning("Held %s: %s", key, reason)
            save({"issues": [reports.problem("convert:" + key, "convert", sid, label, reason)]})
            continue
        canonical = cfg.niftis / "Subjects" / sid / label
        output_issue, conversion = "output:" + key, entry.get("conversion") or {}
        complete = complete_record(entry, canonical)
        if complete and not cfg.force_reconvert:
            # A complete session is never reconverted automatically (Q7, P10).
            if receipts.fast_valid(complete, "convert", cfg.audit):
                counts["cached"] += 1
                stale = [k for k in ("convert:" + key, output_issue) if k in issues]
                if stale:
                    save({"clear_issues": stale})
                continue
            refreshed = revalidate(complete, canonical) if cfg.audit and cfg.sessions and key in cfg.sessions else None
            if refreshed:
                counts["cached"] += 1
                logger.info("Revalidated %s: every listed file matches; new folder times recorded", key)
                save({"key": key, "session": dict(entry, conversion=refreshed), "clear_issues": [output_issue, "convert:" + key]})
                continue
            counts["held"] += 1
            note = ("Output folder modified or missing; it was not reconverted. Revalidate a restored folder with "
                    "convert --audit --session KEY, or reconvert with --force-reconvert --session KEY")
            logger.warning("Held %s: %s", key, note)
            save({"issues": [reports.problem(output_issue, "convert", sid, label, note, canonical)]})
            continue
        if not complete and not cfg.force_reconvert and (canonical / receipts.RECEIPT).exists():
            # A published folder that state does not record: adopt it or flag it, never replace it (Q7, D6).
            if receipts.adoptable(canonical, "convert", entry["content_sha256"]):
                counts["cached"] += 1
                logger.info("Adopted %s: the published folder matches and every listed file is present", key)
                adopted = dict(entry, conversion=receipts.adopted_conversion(canonical, entry["content_sha256"]))
                adopted.pop("last_attempt", None)
                save({"key": key, "session": adopted, "clear_issues": [output_issue, "convert:" + key]})
                continue
            counts["held"] += 1
            note = "Unreferenced output folder that does not match the prepared session; it was not replaced"
            logger.warning("Held %s: %s", key, note)
            save({"issues": [reports.problem(output_issue, "convert", sid, label, note, canonical)]})
            continue
        same = conversion.get("source_sha256") == entry["content_sha256"]
        if not cfg.force_reconvert and same and conversion.get("status") in ("failed", "partial") and not cfg.retry_failed:
            counts["cached"] += 1
            continue
        tasks.setdefault(sid, []).append(copy.deepcopy(entry))
    return tasks, counts
