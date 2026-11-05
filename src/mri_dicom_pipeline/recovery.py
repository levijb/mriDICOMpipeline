"""Publish intents and crash reconciliation (Q12).

Before publishing a folder, a worker writes an intent naming the destination,
its swap folders, and the receipt it is publishing. The coordinator deletes
the intent once the result is saved in state. At startup, each non-dry run
resolves the open intents of its own command: it rolls the swap back, removes
an unfinished copy so the work is redone, or adopts a published folder that
passes the adoption rule. A copied .incoming_* folder is never published.
"""

from __future__ import annotations

import copy
from pathlib import Path
import time
import uuid

from . import config, fsutil, receipts, reports


def intents_dir(reports_root):
    return Path(reports_root) / ".state" / "intents"


def write_intent(reports_root, **fields):
    """Record a folder publication before it starts; returns the intent ID."""
    intent_id = uuid.uuid4().hex
    fsutil.atomic_json(intents_dir(reports_root) / f"{intent_id}.json",
                       {"version": config.SCHEMA_VERSION, "id": intent_id, "kind": "publish",
                        "created_utc": time.time(), **fields})
    return intent_id


def clear_intents(reports_root, ids):
    """Delete intents whose results are saved in state."""
    for intent_id in ids:
        (intents_dir(reports_root) / f"{intent_id}.json").unlink(missing_ok=True)


def blocked_keys(state):
    """Session keys held by a crash-recovery issue that needs a person."""
    return {f"{issue.get('subject')}/{issue.get('session')}"
            for key, issue in state.data["issues"].items() if key.startswith("recover:")}


def reconcile(cfg, phase, state, logger, dry_run):
    """Resolve this command's open intents on its locked roots; returns how many were resolved."""
    folder = intents_dir(cfg.reports)
    locked = {str(root) for root in cfg.lock_roots}
    if not dry_run:
        # A recover: issue whose intent file is gone has been resolved by hand.
        stale = [key for key in state.data["issues"]
                 if key.startswith("recover:") and not (folder / f"{key[len('recover:'):]}.json").exists()]
        if stale:
            state.checkpoint({"clear_issues": stale})
    resolved = 0
    for path in sorted(folder.glob("*.json")):
        intent = fsutil.read_json(path)
        if intent.get("stage") != phase or intent.get("ownership_root") not in locked:
            continue
        action, done = _resolve(intent, state, cfg, dry_run)
        logger.warning("%s %s %s after an interrupted run: %s", "WOULD RECONCILE" if dry_run else "RECONCILED",
                       phase, intent["key"], action)
        resolved += done
    return resolved


def _recorded_receipt(state, intent):
    """The receipt_id state records for the intent's destination, if any."""
    entry = state.data["sessions"].get(intent["key"], {})
    record = entry.get("extraction" if intent["stage"] == "prepare" else "conversion", {})
    return record.get("receipt_id") if record.get("root") == intent["destination"] else None


def _adoption_event(intent, state, destination):
    """The state event that records an adopted folder, or None if it cannot be built."""
    key, digest = intent["key"], intent["expected_digest"]
    try:
        if intent["stage"] == "prepare":
            canonical = Path(intent["archive"])
            inventory = fsutil.read_json(destination / receipts.INVENTORY)
            prior = state.data["sessions"].get(key, {})
            entry = receipts.prepared_entry(intent["subject_id"], intent["date"], intent["label"], canonical,
                                            digest, inventory, destination, prior.get("conversion", {}))
            source = {"path": str(canonical), "signature": fsutil.signature(canonical), "key": key,
                      "outcome": "prepared", "against": digest, "version": config.SCHEMA_VERSION}
            return {"key": key, "session": entry, "sources": [source]}
        entry = copy.deepcopy(state.data["sessions"][key])
        entry["conversion"] = receipts.adopted_conversion(destination, digest)
        entry.pop("last_attempt", None)
        return {"key": key, "session": entry, "clear_issues": ["convert:" + key, "output:" + key]}
    except (OSError, ValueError, KeyError):
        return None


def _resolve(intent, state, cfg, dry_run):
    """Apply the Q12 table to one intent; returns (description, resolved)."""
    root = Path(intent["ownership_root"])
    destination, incoming, aside = (Path(intent[name]) for name in ("destination", "incoming", "previous"))
    for path in (destination, incoming, aside):
        fsutil.owned(path, root)
    issue = "recover:" + intent["id"]
    event = None
    if destination.exists():
        found = receipts.receipt_id(destination)
        if intent.get("attempt"):
            action = "kept the published attempt folder"
        elif found is not None and found == _recorded_receipt(state, intent):
            action = "the result was already saved; removed leftover swap folders"
        elif found is not None and found == intent["receipt_id"]:
            kind = "extract" if intent["stage"] == "prepare" else "convert"
            if receipts.adoptable(destination, kind, intent["expected_digest"]):
                event = _adoption_event(intent, state, destination)
            if event is None:
                return _refuse(intent, state, dry_run, "the published folder fails the adoption check "
                                                       "(a listed file is missing or has the wrong size)")
            action = "adopted the published folder after checking every listed file"
        else:
            return _refuse(intent, state, dry_run, "the destination holds an unexpected receipt")
        if not dry_run:
            if event is not None:
                state.checkpoint(event)
            fsutil.remove_tree(aside, root)
            fsutil.remove_tree(incoming, root)
    elif aside.exists():
        action = "rolled back to the previous folder"
        if not dry_run:
            fsutil.retry_locked(lambda: aside.rename(destination))
            fsutil.remove_tree(incoming, root)
    else:
        action = "removed the unfinished copy; the work will be redone"
        if not dry_run:
            fsutil.remove_tree(incoming, root)
    if not dry_run:
        _remove_stage(intent, state, cfg)
        clear_intents(cfg.reports, [intent["id"]])
        if issue in state.data["issues"]:
            state.checkpoint({"clear_issues": [issue]})
    return action, True


def _refuse(intent, state, dry_run, reason):
    """Touch nothing and raise a blocking issue that names the destination."""
    if not dry_run:
        sid, label = intent["key"].split("/", 1)
        note = f"Crash recovery left this folder untouched: {reason}. See the runbook."
        state.checkpoint({"issues": [reports.problem("recover:" + intent["id"], intent["stage"], sid, label,
                                                     note, intent["destination"])]})
    return f"left untouched ({reason}); raised an issue", False


def _remove_stage(intent, state, cfg):
    """Remove the intent's stage, unless an issue points at it or it is on another node."""
    stage = Path(intent.get("stage_dir") or "")
    if not intent.get("stage_dir") or not stage.exists():
        return
    if any(issue.get("path") == str(stage) for issue in state.data["issues"].values()):
        return
    if stage.resolve().is_relative_to(cfg.work):
        fsutil.remove_tree(stage, cfg.work)
