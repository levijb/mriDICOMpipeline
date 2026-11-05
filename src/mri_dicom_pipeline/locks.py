"""Process locks that keep two commands from running on the same project."""

from __future__ import annotations

import os
from pathlib import Path


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
