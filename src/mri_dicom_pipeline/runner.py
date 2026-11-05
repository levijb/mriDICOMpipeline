"""Shared implementation for the study-agnostic DICOM commands (Python >= 3.11).

Only the coordinator updates permanent state/reports. Workers own distinct
subjects, staging directories and output sessions. No patient identity fields
are used or copied into state. A normal rerun never traverses old DICOM trees.
"""

from __future__ import annotations

import concurrent.futures as futures
import copy
import csv
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import shutil
import sys
import threading
import time

from . import config, convert, identity, locks, prepare, reports
from .state import State


def process_subject(items, cfg, phase, session_state, events, stop):
    local_sessions = session_state
    for item in items:
        if stop.is_set():
            break
        if phase == "prepare":
            key = f"{item[1]}/{item[3]}"
            event = prepare.prepare_one(item, local_sessions.get(key), cfg, stop)
            if event.get("session"):
                local_sessions[key] = event["session"]
        else:
            event = convert.convert_one(item, cfg, stop)
        events.put(event)


def main(phase, argv=None):
    args = config.config_parser(phase).parse_args(argv)
    try:
        cfg = config.configure(args, phase)
        ids, pairs, roster_warnings = identity.load_roster(cfg.reference, cfg.subject_column, cfg.date_column)
        # A lock on every root the command touches, so commands that share any
        # root exclude each other whatever their project roots (Q13). Dry runs
        # take no lock.
        if cfg.dry_run:
            return run(cfg, phase, ids, pairs, roster_warnings)
        with locks.RootLocks(cfg.lock_roots, phase, cfg.run_id):
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
    logger = reports.setup_log(cfg.reports, phase, cfg.dry_run)
    started = time.monotonic()
    run_id = cfg.run_id
    logger.info("RUN %s START stage=%s workers=%s dry_run=%s", run_id, phase, cfg.workers, cfg.dry_run)
    if not cfg.dry_run:
        logger.info("Main log: %s | Details: %s", cfg.reports / "pipeline.log", cfg.reports / f"latest_{phase}.log")
        logger.info("Current status: %s | Problems: %s", cfg.reports / "session_status.csv", cfg.reports / "issues.csv")
    for warning in roster_warnings:
        logger.warning(warning)
    state = State(cfg.reports, run_id)
    if state.recovered_from_backup:
        logger.error("State index %s could not be read; loaded %s and its journals instead. "
                     "The unreadable file is moved aside at the next save.", state.path, state.backup)
    if phase == "prepare":
        if not cfg.import_legacy and (cfg.uploads / "Subjects").is_dir() and not state.data["sessions"]:
            logger.warning("Legacy DicomUploads/Subjects is not scanned by default. Preview migration with --import-legacy --dry-run.")
        tasks, counts = prepare.plan_prepare(cfg, state, ids, pairs, logger)
    else:
        tasks, counts = convert.plan_convert(cfg, state, ids, pairs, logger)
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
                reports.reports(state, cfg, pairs)
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
        reports.reports(state, cfg, pairs)
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
