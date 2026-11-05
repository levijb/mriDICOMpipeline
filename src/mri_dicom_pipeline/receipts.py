"""Ownership receipts, the metadata-only cache check, and the adoption rule."""

from __future__ import annotations

from pathlib import Path

from . import config, fsutil

RECEIPT, INVENTORY = ".pipeline_complete.json", ".inventory.json"


def fast_valid(entry, kind, audit=False):
    """O(directories), never O(DICOM instances), unless explicitly audited."""
    try:
        if entry.get("version") != config.SCHEMA_VERSION or not entry.get("receipt"):
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
    return {"version": config.SCHEMA_VERSION, "root": str(root), "receipt": str(receipt),
            "receipt_signature": fsutil.signature(receipt), "directories": dirs, "inventory": str(inventory),
            "receipt_id": fsutil.read_json(receipt).get("receipt_id")}


def receipt_id(root):
    """The receipt_id of the receipt in root, or None if there is no readable receipt."""
    try:
        return fsutil.read_json(Path(root) / RECEIPT).get("receipt_id")
    except (OSError, ValueError, AttributeError):
        return None


def adoptable(root, kind, expected_digest):
    """The Q12 adoption rule, for a folder whose receipt state does not record.

    The receipt must match the prepared content (``archive_sha256`` for an
    extraction; ``complete`` and ``source_sha256`` for a conversion), and every
    inventory file must exist at its recorded size. Costs one stat per file.
    """
    root = Path(root)
    try:
        receipt, inventory = fsutil.read_json(root / RECEIPT), fsutil.read_json(root / INVENTORY)
        if receipt.get("version") != config.SCHEMA_VERSION:
            return False
        if kind == "extract":
            matches = receipt.get("archive_sha256") == expected_digest
        else:
            matches = receipt.get("complete") is True and receipt.get("source_sha256") == expected_digest
        return matches and all((root / item["name"]).stat().st_size == item["size"] for item in inventory["files"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def prepared_entry(sid, date, label, canonical, digest, inventory, destination, conversion):
    """The state entry for a session whose extraction is published at destination."""
    destination = Path(destination)
    return {"subject_id": sid, "date": date, "label": label, "status": "ready", "version": config.SCHEMA_VERSION,
            "archive": str(canonical), "archive_signature": fsutil.signature(canonical), "archive_sha256": digest,
            "content_sha256": inventory["content_sha256"], "n_dicom": inventory["n_dicom"],
            "n_series": inventory["n_series"], "dicom_bytes": inventory["dicom_bytes"],
            "pixel_bytes": inventory["pixel_bytes"],
            "extraction": receipt_info(destination, destination / RECEIPT, destination / INVENTORY),
            "conversion": conversion}


def conversion_record(destination, status, n_volumes, source_sha256):
    """The state record for a conversion published at destination."""
    destination = Path(destination)
    record = receipt_info(destination, destination / RECEIPT, destination / INVENTORY)
    record.update(status=status, output=str(destination), n_volumes=n_volumes, source_sha256=source_sha256)
    return record
