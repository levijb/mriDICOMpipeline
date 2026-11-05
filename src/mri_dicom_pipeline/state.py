"""Persistent state index with crash-recoverable journals.

The index (index.json) and its previous copy (index.json.bak) each record the
highest journal sequence number they include. Startup loads one of them by
the Q21 decision table and replays only journals with a higher number, so a
partial journal deletion can never roll a session back (P11).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
import uuid

from . import config, fsutil

_MISSING, _UNREADABLE = object(), object()


class StateError(ValueError):
    """The state index cannot be used; the run stops without writing."""


def _read_index(path):
    """The parsed index at path, or _MISSING, or _UNREADABLE."""
    try:
        data = fsutil.read_json(path)
    except FileNotFoundError:
        return _MISSING
    except (OSError, ValueError):
        return _UNREADABLE
    return data if isinstance(data, dict) else _UNREADABLE


class State:
    """Compact index plus crash-recoverable, small per-result journals."""
    def __init__(self, report_root, run_id=None):
        self.root = Path(report_root) / ".state"
        self.path = self.root / "index.json"
        self.backup = self.root / "index.json.bak"
        self.pending = self.root / "pending"
        self.run_id = run_id
        index, backup = _read_index(self.path), _read_index(self.backup)
        # True when index.json exists but cannot be read; it is moved aside at
        # the first save (never during a dry run, which does not save).
        self.recovered_from_backup = index is _UNREADABLE and isinstance(backup, dict)
        self._move_aside = self.recovered_from_backup
        if isinstance(index, dict):
            self.data, loaded = index, self.path
        elif isinstance(backup, dict):
            self.data, loaded = backup, self.backup
        elif index is _MISSING and backup is _MISSING:
            self.data, loaded = {"version": config.SCHEMA_VERSION, "last_seq": 0,
                                 "sessions": {}, "sources": {}, "issues": {}}, None
        else:
            raise StateError(f"Neither state index can be read: {self.path} and {self.backup}")
        if self.data.get("version") != config.SCHEMA_VERSION:
            raise StateError(f"Unsupported state version {self.data.get('version')} in {loaded}; "
                             "this version starts new projects only. Use a new --report-root.")
        # Sequence numbers of the files on disk: deleting journals up to the
        # backup's number keeps .bak plus the journals able to rebuild state.
        self.saved_seq = index.get("last_seq", 0) if isinstance(index, dict) else 0
        self.backup_seq = backup.get("last_seq", 0) if isinstance(backup, dict) else 0
        self.last_seq = self.data.get("last_seq", 0)
        self.last_save = time.monotonic()
        for seq, path in sorted(self._journals()):
            if seq > self.last_seq:
                event = fsutil.read_json(path)
                if event.get("seq") != seq:
                    raise StateError(f"Journal {path} does not match its sequence number")
                self.apply(event)
                self.last_seq = seq

    def _journals(self):
        """(sequence number, path) for each journal file."""
        found = []
        for path in self.pending.glob("*.json"):
            try:
                found.append((int(path.name.split("_", 1)[0], 16), path))
            except ValueError:
                raise StateError(f"Unexpected file in the journal folder: {path}") from None
        return found

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
        self.last_seq += 1
        event["seq"] = self.last_seq
        fsutil.atomic_json(self.pending / f"{self.last_seq:016x}_{uuid.uuid4().hex[:4]}.json", event)
        self.apply(event)
        if time.monotonic() - self.last_save > 10:
            self.flush()

    def flush(self):
        """Write the index, keep the previous one as .bak, and drop journals both include."""
        self.data["last_seq"] = self.last_seq
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.root / (".tmp_" + uuid.uuid4().hex[:12])
        try:
            with temp.open("w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            if self._move_aside and self.path.exists():
                # Never let an unreadable index replace the good backup.
                suffix = self.run_id or str(int(time.time()))
                os.replace(self.path, self.root / f"index.json.corrupt-{suffix}")
            elif self.path.exists():
                os.replace(self.path, self.backup)
                self.backup_seq = self.saved_seq
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)
        self._move_aside = False
        self.saved_seq = self.last_seq
        for seq, path in self._journals():
            if seq <= self.backup_seq:
                path.unlink(missing_ok=True)
        self.last_save = time.monotonic()
