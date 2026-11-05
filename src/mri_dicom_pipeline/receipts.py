"""Ownership receipts and the metadata-only cache check."""

from __future__ import annotations

from pathlib import Path

from . import config, fsutil


def fast_valid(entry, kind, audit=False):
    """O(directories), never O(DICOM instances), unless explicitly audited."""
    try:
        if entry.get("version") != config.VERSION or not entry.get("receipt"):
            return False
        if fsutil.signature(entry["receipt"]) != entry["receipt_signature"]:
            return False
        for path, expected in entry["directories"].items():
            if Path(path).stat().st_mtime_ns != expected:
                return False
        if audit:
            inventory = fsutil.read_json(entry["inventory"])
            for item in inventory["files"]:
                p = Path(entry["root"]) / item["name"]
                if fsutil.signature(p)[0] != item["size"] or fsutil.sha256(p) != item["sha256"]:
                    return False
        return True
    except (OSError, KeyError, ValueError):
        return False


def receipt_info(root, receipt, inventory):
    dirs = {str(p): p.stat().st_mtime_ns for p in root.rglob("*") if p.is_dir()}
    dirs[str(root)] = root.stat().st_mtime_ns
    return {"version": config.VERSION, "root": str(root), "receipt": str(receipt),
            "receipt_signature": fsutil.signature(receipt), "directories": dirs, "inventory": str(inventory)}
