"""Session identity: ZIP file-name grammar and the study roster."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from datetime import datetime


NAME = re.compile(r"^(?P<id>[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*)_(?P<date>[0-9]{8})(?:_(?P<part>[0-9]+))?\.zip$", re.I)
SAFE_ID = re.compile(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*\Z")
# Windows device names cannot be file or folder names in any letter case.
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{n}" for p in ("COM", "LPT") for n in range(10)}


def valid_id(text):
    return bool(SAFE_ID.fullmatch(text)) and text.upper() not in RESERVED_NAMES


def parse_name(name):
    m = NAME.fullmatch(name)
    if not m:
        raise ValueError("Expected ID_YYYYMMDD.zip or ID_YYYYMMDD_2.zip")
    if not valid_id(m["id"]):
        raise ValueError("Subject ID is a reserved Windows device name")
    datetime.strptime(m["date"], "%Y%m%d")
    part = m["part"]
    if part and (int(part) < 2 or str(int(part)) != part):
        raise ValueError("Additional session suffix must be an integer >= 2, without leading zeros")
    label = m["date"] + ("_" + part if part else "")
    return m["id"], m["date"], label


def parse_key(text):
    """(subject, session label) from a SUBJECT/SESSION value such as S001/20260725_2."""
    sid, slash, label = str(text).partition("/")
    try:
        if not slash:
            raise ValueError("expected SUBJECT/SESSION, for example S001/20260725")
        parsed_sid, _, parsed_label = parse_name(f"{sid}_{label}.zip")
        if (parsed_sid, parsed_label) != (sid, label):
            raise ValueError("ambiguous subject ID")
    except ValueError as e:
        raise ValueError(f"Invalid session key {text!r}: {e}") from None
    return sid, label


def load_roster(path, subject_column="subject_id", date_column="scan_date"):
    pairs, ids, warnings, first_row = set(), set(), [], {}
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
            if not valid_id(sid):
                warnings.append(f"Reference row {line}: invalid {subject_column}; row ignored")
                continue
            ids.add(sid)
            first_row.setdefault(sid, line)
            if not raw:
                continue
            try:
                date_format = "%Y%m%d" if re.fullmatch(r"[0-9]{8}", raw) else "%Y-%m-%d" if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw) else "%m/%d/%Y"
                d = datetime.strptime(raw, date_format)
                pairs.add((sid, d.strftime("%Y%m%d")))
            except ValueError:
                warnings.append(f"Reference row {line}: invalid {date_column}; row ignored")
    # IDs that differ only by letter case would share folders on Windows and
    # macOS, so every spelling of such an ID is ignored, on every platform.
    spellings = {}
    for sid in ids:
        spellings.setdefault(sid.casefold(), []).append(sid)
    for group in spellings.values():
        if len(group) > 1:
            group.sort(key=first_row.get)
            ids.difference_update(group)
            pairs = {pair for pair in pairs if pair[0] not in group}
            listed = ", ".join(f"{sid!r} (row {first_row[sid]})" for sid in group)
            warnings.append(f"Reference {subject_column} values differ only by letter case: {listed}; all ignored")
    if not pairs:
        raise ValueError("Reference CSV contains no valid subject/date pairs")
    return ids, pairs, warnings
