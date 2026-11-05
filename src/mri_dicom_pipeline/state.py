"""Persistent state index with crash-recoverable journals."""

from __future__ import annotations

from pathlib import Path
import time
import uuid

from . import config, fsutil


class State:
    """Compact index plus crash-recoverable, small per-result journals."""
    def __init__(self, report_root):
        self.root = Path(report_root) / ".state"
        self.path = self.root / "index.json"
        self.data = fsutil.read_json(self.path) if self.path.exists() else {"version": config.VERSION, "sessions": {}, "sources": {}, "issues": {}}
        if self.data.get("version") != config.VERSION:
            raise ValueError("Unsupported state version; preserve .state and migrate explicitly")
        self.pending = self.root / "pending"
        self.last_save = time.monotonic()
        for p in sorted(self.pending.glob("*.json")):
            self.apply(fsutil.read_json(p))

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
        fsutil.atomic_json(self.pending / (f"{time.time_ns():x}_" + uuid.uuid4().hex[:4] + ".json"), event)
        self.apply(event)
        if time.monotonic() - self.last_save > 10:
            self.flush()

    def flush(self):
        fsutil.atomic_json(self.path, self.data)
        for p in self.pending.glob("*.json"):
            p.unlink()
        self.last_save = time.monotonic()
