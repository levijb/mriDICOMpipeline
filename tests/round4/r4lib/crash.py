"""Crash injection (plan 2.1 item 7, write steps in plan 3.3).

``run_with_crash`` starts the pipeline in a separate Python process whose file-system calls are
wrapped (os.replace, os.rename, shutil.move, shutil.rmtree, os.rmdir, and closing a file opened
for writing). As soon as the named step has completed, the wrapper drops ``<step>.fired`` into
the marker folder and kills the process with os._exit(137). If a test finds no marker file, the
injection never happened, and the test reports an error instead of a result.
"""

from __future__ import annotations

import os
import subprocess
import sys

STEPS = ("zip-renamed", "rename-logged", "naming-cache-saved", "staged", "moved-aside", "swapped",
         "previous-deleted", "marker-written", "participants-written", "sessions-written",
         "bidsignore-written")

CHILD = r'''
import builtins, io, os, re, shutil, sys
step, marker_dir, occurrence = sys.argv[1], sys.argv[2], int(sys.argv[3])
argv = sys.argv[4:]
_open, _replace, _rename, _rmtree, _rmdir, _move = builtins.open, os.replace, os.rename, shutil.rmtree, os.rmdir, shutil.move
count = [0]

def norm(p):
    return os.path.abspath(os.fspath(p)).replace("\\", "/")

def base(p):
    return os.path.basename(norm(p).rstrip("/"))

def fire():
    count[0] += 1
    if count[0] < occurrence:
        return
    with _open(os.path.join(marker_dir, step + ".fired"), "w") as f:
        f.write("fired\n")
        f.flush()
        os.fsync(f.fileno())
    os._exit(137)

def written(path):
    p = norm(path); b = base(path)
    return {
        "rename-logged": p.endswith("/Reports/renamed_uploads.csv"),
        "naming-cache-saved": p.endswith("/Reports/.naming_cache.json"),
        "marker-written": b == ".bids_export.json",
        "participants-written": b == "participants.tsv",
        "sessions-written": re.fullmatch(r"sub-.+_sessions\.tsv", b) is not None,
        "bidsignore-written": b == ".bidsignore",
    }.get(step, False)

def moved(src, dst):
    s, d = norm(src), norm(dst)
    if step == "zip-renamed":
        return (s.lower().endswith(".zip") and d.lower().endswith(".zip") and "/Uploads/" in s
                and "/Uploads/" in d and os.path.dirname(s) == os.path.dirname(d))
    if step == "staged":
        return base(d).startswith(".incoming_")
    if step == "moved-aside":
        return base(d).startswith(".previous_")
    if step == "swapped":
        return base(s).startswith(".incoming_") and not base(d).startswith(".")
    return written(dst)

def replace(src, dst, *a, **k):
    r = _replace(src, dst, *a, **k)
    if moved(src, dst):
        fire()
    return r

def rename(src, dst, *a, **k):
    r = _rename(src, dst, *a, **k)
    if moved(src, dst):
        fire()
    return r

def move(src, dst, *a, **k):
    r = _move(src, dst, *a, **k)
    if moved(src, dst):
        fire()
    return r

def rmtree(path, *a, **k):
    r = _rmtree(path, *a, **k)
    if step == "previous-deleted" and base(path).startswith(".previous_"):
        fire()
    return r

def rmdir(path, *a, **k):
    r = _rmdir(path, *a, **k)
    if step == "previous-deleted" and base(path).startswith(".previous_"):
        fire()
    return r

class Hooked:
    def __init__(self, f, path):
        self._f, self._path = f, path
    def __getattr__(self, name):
        return getattr(self._f, name)
    def __enter__(self):
        self._f.__enter__()
        return self
    def __exit__(self, *exc):
        r = self._f.__exit__(*exc)
        if exc[0] is None and written(self._path):
            fire()
        return r
    def __iter__(self):
        return iter(self._f)
    def close(self):
        self._f.close()
        if written(self._path):
            fire()

def hooked_open(file, mode="r", *a, **k):
    f = _open(file, mode, *a, **k)
    if isinstance(file, (str, bytes, os.PathLike)) and any(c in mode for c in "wax+") and written(file):
        return Hooked(f, file)
    return f

os.replace, os.rename, shutil.move, shutil.rmtree, os.rmdir = replace, rename, move, rmtree, rmdir
builtins.open = io.open = hooked_open
from mri_dicom_pipeline import cli
code = cli.main(argv)
sys.exit(code if isinstance(code, int) else 0)
'''


def run_with_crash(argv, step, marker_dir, occurrence=1, env=None, cwd=None, timeout=600):
    """Run ``mri_dicom_pipeline`` with ``argv`` (command and options) and crash after ``step``."""
    if step not in STEPS:
        raise ValueError(f"unknown crash point {step!r}")
    os.makedirs(marker_dir, exist_ok=True)
    return subprocess.run([sys.executable, "-c", CHILD, step, str(marker_dir), str(occurrence), *map(str, argv)],
                          capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout)


def fired(marker_dir, step):
    return os.path.exists(os.path.join(marker_dir, step + ".fired"))
