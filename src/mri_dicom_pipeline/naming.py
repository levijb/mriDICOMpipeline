"""Name unnamed upload ZIPs from their subject folder and DICOM StudyDate (owner request, 2026-11-18).

A ZIP whose name is not ID_YYYYMMDD.zip, inside DicomUploads/Uploads/.../<subject ID>/,
is renamed in place before preparation plans its work:

* the subject is the ZIP's folder, which must be an ID in the reference CSV
  (headers cannot say: PatientID is a new random value per exam);
* the date is the StudyDate in its DICOM headers, matched to the subject's dates
  in the reference CSV: an exact match, or the one listed date within --date-tolerance days;
* a different study on a date that already has a session gets the next _N suffix,
  and the run says so; the same study gets the existing name, so preparation
  treats it as a duplicate or a conflict as usual.

Anything else is held with an issue and left unnamed. Each rename is appended to
Reports/renamed_uploads.csv. Preparation then checks every DICOM object as usual.
"""

from __future__ import annotations

import csv
from datetime import datetime
import hashlib
from pathlib import Path
import zipfile
import zlib

from . import fsutil, identity, prepare

NAME_LOG = "renamed_uploads.csv"
NAME_FIELDS = ["run_id", "subject_id", "session", "old_path", "new_path", "study_date", "listed_date", "note"]
CACHE = ".naming_cache.json"
# Key in the naming cache: the log rows of renames recorded before they are made, so a run killed between a
# rename and its log row leaves the row for the next run to write (R4-F-002).
PENDING = "pending_renames"
SAMPLE = 100  # DICOM headers read per ZIP; preparation later reads every object


def candidates(cfg):
    """Unnamed ZIPs inside a folder below DicomUploads/Uploads (never directly in it)."""
    root = cfg.uploads / "Uploads"
    found = []
    for path in prepare.discover(cfg):
        if not path.is_relative_to(root) or path.parent == root:
            continue
        try:
            identity.parse_name(path.name)
        except ValueError:
            found.append(path)
    return found


def _skipped(member):
    parts = member.filename.replace("\\", "/").split("/")
    return "ddv" in [p.lower() for p in parts] or Path(member.filename).suffix.lower() in prepare.SKIP_EXT


def zip_facts(path):
    """StudyDates and studies (hashed StudyInstanceUID -> StudyTime) from a sample of the ZIP's DICOM headers.

    Corrupt compressed data in a member is reported as zipfile.BadZipFile, like the ZIP's other damage (R4-F-001).
    """
    try:
        return _zip_facts(path)
    except zlib.error:
        raise zipfile.BadZipFile("corrupt compressed data in a member") from None


def _zip_facts(path):
    import pydicom
    dates, studies = set(), {}
    with zipfile.ZipFile(path) as archive:
        members = [m for m in archive.infolist() if not m.is_dir() and not _skipped(m)]
        for member in members[::max(1, len(members) // SAMPLE)]:
            with archive.open(member) as f:
                head = f.read(132)
            if head[128:132] != b"DICM" and head[:2] not in (b"\x02\x00", b"\x08\x00", b"\x00\x02", b"\x00\x08"):
                continue
            try:
                with archive.open(member) as f:
                    ds = pydicom.dcmread(f, stop_before_pixels=True, force=True,
                                         specific_tags=["StudyInstanceUID", "StudyDate", "StudyTime"])
            except Exception:
                continue  # preparation reports damaged objects with their position
            uid, date = str(ds.get("StudyInstanceUID", "")), str(ds.get("StudyDate", ""))
            if not uid:
                continue
            if len(date) == 8 and date.isdigit():
                dates.add(date)
            studies.setdefault(hashlib.sha256(uid.encode()).hexdigest(), str(ds.get("StudyTime", "")))
    return {"dates": sorted(dates), "studies": studies}


def _days(a, b):
    return abs((datetime.strptime(a, "%Y%m%d") - datetime.strptime(b, "%Y%m%d")).days)


def resolve_date(header_dates, listed, allow_unlisted, tolerance=prepare.DEFAULT_DATE_TOLERANCE,
                 subject="this subject"):
    """(date, note) for the ZIP name, or (None, reason to hold).

    tolerance: --date-tolerance, the gap preparation's date check accepts with a warning.
    """
    if not header_dates:
        return None, "No StudyDate in the DICOM headers; name the ZIP ID_YYYYMMDD.zip by hand"
    if len(header_dates) > 1:
        return None, (f"The DICOM headers hold {len(header_dates)} StudyDates ({', '.join(header_dates)}); "
                      "split the ZIP, or name it ID_YYYYMMDD.zip by hand")
    date = header_dates[0]
    try:
        datetime.strptime(date, "%Y%m%d")
    except ValueError:
        return None, f"The DICOM StudyDate {date} is not a valid date; name the ZIP ID_YYYYMMDD.zip by hand"
    if date in listed:
        return date, ""
    near = sorted(d for d in listed if _days(d, date) <= tolerance)
    if len(near) == 1:
        gap = _days(near[0], date)
        return near[0], f"StudyDate {date} is {gap} day{'s' * (gap != 1)} from the listed date {near[0]}"
    if len(near) > 1:
        return None, (f"StudyDate {date} is within {tolerance} days of {len(near)} listed dates for {subject} "
                      f"({', '.join(near)}); name the ZIP ID_YYYYMMDD.zip by hand")
    if allow_unlisted:
        return date, "StudyDate not in the reference CSV (--allow-unlisted-dates)"
    nearest = min(listed, key=lambda d: (_days(d, date), d), default=None)
    return None, (f"StudyDate {date} is not listed for {subject} in the reference CSV"
                  + (f"; the nearest listed date is {nearest} ({_days(nearest, date)} days away)" if nearest
                     else " (no dates listed)")
                  + "; update the reference CSV or use --allow-unlisted-dates")


def _session_studies(entry):
    """Hashed StudyInstanceUIDs of a prepared session, from its inventory or one extracted header."""
    try:
        inventory = fsutil.read_json(entry["extraction"]["inventory"])
        if "study_keys" in inventory:
            return set(inventory["study_keys"])
        import pydicom
        first = Path(entry["extraction"]["root"]) / inventory["files"][0]["name"]
        uid = str(pydicom.dcmread(first, stop_before_pixels=True, force=True,
                                  specific_tags=["StudyInstanceUID"]).get("StudyInstanceUID", ""))
        return {hashlib.sha256(uid.encode()).hexdigest()} if uid else set()
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return set()


class _Facts:
    """Header facts per ZIP, cached by path and file signature in Reports/.naming_cache.json."""
    def __init__(self, cfg):
        self.path = cfg.reports / CACHE
        try:
            self.data = fsutil.read_json(self.path)
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}
        pending = self.data.pop(PENDING, [])
        self.pending = [p for p in pending if isinstance(p, dict)] if isinstance(pending, list) else []
        self.used = set()

    def get(self, path):
        signature = fsutil.signature(path)
        cached = self.data.get(str(path))
        if cached and cached.get("signature") == signature:
            self.used.add(str(path))
            return cached
        facts = zip_facts(path)
        if fsutil.signature(path) != signature:
            raise prepare.HoldError("Upload changed while its headers were read; wait for the transfer to finish")
        self.data[str(path)] = dict(facts, signature=signature)
        self.used.add(str(path))
        return self.data[str(path)]

    def move(self, old, new):
        self.data[str(new)] = dict(self.data.pop(str(old)), signature=fsutil.signature(new))
        self.used.discard(str(old))
        self.used.add(str(new))

    def save(self, prune=True):
        """Write the cache: with prune, only the ZIPs this run looked at; pending renames always."""
        data = {k: v for k, v in sorted(self.data.items()) if not prune or k in self.used}
        if self.pending:
            data[PENDING] = self.pending
        fsutil.atomic_json(self.path, data)


def _taken(state, facts, named, sid, date, assigned):
    """{label: hashed studies (empty when unknown)} of sessions and named ZIPs already using this subject and date."""
    labels = {}
    for entry in state.data["sessions"].values():
        if entry.get("subject_id") == sid and entry.get("date") == date and entry.get("label"):
            labels[entry["label"]] = _session_studies(entry) if entry.get("status") == "ready" else set()
    for path in named:
        try:
            other_sid, other_date, label = identity.parse_name(path.name)
        except ValueError:
            continue
        if (other_sid, other_date) == (sid, date) and not labels.get(label):
            try:
                labels[label] = set(facts.get(path)["studies"])
            except (OSError, ValueError, RuntimeError, EOFError, zipfile.BadZipFile):
                labels.setdefault(label, set())
    for label, studies in assigned.get((sid, date), {}).items():
        labels[label] = labels.get(label, set()) | studies
    return labels


def _append_log(cfg, rows, logger):
    """Append rows to renamed_uploads.csv; False when it cannot be written (the rows stay pending)."""
    path = cfg.reports / NAME_LOG
    try:
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=NAME_FIELDS)
            if f.tell() == 0:
                writer.writeheader()
            writer.writerows(rows)
    except OSError as e:
        logger.error("Could not append to %s (is it open in another program?): %s; the renames were %s; the next "
                     "run logs them", path, e, rows)
        return False
    return True


def _finish_pending(cfg, facts, logger):
    """Log the renames a killed run made but did not log, and forget the ones it never made (R4-F-002).

    A pending rename was made when its new name exists and its old name does not. Its row is written unless
    the log already holds it (the run was killed after logging, before clearing the record).
    """
    if not facts.pending:
        return
    try:
        with (cfg.reports / NAME_LOG).open(newline="", encoding="utf-8") as f:
            logged = {(r.get("run_id"), r.get("old_path"), r.get("new_path")) for r in csv.DictReader(f)}
    except FileNotFoundError:
        logged = set()
    except OSError as e:
        logger.warning("Could not read %s to finish interrupted renames: %s; the next run tries again",
                       cfg.reports / NAME_LOG, e)
        return
    rows = []
    for row in facts.pending:
        old, new = cfg.uploads / str(row.get("old_path")), cfg.uploads / str(row.get("new_path"))
        if new.exists() and not old.exists() and (row.get("run_id"), row.get("old_path"), row.get("new_path")) \
                not in logged:
            rows.append({k: row.get(k, "") for k in NAME_FIELDS})
            logger.info("RENAMED %s -> %s (by an interrupted run; logged now)", row.get("old_path"), new.name)
    if not rows or _append_log(cfg, rows, logger):
        facts.pending = []
        facts.save(prune=False)


def name_uploads(cfg, state, ids, pairs, logger):
    """Rename unnamed upload ZIPs.

    Returns (paths preparation must leave alone this run, held [(subject, path, reason)],
    names [(subject, path, new name, note)], renamed or in a dry run proposed).
    """
    facts = _Facts(cfg)
    if not cfg.dry_run:
        _finish_pending(cfg, facts, logger)
    found = candidates(cfg)
    if not found:
        return set(), [], []
    named = [p for p in prepare.discover(cfg) if p not in set(found)]
    skip, holds, resolved, names = set(), [], [], []
    listed = {}
    for sid, date in pairs:
        listed.setdefault(sid, set()).add(date)
    for path in found:
        sid = path.parent.name
        issue_key = "prepare:" + str(path)
        try:
            if path.is_symlink():
                raise prepare.HoldError("Symbolic-link ZIPs are not accepted")
            if not identity.valid_id(sid) or sid not in ids:
                raise prepare.HoldError(f"Unnamed ZIP in a folder that is not a subject ID in the reference CSV "
                                        f"({sid!r}); put it in a folder named after its subject, or name it "
                                        "ID_YYYYMMDD.zip by hand")
            try:
                info = facts.get(path)
            except (zipfile.BadZipFile, RuntimeError, EOFError):
                raise ValueError("Damaged, encrypted or still-copying ZIP; it is read again at the next run") from None
            if not info["studies"]:
                raise prepare.HoldError("No DICOM objects with a StudyInstanceUID found; name the ZIP by hand")
            date, note = resolve_date(info["dates"], listed.get(sid, set()), cfg.allow_unlisted_dates,
                                      cfg.date_tolerance, sid)
            if date is None:
                raise prepare.HoldError(note)
            first_time = min(info["studies"].values()) if info["studies"] else ""
            resolved.append((sid, date, first_time, str(path), path, info, note))
        except (OSError, ValueError) as e:
            skip.add(path)
            holds.append((issue_key, sid, path, e))
    assigned, renamed, extra_sessions = {}, 0, 0
    for sid, date, _, _, path, info, note in sorted(resolved, key=lambda r: r[:4]):
        studies = set(info["studies"])
        labels = _taken(state, facts, named, sid, date, assigned)
        same = sorted(label for label, keys in labels.items() if keys & studies)
        if same:
            label = same[0]
            note = "; ".join(filter(None, [note, f"same study as session {label}"]))
        else:
            label, n = date, 1
            while label in labels:
                n += 1
                label = f"{date}_{n}"
            if n > 1:
                extra_sessions += 1
                note = "; ".join(filter(None, [note, f"a second session on this date: named _{n} (a different study "
                                                     f"from {sid}_{date})"]))
        target = path.parent / f"{sid}_{label}.zip"
        if target.exists():
            skip.add(path)
            holds.append(("prepare:" + str(path), sid, path, prepare.HoldError(
                f"{target.name} already exists in this folder; move one of the two ZIPs or name it by hand")))
            continue
        assigned.setdefault((sid, date), {}).setdefault(label, set()).update(studies)
        names.append((sid, path, target.name, note))
        relative_old, relative_new = path.relative_to(cfg.uploads).as_posix(), target.relative_to(cfg.uploads).as_posix()
        if cfg.dry_run:
            skip.add(path)
            logger.info("WOULD RENAME %s -> %s%s", relative_old, target.name, f" ({note})" if note else "")
            continue
        row = {"run_id": cfg.run_id, "subject_id": sid, "session": label, "old_path": relative_old,
               "new_path": relative_new, "study_date": info["dates"][0],
               "listed_date": date if date in listed.get(sid, set()) else "", "note": note}
        # On record before it is made, logged right after: a kill in between leaves the row to the next run.
        facts.pending.append(row)
        facts.save(prune=False)
        fsutil.retry_locked(lambda: path.rename(target))
        facts.move(path, target)
        renamed += 1
        (logger.warning if "second session" in note else logger.info)(
            "RENAMED %s -> %s%s", relative_old, target.name, f" ({note})" if note else "")
        if _append_log(cfg, [row], logger):
            facts.pending.remove(row)
        state.checkpoint({"clear_issues": ["prepare:" + str(path)]})
    for issue_key, sid, path, error in holds:
        logger.warning("Held %s: %s", path, error)
        if not cfg.dry_run:
            state.checkpoint({"issues": [prepare.hold_issue(issue_key, sid, "", error, path)]})
    if not cfg.dry_run:
        facts.save()
    if renamed:
        logger.info("Named %s upload ZIP(s) from their subject folder and StudyDate%s; see %s", renamed,
                    f", {extra_sessions} as a further session on a date that already had one" if extra_sessions else "",
                    cfg.reports / NAME_LOG)
    return skip, [(sid, path, str(error)) for _, sid, path, error in holds], names
