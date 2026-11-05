"""Process locks that keep two commands from running on the same storage roots."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import socket
import time

from . import fsutil

LOCK_NAME = ".mri_dicom_pipeline.lock"
OWNER_NAME = LOCK_NAME + ".owner"


class LockBusy(RuntimeError):
    """Another pipeline command holds a lock this command needs."""


class ProjectLock:
    """OS lock releases on process exit, including crashes; file itself persists."""
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b" ")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.file.close()
            raise RuntimeError("Another pipeline command is running for this project") from e
        return self

    def __exit__(self, *args):
        self.file.close()


def _holder(root):
    """Who holds the lock on root, from its owner file, for the error message."""
    try:
        owner = fsutil.read_json(root / OWNER_NAME)
        since = datetime.fromtimestamp(owner["started_utc"]).strftime("%Y-%m-%d %H:%M:%S")
        return f"{owner['host']} pid {owner['pid']} ({owner['command']}, run {owner['run_id']}, since {since})"
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown (no readable owner file)"


class RootLocks:
    """One lock per storage root, taken in sorted path order (Q13).

    Two commands that share any root exclude each other, whatever their
    project roots. Each held lock has an owner file naming the holder.
    """
    def __init__(self, roots, command, run_id):
        self.roots = sorted({Path(root) for root in roots}, key=str)
        self.command, self.run_id = command, run_id
        self.held = []

    def __enter__(self):
        try:
            for root in self.roots:
                root.mkdir(parents=True, exist_ok=True)
                lock = ProjectLock(root / LOCK_NAME)
                try:
                    lock.__enter__()
                except RuntimeError:
                    raise LockBusy(f"{root} is locked by {_holder(root)}") from None
                self.held.append((root, lock))
                fsutil.atomic_json(root / OWNER_NAME, {"host": socket.gethostname(), "pid": os.getpid(),
                                                       "command": self.command, "run_id": self.run_id,
                                                       "started_utc": time.time()})
        except BaseException:
            self._release()
            raise
        return self

    def __exit__(self, *args):
        self._release()

    def _release(self):
        while self.held:
            root, lock = self.held.pop()
            (root / OWNER_NAME).unlink(missing_ok=True)
            lock.__exit__(None, None, None)
