"""BIDS export of the selection decisions (design: BIDS output, sections 4 and 5).

One plan per visit names every output before anything is copied: path, entities, sidecar
additions and an export status (standard, conventional or omitted with a reason). The publish
step in selection.py copies from the plan. After a publish run, the dataset files
(participants, sessions, description, README, .bidsignore, filter file) are rebuilt from the
receipts of every committed visit in the root.
"""

from __future__ import annotations

from collections import Counter
import itertools
import json
import math
import os
from pathlib import Path
import re
import uuid

from . import __version__, fsutil, nifti, receipts
from .selection_rules import selection_role

EXPORTER_REVISION = 1
# The newest release the current validator (bids-validator-deno 3.0.2) knows; 1.11.2 only fixed the text.
BIDS_VERSION = "1.11.1"
MARKER = ".bids_export.json"
MANIFEST = ".selection.json"  # hidden from the validator
LABEL = re.compile(r"[A-Za-z0-9]+\Z")
NOT_EXPORTED = frozenset({"LOCALIZER", "PERFUSION"})
ENTITY_ORDER = ("acq", "ce", "rec", "dir", "run", "echo", "part")  # after sub and ses
_ANAT = frozenset({"acq", "ce", "rec", "run", "echo", "part"})
_DWI = frozenset({"acq", "rec", "dir", "run", "part"})
ALLOWED = {("anat", s): _ANAT for s in ("T1w", "T2w", "FLAIR", "T2starw", "MEGRE", "angio", "swi", "minIP", "QSM")}
ALLOWED.update({("dwi", s): _DWI for s in ("dwi", "ADC", "expADC", "trace")})
ALLOWED[("fmap", "epi")] = frozenset({"acq", "ce", "rec", "dir", "run", "part"})
REQUIRED = {("anat", "MEGRE"): {"echo"}, ("fmap", "epi"): {"dir"}}
# No released suffix: these files are listed in .bidsignore (4.1, 4.2).
CONVENTIONAL = frozenset({"swi", "minIP", "QSM"})
INGREDIENTS = {"iodine": "IODINE", "gadolinium": "GADOLINIUM", "carbondioxide": "CARBON DIOXIDE",
               "barium": "BARIUM", "xenon": "XENON", "unknown": "UNKNOWN"}
STRUCTURAL = {"T1": "T1w", "T1_POST": "T1w", "T2": "T2w", "FLAIR": "FLAIR"}
GRE_LIKE = {"SWI": "swi", "QSM": "qsm", "GRE": None}  # acquisition role -> acq label
SCANS_JSON = {
    "source_nifti": {"Description": "The converted file this copy was made from, relative to the Niftis root"},
    "role": {"Description": "Modality assigned by mri-dicom-pipeline select"},
    "fate": {"Description": "Why the file was published: selected, selected_derived, companion or kept"},
    "candidate_id": {"Description": "Acquisition or derived-group ID in selection.csv and selection_volumes.csv"}}
SESSIONS_JSON = {"source_sessions": {
    "Description": "The prepared sessions (one ZIP each) merged into this session; sessions on one date are one visit"}}
FILTER = {"t1w": {"ceagent": None}}
IGNORE_START = "# >>> mri-dicom-pipeline: this block is rewritten by select --bids --publish"
IGNORE_END = "# <<< mri-dicom-pipeline"
IGNORED = ("selection.csv", "selection_volumes.csv", "**/*_swi.*", "**/*_minIP.*", "**/*_QSM.*")
README_NAMES = ("README", "README.md", "README.txt", "README.rst")


class PlanError(ValueError):
    """A visit's outputs can't be named: the visit is refused (a mapping problem, not a data problem)."""


# ---- the plan (4.1-4.5) ------------------------------------------------------------------------------------

def plan_visit(subject, label, chosen, fates, decisions, agent, grid):
    """One entry per chosen volume, in order: what it becomes in the BIDS tree (4.4).

    chosen: the volumes the default layout copies (no extras); grid(volume): (dims, rows) of its
    NIfTI or None. Entry keys: volume, status (standard, conventional, omitted), reason, datatype,
    suffix, entities, extra (sidecar additions), path (inside the session folder), fate, candidate_id.
    """
    if not LABEL.fullmatch(subject):
        raise PlanError(f"subject ID {subject!r} is not a BIDS label (letters and digits only)")
    entries = [_entry(v, fates[v], agent) for v in chosen]
    _parts(entries)
    _echoes(entries)
    for e in _live(entries):
        v = e["volume"]
        if e["suffix"] == "dwi" and not ({"bval", "bvec"} <= {c["kind"] for c in v.companions}
                                         and v.gradients and not v.gradients.get("mismatch")):
            _omit(e, "missing_gradients")
    winner = decisions["DWI"]["winner"]
    target = next((e for e in entries if winner is not None and e["volume"] is getattr(winner, "primary", None)),
                  None)
    for e in _live(entries):
        if e["suffix"] == "epi":
            reason = _fieldmap_problem(e["volume"], target, grid)
            if reason:
                _omit(e, reason)
    _unlinked(entries)  # after every omission: an omitted scan is no source
    _runs(entries)
    for e in _live(entries):
        e["path"] = f"{e['datatype']}/sub-{subject}_ses-{label}{_name(e)}.nii.gz"
    for e in _live(entries):
        if e["suffix"] == "epi":
            e["extra"]["IntendedFor"] = [f"bids::sub-{subject}/ses-{label}/{target['path']}"]
    _check(entries)
    return entries


def _live(entries):
    return [e for e in entries if e["status"] != "omitted"]


def _omit(e, reason):
    e.update(status="omitted", reason=reason, path="")
    return e


def _set(e, datatype, suffix, extra=None, **entities):
    e.update(datatype=datatype, suffix=suffix, entities={k: x for k, x in entities.items() if x},
             extra=dict(extra or {}), status="conventional" if suffix in CONVENTIONAL else "standard")
    return e


def _entry(v, fate, agent):
    e = {"volume": v, "status": "omitted", "reason": "", "datatype": "", "suffix": "", "entities": {}, "extra": {},
         "path": "", "fate": fate["fate"], "candidate_id": fate["candidate_id"], "gre": False}
    # Runs are numbered per acquisition: a derived group runs with its parent, a kept volume with its series.
    if v.acq is not None:
        e["run_key"], e["family"] = v.acq.id, selection_role(v.acq.role)
    elif v.group is not None:
        owner = v.group.parent or v.group
        e["run_key"], e["family"] = owner.id, selection_role(owner.role)
    else:
        e["run_key"], e["family"] = (v.series or {}).get("series_key") or v.vid, v.role
    role, native = v.role, v.kind == "native"
    if role is None or role in NOT_EXPORTED:
        return _omit(e, "role_not_exported")
    if fate["fate"] == "companion":  # a reverse phase-encoding b0 of the selected DWI (4.3)
        e["family"] = "fmap"
        direction = str(v.sidecar.get("PhaseEncodingDirection") or "")
        if v is not v.acq.primary:
            return _omit(e, "secondary_echo")
        if not re.fullmatch(r"[ijk]-?", direction):
            return _omit(e, "missing_readout_metadata")
        return _set(e, "fmap", "epi", acq="dwi", dir=direction.replace("-", "neg"))
    if role in STRUCTURAL or role == "DWI" and native:
        if not native:
            return _omit(e, "no_bids_mapping")
        if v.acq is not None and v is not v.acq.primary:
            return _omit(e, "secondary_echo")
        if role == "DWI":
            return _set(e, "dwi", "dwi")
        if role == "T1_POST":
            extra = {"ContrastBolusIngredient": INGREDIENTS[agent]} if agent in INGREDIENTS else {}
            return _set(e, "anat", "T1w", extra, ce=agent)
        return _set(e, "anat", STRUCTURAL[role])
    if role == "DWI":
        return _set(e, "dwi", "trace") if "TRACEW" in v.image_type else _omit(e, "no_bids_mapping")
    if role in ("ADC", "EADC"):
        return _set(e, "dwi", "ADC" if role == "ADC" else "expADC")
    if role == "SWI_MINIP":
        return _set(e, "anat", "minIP", acq="swi")
    if role in ("SWI", "SWI_PHASE", "QSM", "GRE"):
        if native:
            owner = selection_role(v.acq.role) if v.acq is not None else selection_role(role)
            e["gre"] = True  # echo, part and suffix come from the whole acquisition (_echoes)
            return _set(e, "anat", "T2starw", acq=GRE_LIKE.get(owner, GRE_LIKE.get(selection_role(role))))
        if role == "SWI":
            return _set(e, "anat", "swi", acq="swi")
        if role == "QSM":  # Chimap means ppm, which nobody has verified for the vendor maps (4.1)
            return _set(e, "anat", "QSM", acq="qsm")
        return _omit(e, "no_bids_mapping")
    if role in ("MRA", "MRV"):
        if native or v.kind == "projection":
            return _set(e, "anat", "angio", acq="mrv" if role == "MRV" else None,
                        rec="mip" if v.kind == "projection" else None)
        return _omit(e, "no_bids_mapping")
    return _omit(e, "no_bids_mapping")


def _seconds(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _parts(entries):
    """part- wherever an exported image isn't plain magnitude, so a phase image never passes as one (4.4).

    A GRE-like acquisition is one set: when any of its images isn't magnitude, all of them carry part-.
    Other entries export one image each and are judged alone.
    """
    sets = {}
    for e in _live(entries):
        sets.setdefault(("gre", e["run_key"]) if e["gre"] else id(e), []).append(e)
    for members in sets.values():
        parts = {e["volume"].part for e in members}
        if parts == {"mag"}:
            continue
        if not parts <= {"mag", "phase", "real", "imag"}:
            for e in members:
                _omit(e, "part_unknown")
            continue
        for e in members:
            e["entities"]["part"] = e["volume"].part
            if e["volume"].part == "phase":
                e["extra"]["Units"] = "arbitrary"  # values unchanged, never claimed to be radians (4.5)


def _echoes(entries):
    """Echo entities of each GRE-like acquisition: one numbering shared by its parts (4.4).

    A multi-echo set is exported whole or not at all: a partial MEGRE set would misdescribe the scan.
    """
    sets = {}
    for e in _live(entries):
        if e["gre"]:
            sets.setdefault(e["run_key"], []).append(e)
    for members in sets.values():
        vols = [e["volume"] for e in members]
        parts = Counter(v.part for v in vols)
        times = [_seconds(v.sidecar.get("EchoTime")) for v in vols]
        if max(parts.values()) > 1:
            if any(t is None for t in times):
                for e in members:
                    _omit(e, "missing_echo_time")
                continue
            tes = [round(t * 1000, 2) for t in times]
            order = sorted(set(tes))
            numbers = [v.sidecar.get("EchoNumber") for v in vols]
            pairs = set(zip(tes, numbers))
            by_number = (all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in numbers)
                         and len(pairs) == len(order) == len({n for _, n in pairs}))
            for e, te, n in zip(members, tes, numbers):
                e["entities"]["echo"] = n if by_number else order.index(te) + 1
                e["suffix"] = "MEGRE"


def _unlinked(entries):
    """A derived map shares its source's entities only when linked to an acquisition exported here (4.1).

    Any other map (unlinked, or linked to an acquisition that wasn't chosen or was omitted) gets an acq-
    label of its own, so its name can't pass it off as a map of an exported scan with the same entities.
    Such maps share one run numbering: two of them can now have the same name whatever their roles.
    """
    sources = {e["run_key"] for e in _live(entries) if e["volume"].acq is not None}
    for e in _live(entries):
        if e["volume"].group is not None and e["run_key"] not in sources:
            acq = e["entities"].get("acq")
            e["entities"]["acq"] = acq + "Unlinked" if acq else "unlinked"
            e["family"] = "unlinked"


def same_grid(a, b):
    """True when two (dims, rows) grids match: same matrix, every corner within half a voxel (4.3)."""
    (dims_a, rows_a), (dims_b, rows_b) = a, b
    if list(dims_a) != list(dims_b):
        return False
    voxel = min(math.sqrt(sum(rows_a[i][j] ** 2 for i in range(3))) for j in range(3))
    for corner in itertools.product(*[(0, n - 1) for n in dims_a]):
        pa = [sum(r[j] * corner[j] for j in range(3)) + r[3] for r in rows_a]
        pb = [sum(r[j] * corner[j] for j in range(3)) + r[3] for r in rows_b]
        if math.dist(pa, pb) > voxel / 2 + 1e-6:
            return False
    return True


def _fieldmap_problem(v, target, grid):
    """Why a reverse b0 partner can't be a field map for the selected DWI, or '' (4.3)."""
    if target is None or target["status"] == "omitted":
        return "target_not_exported"
    dwi = target["volume"]
    for x in (v, dwi):
        if not re.fullmatch(r"[ijk]-?", str(x.sidecar.get("PhaseEncodingDirection") or "")) \
                or _seconds(x.sidecar.get("TotalReadoutTime")) is None:
            return "missing_readout_metadata"
    fa, fb = v.s("for_hash"), dwi.s("for_hash")
    a, b = grid(v), grid(dwi)
    if (fa and fb and fa != fb) or a is None or b is None or not same_grid(a, b):
        return "grid_mismatch"
    return ""


def _name(e):
    ents = e["entities"]
    return "".join(f"_{k}-{ents[k]}" for k in ENTITY_ORDER if k in ents) + f"_{e['suffix']}"


def _runs(entries):
    """run-<n> per acquisition, only where names would otherwise repeat (4.4)."""
    live = _live(entries)
    names, order = {}, {}
    for e in live:
        names.setdefault((e["datatype"], _name(e)), set()).add(e["run_key"])
        v = e["volume"]
        number = v.s("series_number") or v.sidecar.get("SeriesNumber") or 0
        order[e["run_key"]] = min(order.get(e["run_key"], (math.inf, "")), (number, v.relpath))
    needs = {k for keys in names.values() if len(keys) > 1 for k in keys}
    families = {}
    for e in live:
        if e["run_key"] in needs:
            families.setdefault(e["family"], set()).add(e["run_key"])
    run = {k: i for keys in families.values() for i, k in enumerate(sorted(keys, key=order.get), 1)}
    for e in live:
        if e["run_key"] in run:
            e["entities"]["run"] = run[e["run_key"]]


def _check(entries):
    """The plan's invariants; a violation refuses the visit (4.4)."""
    live, seen = _live(entries), {}
    for e in live:
        key = e["path"].lower()
        if key in seen:
            raise PlanError(f"two outputs would share the name {e['path']} ({seen[key]['volume'].relpath} and "
                            f"{e['volume'].relpath})")
        seen[key] = e
        allowed = ALLOWED.get((e["datatype"], e["suffix"]))
        if allowed is None or not set(e["entities"]) <= allowed \
                or not REQUIRED.get((e["datatype"], e["suffix"]), set()) <= set(e["entities"]) \
                or not all(LABEL.fullmatch(str(x)) for x in e["entities"].values()):
            raise PlanError(f"entities not allowed for {e['path']}")
        if e["entities"].get("part", "mag") != e["volume"].part:
            raise PlanError(f"{e['path']} is a {e['volume'].part} image without part-{e['volume'].part}")
        if e["suffix"] == "dwi" and not {"bval", "bvec"} <= {c["kind"] for c in e["volume"].companions}:
            raise PlanError(f"{e['path']} has no bval/bvec")
    paths = {e["path"] for e in live}
    for e in live:
        for uri in e["extra"].get("IntendedFor", ()):
            if uri.split("/", 2)[2] not in paths:
                raise PlanError(f"{e['path']} points to a file outside the plan")
    sets = {}
    for e in entries:
        if e["gre"]:
            sets.setdefault(e["run_key"], set()).add(e["status"] == "omitted")
    if any(len(states) > 1 for states in sets.values()):
        raise PlanError("an echo set would be exported in part")


def unexported_roles(entries, decisions, profile):
    """Listed modalities whose selection has no file in the plan (--complete-only, 5.2)."""
    live, missing = _live(entries), []
    for role in profile.get("modalities") or []:
        d = decisions[role]
        if d["outcome"] == "kept_all":
            found = any(selection_role(e["volume"].role) == role for e in live)
        elif d["winner"] is not None:
            members = {id(m) for m in d["winner"].members}
            found = any(id(e["volume"]) in members for e in live)
        else:
            continue  # selection itself found nothing; incomplete_roles reports it
        if not found:
            missing.append(role)
    return missing


def sidecar(raw, extra):
    """A BIDS sidecar: the converted sidecar's technical fields (no pipeline keys or dates) plus extra (4.5)."""
    data = json.loads(raw)
    return dict({k: data[k] for k in sorted(data) if k in nifti.TECH_FIELDS}, **extra)


def scans_tsv(entries):
    """sub-<id>_ses-<s>_scans.tsv: one row per standard data file; conventional files are left out (5.3)."""
    lines = ["filename\tsource_nifti\trole\tfate\tcandidate_id"]
    for e in sorted((e for e in entries if e["status"] == "standard"), key=lambda e: e["path"]):
        lines.append("\t".join([e["path"], e["volume"].relpath, e["volume"].role, e["fate"],
                                e["candidate_id"] or "n/a"]))
    return ("\n".join(lines) + "\n").encode()


# ---- the root and the dataset files (5.2, 5.3) -------------------------------------------------------------

def read_marker(root):
    """The root's export marker, or None when it has none."""
    path = Path(root) / MARKER
    if not path.exists():
        return None
    try:
        marker = fsutil.read_json(path)
    except (OSError, ValueError) as e:
        raise ValueError(f"Could not read {path}: {e}") from None
    if not isinstance(marker, dict) or marker.get("layout") != "bids":
        raise ValueError(f"{path} is not a BIDS export marker")
    return marker


def check_root(root, marker):
    """A BIDS run writes only to an empty root or one this exporter made (5.2)."""
    root = Path(root)
    if marker is None and root.is_dir() and any(not p.name.startswith(".") for p in root.iterdir()):
        raise ValueError(f"{root} is not empty and was not made by --bids; choose an empty --output-root")


def write_marker(root, agent):
    fsutil.atomic_json(Path(root) / MARKER, {"layout": "bids", "exporter_revision": EXPORTER_REVISION,
                                             "bids_version": BIDS_VERSION, "contrast_agent": agent})


def _write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(".tmp_" + uuid.uuid4().hex[:12])
    try:
        temp.write_bytes(text.encode("utf-8"))
        fsutil.retry_locked(lambda: os.replace(temp, path))
    finally:
        temp.unlink(missing_ok=True)


def _create(path, text):
    if not path.exists():
        _write_text(path, text)


def _json(data):
    return json.dumps(data, indent=2, ensure_ascii=True, allow_nan=False) + "\n"


def merge_tsv(path, key, wanted, logger):
    """Add rows for new keys and set the package's own columns; every other row, column and value stays.

    wanted: {key value: {column: value}}. New cells of other columns get n/a.
    """
    header, rows = [key], []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        header = lines[0].split("\t") if lines else [key]
        if header[0] != key:
            logger.warning("Left %s unchanged: its first column is not %s", path, key)
            return
        rows = [line.split("\t") for line in lines[1:] if line.strip()]
    for columns in wanted.values():
        header += [c for c in columns if c not in header]
    table = [dict(zip(header, r + ["n/a"] * (len(header) - len(r)))) for r in rows]
    known = {r[key] for r in table}
    table += [{key: k} for k in sorted(wanted) if k not in known]
    for r in table:
        r.update(wanted.get(r[key], {}))
    text = "\t".join(header) + "\n" + "".join("\t".join(r.get(c, "n/a") for c in header) + "\n" for r in table)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        _write_text(path, text)


def _bidsignore(path):
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if IGNORE_START in lines and IGNORE_END in lines[lines.index(IGNORE_START):]:
        start = lines.index(IGNORE_START)
        end = lines.index(IGNORE_END, start)
        lines = lines[:start] + lines[end + 1:]
    text = "\n".join([IGNORE_START, *IGNORED, IGNORE_END, *lines]) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        _write_text(path, text)


def readme(agent):
    return f"""# BIDS export

Made by mri-dicom-pipeline {__version__} (`select --bids --publish`) from its scan selection: one
acquisition per modality and visit. Sessions are visit dates; scans of one subject on one date are one session.

## Included

* `anat`: T1w, post-contrast T1w (`ce-{agent}`), T2w, FLAIR, T2starw and MEGRE (SWI, QSM source and
  GRE images), angio (MRA; `acq-mrv` for MRV, `rec-mip` for projections).
* `dwi`: dwi with bval/bvec, and the scanner's ADC, expADC and trace maps.
* `fmap`: `acq-dwi_..._epi` field maps from reverse phase-encoding b0 scans, only where the scan and the DWI
  share one voxel grid and both have a readout time. They have not been tested with QSIPrep.
* A scanner map (ADC, trace, MIP, processed SWI, QSM) whose source scan is not in this export has
  `unlinked` in its `acq-` label, for example `acq-unlinked_ADC`. Other maps share their source's entities.

## Not standard BIDS (listed in `.bidsignore`)

* `*_acq-swi_swi` and `*_acq-swi_minIP`: the scanner's processed SWI and minimum-intensity projections.
  BIDS has no SWI names yet.
* `*_acq-qsm_QSM`: the scanner's QSM maps. BIDS defines `Chimap` in ppm, and the vendor scaling has not been
  verified.

## Not exported

Localizers, perfusion, unclassified scans and alternatives that were not selected. There is no fMRI.
`selection_volumes.csv` lists every omission with its reason.

## Notes

* Contrast agent setting when this file was made: `{agent}`.
* For pre-contrast T1w only in NiPreps apps, pass `--bids-filter-file code/bids_filter_precontrast.json`.
* Images are not defaced. Deface a copy before sharing.
"""


def write_dataset_files(root, name, agent, logger):
    """Dataset files from the receipts of every committed visit in the root, not only this run's (5.3)."""
    root = Path(root)
    visits = {}
    for path in sorted(root.glob(f"sub-*/ses-*/{receipts.RECEIPT}")):
        try:
            r = fsutil.read_json(path)
        except (OSError, ValueError):
            continue
        if isinstance(r, dict) and r.get("kind") == "select" and r.get("layout") == "bids" and r.get("complete"):
            visits.setdefault(r["subject_id"], {})[r["session"]] = r.get("sessions") or [r["session"]]
    merge_tsv(root / "participants.tsv", "participant_id", {f"sub-{s}": {} for s in visits}, logger)
    for subject, sessions in sorted(visits.items()):
        folder = root / f"sub-{subject}"
        merge_tsv(folder / f"sub-{subject}_sessions.tsv", "session_id",
                  {f"ses-{label}": {"source_sessions": ",".join(parts)} for label, parts in sessions.items()}, logger)
        _create(folder / f"sub-{subject}_sessions.json", _json(SESSIONS_JSON))
    description = root / "dataset_description.json"
    _create(description, _json({"Name": name, "BIDSVersion": BIDS_VERSION, "DatasetType": "raw",
                                "GeneratedBy": [{"Name": "mri-dicom-pipeline", "Version": __version__}]}))
    try:
        found = fsutil.read_json(description)
        if not isinstance(found, dict) or not {"Name", "BIDSVersion"} <= set(found):
            logger.warning("%s lacks the required Name or BIDSVersion", description)
    except (OSError, ValueError) as e:
        logger.warning("Could not read %s: %s", description, e)
    if not any((root / n).exists() for n in README_NAMES):
        _write_text(root / "README.md", readme(agent))
    _bidsignore(root / ".bidsignore")
    _create(root / "code" / "bids_filter_precontrast.json", _json(FILTER))
