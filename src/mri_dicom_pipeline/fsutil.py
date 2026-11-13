"""Filesystem primitives: atomic JSON, hashing, ownership checks and publication."""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time
import uuid

# On Windows a virus scanner or the search indexer briefly opens files that
# were just written. A rename, replace, delete or read of them, or of a folder
# that holds them, then fails with Access denied or a sharing violation; both
# are PermissionError. The operations below retry it for up to this long.
LOCKED_RETRY_SECONDS = 30


def retry_locked(action):
    """Return action(), retrying while it raises PermissionError, for up to LOCKED_RETRY_SECONDS."""
    deadline, delay = time.monotonic() + LOCKED_RETRY_SECONDS, 0.05
    while True:
        try:
            return action()
        except PermissionError:
            if time.monotonic() + delay > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(".tmp_" + uuid.uuid4().hex[:12])
    try:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        retry_locked(lambda: os.replace(temp, path))
    finally:
        temp.unlink(missing_ok=True)


def read_json(path):
    return json.loads(retry_locked(lambda: Path(path).read_text(encoding="utf-8")))


def signature(path):
    s = Path(path).stat()
    return [s.st_size, s.st_mtime_ns]


def sha256(path, stop=None):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            if stop is not None and stop.is_set():
                raise InterruptedError("Preparation interrupted")
            h.update(block)
    return h.hexdigest()


def owned(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"Refusing operation outside owned directory: {path}")
    return path


def remove_tree(path, root):
    path = owned(path, root)

    def make_writable(function, failed, _):
        # A read-only folder (POSIX) or file (Windows) inside the tree, for example outputs someone
        # protected: the tree is the pipeline's own, so make it writable and retry. Nothing a link points
        # to is touched (SB-28): links are skipped, POSIX files never need it (deleting an entry needs
        # write permission on its folder only), and a Windows file with other hard links is left alone.
        for target in {Path(failed), Path(failed).parent} - {path.parent}:
            try:
                info = target.lstat()
                if stat.S_ISDIR(info.st_mode):
                    target.chmod(info.st_mode | stat.S_IRWXU)
                elif os.name == "nt" and stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    target.chmod(info.st_mode | stat.S_IWRITE)
            except OSError:
                pass
        function(failed)

    hook = {"onexc": make_writable} if sys.version_info >= (3, 12) else {"onerror": make_writable}
    if path.exists():
        retry_locked(lambda: shutil.rmtree(path, **hook))


def swap_names(destination):
    """The sibling .incoming_ and .previous_ paths used to publish destination."""
    destination = Path(destination)
    # Keep swap names short: nested files may already approach Windows limits.
    return (destination.with_name(".incoming_" + uuid.uuid4().hex[:12]),
            destination.with_name(".previous_" + uuid.uuid4().hex[:12]))


def publish_tree(stage, destination, ownership_root, incoming, previous):
    """Copy to a sibling, then swap; retain the previous version until committed.

    The caller picks the swap names (swap_names) and records them in its
    intent first, so crash recovery knows where to look (Q12).
    Returns False when the new folder is in place but the previous one could
    not be deleted (for example a file in it is held open); the caller keeps
    the intent, and reconciliation retries the deletion on later runs.
    """
    destination = owned(destination, ownership_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    incoming, previous = Path(incoming), Path(previous)
    try:
        try:
            retry_locked(lambda: Path(stage).rename(incoming))
        except OSError as e:
            if e.errno != errno.EXDEV and getattr(e, "winerror", None) != 17:
                raise
            shutil.copytree(stage, incoming)
        if destination.exists():
            retry_locked(lambda: destination.rename(previous))
        try:
            retry_locked(lambda: incoming.rename(destination))
        except BaseException:
            if previous.exists():
                retry_locked(lambda: previous.rename(destination))
            raise
        try:
            remove_tree(previous, ownership_root)
        except OSError:
            return False
        return True
    finally:
        remove_tree(incoming, ownership_root)
