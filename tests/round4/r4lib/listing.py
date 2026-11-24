"""Tree listings and their differences (plan 2.1 item 8; R4-OPS-01, R4-OR-08, R4-PUB-05).

For every file a listing keeps the relative path (with '/'), the byte size and st_mtime_ns; it
also keeps st_mtime_ns of each session folder, because the pipeline's quick change detection
looks at folder times. ``diff`` compares two listings and sets aside changes that match the
allow-list (fnmatch patterns from the root, where ``*`` crosses '/'). Evidence receives only the
difference, with paths passed through the caller's coding function; complete listings stay in a
private folder.
"""

from __future__ import annotations

import fnmatch
import json
import os

SESSION_DIR_PATTERNS = ("DicomExtracted/Subjects/*/*", "Niftis/Subjects/*/*")


def _rel(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/")


def list_tree(root, session_patterns=SESSION_DIR_PATTERNS):
    """Walk ``root`` read-only. Symbolic links and junctions are listed, never followed."""
    files, session_dirs = {}, {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = _rel(root, dirpath)
        for name in dirnames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            full = os.path.join(dirpath, name)
            if any(fnmatch.fnmatchcase(rel, p) for p in session_patterns) and rel.count("/") == p_depth(session_patterns):
                session_dirs[rel] = os.stat(full, follow_symlinks=False).st_mtime_ns
        for name in filenames:
            full = os.path.join(dirpath, name)
            st = os.stat(full, follow_symlinks=False)
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            files[rel] = [st.st_size, st.st_mtime_ns]
    return {"files": files, "session_dirs": session_dirs}


def p_depth(patterns):
    return patterns[0].count("/")


def save(listing, path):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(listing, f, sort_keys=True)


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def allowed(rel, patterns):
    return any(fnmatch.fnmatchcase(rel, p) for p in patterns)


def diff(before, after, allow_patterns=(), forbidden_patterns=()):
    """Changes outside the allow-list, and allowed ones separately.

    Returns {"added", "removed", "changed", "session_dirs_changed", "allowed", "forbidden_present"}.
    ``forbidden_patterns`` lists files that must not exist afterwards (for example a lock owner
    file after a run that ended normally), whether or not they changed.
    """
    out = {"added": [], "removed": [], "changed": [], "session_dirs_changed": [], "allowed": [], "forbidden_present": []}
    b, a = before["files"], after["files"]
    for rel in sorted(set(a) - set(b)):
        (out["allowed"] if allowed(rel, allow_patterns) else out["added"]).append(rel)
    for rel in sorted(set(b) - set(a)):
        (out["allowed"] if allowed(rel, allow_patterns) else out["removed"]).append(rel)
    for rel in sorted(set(a) & set(b)):
        if a[rel] != b[rel]:
            (out["allowed"] if allowed(rel, allow_patterns) else out["changed"]).append(rel)
    bs, as_ = before["session_dirs"], after["session_dirs"]
    for rel in sorted(set(bs) | set(as_)):
        if bs.get(rel) != as_.get(rel):
            out["session_dirs_changed"].append(rel)
    for rel in sorted(a):
        if allowed(rel, forbidden_patterns):
            out["forbidden_present"].append(rel)
    return out


def unexplained(d):
    return d["added"] + d["removed"] + d["changed"] + d["session_dirs_changed"] + d["forbidden_present"]


def coded(d, code_path):
    """The difference with every path passed through ``code_path`` (for example a function that
    replaces subject IDs and dates by visit codes)."""
    return {k: [code_path(p) for p in v] for k, v in d.items()}
