"""Shared implementation for the study-agnostic DICOM commands (Python >= 3.11).

Only the coordinator updates permanent state/reports. Workers own distinct
subjects, staging directories and output sessions. No patient identity fields
are used or copied into state. A normal rerun never traverses old DICOM trees.
"""

from __future__ import annotations

import concurrent.futures as futures
import copy
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import shutil
import signal
import sys
import threading
import time

from . import config, convert, identity, locks, naming, prepare, recovery, reports, selection
from .state import State, refuse_moved_project


class StopSignal:
    """The run's stop flag, shared by SIGTERM and Ctrl+C (Q12).

    The first signal sets the flag; later signals are ignored until the run
    ends. Workers stop at the flag, and nothing is published after it.
    """
    def __init__(self):
        self.event = threading.Event()
        self.signum = None

    def __call__(self, signum, frame):
        if self.signum is None:
            self.signum = signum
            self.event.set()


# Run-summary counter for each worker outcome; anything else counts as completed.
OUTCOME_COUNTERS = {"duplicate": "duplicate", "failed": "failed", "interrupted": "failed",
                    "held": "held", "refresh": "cached"}


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
    stopper, previous_handlers = StopSignal(), {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.signal(signum, stopper)
    try:
        cfg = config.configure(args, phase)
        ids, pairs, roster_warnings = identity.load_roster(cfg.reference, cfg.subject_column, cfg.date_column)
        # A lock on every root the command touches, so commands that share any
        # root exclude each other whatever their project roots (Q13). Dry runs
        # take no lock.
        if phase == "select":
            target = selection.run
        else:
            def target(cfg, ids, pairs, roster_warnings, stopper):
                return run(cfg, phase, ids, pairs, roster_warnings, stopper)
        if cfg.dry_run:
            return target(cfg, ids, pairs, roster_warnings, stopper)
        with locks.RootLocks(cfg.lock_roots, phase, cfg.run_id):
            return target(cfg, ids, pairs, roster_warnings, stopper)
    except (ValueError, RuntimeError, OSError) as e:
        logger = logging.getLogger("mri_dicom_pipeline")
        if logger.handlers:
            logger.error("RUN ABORTED: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        logger = logging.getLogger("mri_dicom_pipeline")
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()


def run(cfg, phase, ids, pairs, roster_warnings, stopper):
    logger = reports.setup_log(cfg.reports, phase, cfg.dry_run)
    started = time.monotonic()
    # Filled in as the run goes. A locked run that stops early still writes
    # its history row, with outcome "aborted" (R16).
    summary = {"run_id": cfg.run_id, "stage": phase, "outcome": "aborted", "reconciled": 0, "notes": ""}
    try:
        return _run(cfg, phase, ids, pairs, roster_warnings, stopper, logger, started, summary)
    finally:
        if not cfg.dry_run:
            summary["seconds"] = round(time.monotonic() - started, 2)
            reports.append_history(cfg, summary, logger)


def _run(cfg, phase, ids, pairs, roster_warnings, stopper, logger, started, summary):
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
        summary["notes"] = "state recovered from index.json.bak"
    refuse_moved_project(cfg, state)
    summary["reconciled"] = recovery.reconcile(cfg, phase, state, logger, cfg.dry_run)
    if summary["reconciled"] and not cfg.dry_run:
        logger.info("Resolved %s open operations (interrupted runs or pending cleanups)", summary["reconciled"])
    if phase == "prepare":
        # Unnamed ZIPs in subject folders get their ID_YYYYMMDD name first (owner request, 2026-11-18).
        skip, held, names = naming.name_uploads(cfg, state, ids, pairs, logger)
        named_held = len(held)
        tasks, counts = prepare.plan_prepare(cfg, state, ids, pairs, logger, skip, held)
        counts["held"] += named_held
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
    # In a dry run the unnamed ZIPs are not renamed, so they are planned only once named.
    once_named = f" (plus {len(names)} unnamed ZIPs once named)" if phase == "prepare" and cfg.dry_run and names else ""
    logger.info("Plan: %s work items across %s subjects%s; %s cached; %s held for review. Cached checks did not read old DICOM headers.", pending, len(tasks), once_named, counts["cached"], counts["held"])
    if cfg.dry_run:
        for sid, items in tasks.items():
            for item in items:
                logger.info("WOULD %s %s/%s", phase, sid, item[3] if phase == "prepare" else item["label"])
        if phase == "prepare":
            print(prepare.dry_run_summary(tasks, counts, held, names), flush=True)
        return 2 if counts["held"] else 0
    if phase == "convert" and pending and not (Path(cfg.dcm2niix_bin).is_file() or shutil.which(cfg.dcm2niix_bin)):
        raise ValueError("dcm2niix not found; load its module/environment or pass --dcm2niix-bin")
    if phase == "prepare" and pending:
        try:
            import pydicom
        except ImportError as e:
            raise ValueError("pydicom is missing; install mri-dicom-pipeline before preparation") from e
    cfg.work.mkdir(parents=True, exist_ok=True)
    events, stop = queue.Queue(), stopper.event
    counts.update(completed=0, duplicate=0, failed=0)
    last_progress, last_report = time.monotonic(), time.monotonic()
    moved = []  # Uploads paths that prepare moved ZIPs from
    if stop.is_set():
        tasks = {}  # A stop that arrived before the workers started.

    def accept(event):
        state.checkpoint(event)
        recovery.clear_intents(cfg.reports, event.get("intents", []))
        if event.get("moved_from"):
            moved.append(event["moved_from"])
        outcome = event["outcome"]
        counts[OUTCOME_COUNTERS.get(outcome, "completed")] += 1
        level = logging.ERROR if outcome in ("failed", "interrupted") else logging.WARNING if outcome == "held" else logging.INFO
        logger.log(level, "%s %s: %s", outcome.upper(), event["key"], event.get("note", "identical archive/content; skipped"))
        if event.get("detail"):
            # Converter verbosity belongs only in the latest detailed log.
            for handler in logger.handlers:
                if isinstance(handler, logging.FileHandler) and not isinstance(handler, RotatingFileHandler):
                    record = logger.makeRecord(logger.name, logging.INFO, "", 0, "%s", (event["detail"],), None)
                    handler.handle(record)

    pool = futures.ThreadPoolExecutor(max_workers=cfg.workers)
    jobs = [pool.submit(process_subject, items, cfg, phase, copy.deepcopy({k: v for k, v in state.data["sessions"].items() if k.startswith(sid + "/")}), events, stop) for sid, items in tasks.items()]
    save_failed, stop_logged = False, False
    try:
        while any(not job.done() for job in jobs) or not events.empty():
            try:
                accept(events.get(timeout=0.5))
            except queue.Empty:
                pass
            if stop.is_set() and not stop_logged:
                logger.warning("Stop requested; stopping workers. Completed sessions remain checkpointed")
                stop_logged = True
            if time.monotonic() - last_progress >= 30:
                logger.info("Progress: %s/%s work items finished; %ss elapsed", counts["completed"] + counts["duplicate"] + counts["failed"], pending, int(time.monotonic()-started))
                last_progress = time.monotonic()
            if time.monotonic() - last_report >= 15:
                reports.reports(state, cfg, pairs, logger)
                last_report = time.monotonic()
        for job in jobs:
            job.result()
    finally:
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        while not events.empty():
            accept(events.get_nowait())
        # Attempted, not guaranteed: the journals already hold every result.
        try:
            state.flush()
        except OSError as e:
            save_failed = True
            logger.error("Could not save state at the end of the run; the journals keep every result: %s", e)
        reports.reports(state, cfg, pairs, logger)
    if moved:
        # After the workers, so no folder is deleted while a ZIP in it is still being worked on.
        prepare.remove_emptied_folders(cfg, moved, logger)
    terminated, interrupted = stopper.signum == signal.SIGTERM, stopper.signum is not None
    unresolved = reports.unresolved(state)
    outcome = ("terminated" if terminated else "interrupted" if interrupted
               else "issues" if unresolved or counts["failed"] or counts["held"] or save_failed else "ok")
    elapsed = round(time.monotonic()-started, 2)
    summary.update(counts, outcome=outcome, unresolved=unresolved)
    logger.info("RUN %s END outcome=%s completed=%s cached=%s duplicates=%s failed=%s held=%s unresolved=%s elapsed=%ss", run_id, outcome, counts["completed"], counts["cached"], counts["duplicate"], counts["failed"], counts["held"], unresolved, elapsed)
    logger.info("Read %s first; unresolved problems: %s", cfg.reports / "pipeline.log", cfg.reports / "issues.csv")
    return 143 if terminated else 130 if interrupted else 2 if outcome != "ok" else 0
