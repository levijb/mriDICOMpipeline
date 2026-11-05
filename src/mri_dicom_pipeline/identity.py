"""Session identity: ZIP file-name grammar and the study roster."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from datetime import datetime


NAME = re.compile(r"^(?P<id>[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*)_(?P<date>[0-9]{8})(?:_(?P<part>[0-9]+))?\.zip$", re.I)
SAFE_ID = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*\Z")


def parse_name(name, legacy=False):
    if legacy:
        m = re.fullmatch(r"(.+)_([0-9]{6})\.zip", name, re.I)
        if m:
            old = m[2]
            d = datetime(2000 + int(old[4:]), int(old[:2]), int(old[2:4]))
            name = f"{m[1]}_{d:%Y%m%d}.zip"
    m = NAME.fullmatch(name)
    if not m:
        raise ValueError("Expected ID_YYYYMMDD.zip or ID_YYYYMMDD_2.zip")
    datetime.strptime(m["date"], "%Y%m%d")
    part = m["part"]
    if part and (int(part) < 2 or str(int(part)) != part):
        raise ValueError("Additional session suffix must be an integer >= 2, without leading zeros")
    label = m["date"] + ("_" + part if part else "")
    return m["id"], m["date"], label


def load_roster(path, subject_column="subject_id", date_column="scan_date"):
    pairs, ids, warnings = set(), set(), []
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not subject_column or not date_column or subject_column == date_column:
            raise ValueError("Subject and date column names must be nonempty and distinct")
        if len(reader.fieldnames or []) != len(set(reader.fieldnames or [])):
            raise ValueError("Reference CSV contains duplicate column names")
        if not {subject_column, date_column}.issubset(reader.fieldnames or []):
            raise ValueError(f"Reference CSV must contain {subject_column!r} and {date_column!r}")
        for line, row in enumerate(reader, 2):
            sid, raw = (row.get(subject_column) or "").strip(), (row.get(date_column) or "").strip()
            if not sid and not raw:
                continue
            if not SAFE_ID.fullmatch(sid):
                warnings.append(f"Reference row {line}: invalid {subject_column}; row ignored")
                continue
            ids.add(sid)
            if not raw:
                continue
            try:
                date_format = "%Y%m%d" if re.fullmatch(r"[0-9]{8}", raw) else "%Y-%m-%d" if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw) else "%m/%d/%Y"
                d = datetime.strptime(raw, date_format)
                pairs.add((sid, d.strftime("%Y%m%d")))
            except ValueError:
                warnings.append(f"Reference row {line}: invalid {date_column}; row ignored")
    if not pairs:
        raise ValueError("Reference CSV contains no valid subject/date pairs")
    return ids, pairs, warnings
