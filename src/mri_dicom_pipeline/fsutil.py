"""Filesystem primitives: atomic JSON, hashing, ownership checks and publication."""

from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(".tmp_" + uuid.uuid4().hex[:12])
    try:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


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
    if path.exists():
        shutil.rmtree(path)


def publish_tree(stage, destination, ownership_root):
    """Copy to a sibling, then swap; retain the previous version until committed."""
    destination = owned(destination, ownership_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep swap names short: nested files may already approach Windows limits.
    incoming = destination.with_name(".incoming_" + uuid.uuid4().hex[:12])
    previous = destination.with_name(".previous_" + uuid.uuid4().hex[:12])
    try:
        try:
            Path(stage).rename(incoming)
        except OSError as e:
            if e.errno != errno.EXDEV and getattr(e, "winerror", None) != 17:
                raise
            shutil.copytree(stage, incoming)
        if destination.exists():
            destination.rename(previous)
        try:
            incoming.rename(destination)
        except BaseException:
            if previous.exists():
                previous.rename(destination)
            raise
        remove_tree(previous, ownership_root)
    finally:
        remove_tree(incoming, ownership_root)
