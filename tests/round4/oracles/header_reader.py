"""Independent DICOM header reader for round 4 (spec D-9, oracle rule 6).

Written by the round 4 testing agent from DICOM PS3.3 and PS3.5, ``scan-selection.md``
sections 5.1 and 5.3, and the interface notes, in a folder that holds only those documents.
It shares no code with the pipeline and imports no pipeline module.

For every series of one session it computes the normalized facts that ``select`` records
(``scan-selection.md`` 5.1), and the native sampling and stack facts of 5.3. It can compare
them with ``select``'s records using the tolerances of plan section 3.4.

Output rule (interface notes, section 5): the records hold only technical fields, counts,
vocabulary tokens and hash prefixes. They never hold a description, a name, a raw UID, a
date or a time of day, and nothing in this module prints one.

Readings. Where 5.1 or 5.3 needed interpreting, the reading used is listed in ``READINGS``
below, and every record names the readings that shaped its values (``readings``). Where no
reading is safe, the field's value is ``None`` and ``unclear`` gives the reason. The owner
settles both kinds (oracle rule 6).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import struct
import sys
from collections import Counter, defaultdict

import pydicom
from pydicom.errors import InvalidDicomError
from pydicom.multival import MultiValue

READER_VERSION = "r4-header-reader-1"

# Interpretations of scan-selection.md 5.1 and 5.3 that the text does not settle on its own.
READINGS = {
    "RD-01": "Files: every regular file below the session's dicom folder is read, recursively. "
             "A file pydicom refuses without force=True is read again with force=True and counted "
             "in 'forced'; one that still fails is counted in 'unreadable'.",
    "RD-02": "MR objects: Modality (0008,0060) equals 'MR'. Objects without Rows are skipped.",
    "RD-03": "Scalar series fields that are not in the stack key (for example SliceThickness, "
             "AcquisitionMatrix, Manufacturer) take the common value of the series' instances. "
             "When instances disagree the field is unclear ('varies_within_series').",
    "RD-04": "Stack key (see _stack_key): matrix size, exact pixel spacing, orientation cosines "
             "rounded to three decimals, exact TR, TI and flip angle, and the (0008,0008) tokens "
             "in their order minus the PART_TOKENS. Echo time and part stay outside the key, so a "
             "multi-echo or magnitude/phase series is one group. n_stacks counts distinct keys and "
             "mixed means more than one. (0021,1175) does not enter the key.",
    "RD-05": "Part of an instance: a PART_TOKENS token in (0008,0008), else the GE (0043,102F) "
             "value through GE_PART. An instance with no part evidence is counted as magnitude. "
             "Evidence that disagrees makes 'parts' unclear.",
    "RD-06": "Echoes: instances are grouped by EchoTime rounded to 1e-4 ms. With two or more "
             "groups te_ms is None and 'echoes' holds one entry per group in rising TE, each with "
             "the group's TE, its common EchoNumbers value and its instance count over all parts.",
    "RD-07": "Positions use the instances of the lowest TE whose part is mag. Distinct positions "
             "are projections onto the slice normal (row cosine x column cosine) that differ by "
             "more than 0.001 mm. step_mm is the median of the steps between consecutive distinct "
             "positions; with fewer than two positions step_mm falls back to SpacingBetweenSlices "
             "and is marked unclear.",
    "RD-08": "extent_mm follows 5.3: (n_positions - 1) x step_mm + thick_mm. It is None when "
             "SliceThickness is absent. span_mm (pos_max - pos_min) is given as an extra fact.",
    "RD-09": "uniform_spacing: max step - min step <= 0.05 mm + 1% of the median step. With fewer "
             "than two positions it is None and unclear.",
    "RD-10": "Time reference: over all MR series of the session, mixed ones too, take each "
             "series' first AcquisitionTime (its SeriesTime when no instance has one) and use the "
             "smallest. The three relative times are seconds after that reference. If a "
             "comparison is off by the same constant for a whole session, the two sides used "
             "different references: an unclear definition, not a defect.",
    "RD-11": "for_hash: the full SHA-256 hex digest of FrameOfReferenceUID is kept as for_sha256. "
             "5.1 does not state the prefix length, so for_hash compares as a prefix of it.",
    "RD-12": "desc_tokens: the upper-cased SeriesDescription and ProtocolName of each instance "
             "are cut into maximal runs of letters, digits and '*' (regular expression "
             "[A-Z0-9*]+); runs found in VOCABULARY form the set, and desc_dropped counts the "
             "others. Source of the cutting rule: production-hardening.md, implementation plan "
             "item 9 (classifier revision 2), accepted by the owner at G1 (2026-11-24).",
    "RD-13": "multiframe: NumberOfFrames > 1 on any instance, or SOPClassUID one of Enhanced MR "
             "Image (1.2.840.10008.5.1.4.1.1.4.1), Enhanced MR Color Image (...4.3) or Legacy "
             "Converted Enhanced MR Image (...4.4).",
    "RD-14": "image_type: the union of the ImageType (0008,0008) tokens and the ImageTypeText "
             "(0021,1175) tokens of every instance, upper-cased, empty tokens dropped. "
             "(0021,1175) is read by its raw tag, without checking its private creator.",
    "RD-15": "manufacturer: upper-cased; 'GE', 'SIEMENS' or 'PHILIPS' when the first word is one of "
             "them or starts with 'GE ' / 'SIEMENS' / 'PHILIPS'; otherwise the first word.",
    "RD-16": "ref_series_keys: series keys of SOP instances referenced in ReferencedImageSequence or "
             "SourceImageSequence that the session's header pass kept (MR, with Rows), the series "
             "itself included, sorted and unique.",
    "RD-17": "Native sampling (5.3) with phase_dir absent or not ROW/COL: both assignments are "
             "given (native_row_if_row and so on); native_row_mm and native_col_mm are None. "
             "AcquisitionMatrix with two nonzero frequency or two nonzero phase entries is "
             "unclear.",
    "RD-18": "Empty elements count as absent: a present element with an empty value gives None.",
}

PART_TOKENS = {"M": "mag", "P": "phase", "PHASE": "phase", "R": "real", "REAL": "real",
               "I": "imag", "IMAGINARY": "imag"}
GE_PART = {0: "mag", 1: "phase", 2: "real", 3: "imag"}
ENHANCED_MR = {"1.2.840.10008.5.1.4.1.1.4.1", "1.2.840.10008.5.1.4.1.1.4.3",
               "1.2.840.10008.5.1.4.1.1.4.4"}
TAG_IMAGE_TYPE_TEXT = 0x00211175
TAG_GE_PART = 0x0043102F

VOCABULARY = frozenset(
    "T1 T2 FLAIR MPRAGE RAGE BRAVO SPGR FSPGR IR TFE TFL SPACE CUBE VISTA 3D 2D VOL ISO "
    "AX AXIAL SAG SAGITTAL COR CORONAL OBL "
    "PRE POST GAD GADO CONTRAST C "
    "REFORMAT MPR MIP MINIP "
    "DWI DTI DIFFUSION TRACE ADC EADC SWI SWAN QSM PHA PHASE MAG "
    "REPEAT RPT REDO RETAKE MOCO".split()
)

# Fields a mixed record keeps (5.1).
MIXED_FIELDS = ("series_key", "series_number", "manufacturer", "model", "field_t",
                "n_instances", "n_stacks", "mixed")


# ---------------------------------------------------------------------------- helpers

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("ascii", "strict")).hexdigest()


def series_key_of(uid: str) -> str:
    """sha256(SeriesInstanceUID)[:20], as 5.1 defines the join key."""
    return sha256_hex(uid)[:20]


def _is_multi(value) -> bool:
    return isinstance(value, (list, tuple, MultiValue))


def _empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, bytes):
        return not value.strip(b"\x00 ")
    if isinstance(value, str):
        return not value.strip("\x00 ")
    if _is_multi(value):
        return len(value) == 0
    return False


def _value(ds, keyword):
    """The element value, or None when absent or empty (RD-18)."""
    if keyword not in ds:
        return None
    value = ds[keyword].value
    return None if _empty(value) else value


def _private_value(ds, tag):
    element = ds.get(tag)
    if element is None:
        return None
    value = element.value
    return None if _empty(value) else value


def _float(value):
    if value is None:
        return None
    if _is_multi(value):
        value = value[0] if len(value) else None
        if value is None:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    if value is None:
        return None
    if _is_multi(value):
        value = value[0] if len(value) else None
        if value is None:
            return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _str(value):
    if value is None:
        return None
    if _is_multi(value):
        value = "\\".join(str(v) for v in value)
    text = str(value).strip().strip("\x00").strip()
    return text or None


def _tokens(value) -> tuple:
    """Multi-valued CS as a tuple of upper-case tokens, empty tokens dropped."""
    if value is None:
        return ()
    if isinstance(value, bytes):
        value = value.decode("latin-1")
    if _is_multi(value):
        parts = [str(v) for v in value]
    else:
        parts = str(value).split("\\")
    out = []
    for part in parts:
        token = part.strip().strip("\x00").strip().upper()
        if token:
            out.append(token)
    return tuple(out)


def _values(value) -> tuple:
    """Multi-valued text (for example LO SoftwareVersions) as given: stripped, case kept."""
    if value is None:
        return ()
    if _is_multi(value):
        parts = [str(v) for v in value]
    else:
        parts = str(value).split("\\")
    return tuple(p.strip().strip("\x00").strip() for p in parts if p.strip().strip("\x00").strip())


def parse_tm(value):
    """DICOM TM (PS3.5 6.2) as seconds after midnight, or None. Never printed or stored."""
    text = _str(value)
    if not text:
        return None
    text = text.replace(":", "")
    try:
        hours = int(text[0:2])
        minutes = int(text[2:4]) if len(text) >= 4 else 0
        seconds = float(text[4:]) if len(text) > 4 else 0.0
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _ge_part(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        if len(value) >= 2:
            return struct.unpack("<h", value[:2])[0]
        return None
    return _int(value)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit(v):
    n = math.sqrt(_dot(v, v))
    return (v[0] / n, v[1] / n, v[2] / n) if n > 0 else None


def orientation_label(iop):
    """Plane label: whichever axis dominates the plane normal (x: SAG, y: COR, z: AX), or
    'oblique' when the dominant component is under 0.95 (scan-selection.md 5.1)."""
    if iop is None or len(iop) != 6:
        return None
    normal = _unit(_cross(tuple(iop[0:3]), tuple(iop[3:6])))
    if normal is None:
        return None
    comps = [abs(c) for c in normal]
    best = max(range(3), key=lambda i: comps[i])
    if comps[best] < 0.95:
        return "oblique"
    return ("SAG", "COR", "AX")[best]


def tokenize_description(text):
    return re.findall(r"[A-Z0-9*]+", (text or "").upper())


# ---------------------------------------------------------------------------- instance read

def _instance(ds, path):
    """Pull the fields the reader needs from one dataset. Raw UIDs, times and text stay here."""
    iop = _value(ds, "ImageOrientationPatient")
    ipp = _value(ds, "ImagePositionPatient")
    pixel = _value(ds, "PixelSpacing")
    matrix = _value(ds, "AcquisitionMatrix")
    refs = []
    for seq_kw in ("ReferencedImageSequence", "SourceImageSequence"):
        seq = ds.get(seq_kw)
        if seq:
            for item in seq:
                uid = _str(item.get("ReferencedSOPInstanceUID"))
                if uid:
                    refs.append(uid)
    descriptions = " ".join(t for t in (_str(_value(ds, "SeriesDescription")),
                                        _str(_value(ds, "ProtocolName"))) if t)
    image_type = _tokens(_value(ds, "ImageType"))
    return {
        "path": path,
        "series_uid": _str(_value(ds, "SeriesInstanceUID")),
        "sop_uid": _str(_value(ds, "SOPInstanceUID")),
        "sop_class": _str(_value(ds, "SOPClassUID")),
        "rows": _int(_value(ds, "Rows")),
        "cols": _int(_value(ds, "Columns")),
        "pixel": tuple(float(v) for v in pixel) if pixel is not None and _is_multi(pixel) and len(pixel) == 2 else None,
        "iop": tuple(float(v) for v in iop) if iop is not None and _is_multi(iop) and len(iop) == 6 else None,
        "ipp": tuple(float(v) for v in ipp) if ipp is not None and _is_multi(ipp) and len(ipp) == 3 else None,
        "tr": _float(_value(ds, "RepetitionTime")),
        "te": _float(_value(ds, "EchoTime")),
        "ti": _float(_value(ds, "InversionTime")),
        "flip": _float(_value(ds, "FlipAngle")),
        "etl": _int(_value(ds, "EchoTrainLength")),
        "image_type": image_type,
        "image_type_text": _tokens(_private_value(ds, TAG_IMAGE_TYPE_TEXT)),
        "ge_part": _ge_part(_private_value(ds, TAG_GE_PART)),
        "echo_number": _int(_value(ds, "EchoNumbers")),
        "series_number": _int(_value(ds, "SeriesNumber")),
        "manufacturer": _str(_value(ds, "Manufacturer")),
        "model": _str(_value(ds, "ManufacturerModelName")),
        "field_t": _float(_value(ds, "MagneticFieldStrength")),
        "software": _values(_value(ds, "SoftwareVersions")),
        "mr_acq": _str(_value(ds, "MRAcquisitionType")),
        "scan_seq": _tokens(_value(ds, "ScanningSequence")),
        "seq_var": _tokens(_value(ds, "SequenceVariant")),
        "scan_opt": _tokens(_value(ds, "ScanOptions")),
        "seq_name": _str(_value(ds, "SequenceName")),
        "thick": _float(_value(ds, "SliceThickness")),
        "sbs": _float(_value(ds, "SpacingBetweenSlices")),
        "acq_matrix": tuple(int(v) for v in matrix) if matrix is not None and _is_multi(matrix) and len(matrix) == 4 else None,
        "phase_dir": (_str(_value(ds, "InPlanePhaseEncodingDirection")) or "").upper() or None,
        "pct_phase_fov": _float(_value(ds, "PercentPhaseFieldOfView")),
        "for_uid": _str(_value(ds, "FrameOfReferenceUID")),
        "acq_time": parse_tm(_value(ds, "AcquisitionTime")),
        "series_time": parse_tm(_value(ds, "SeriesTime")),
        "bolus_time": parse_tm(_value(ds, "ContrastBolusStartTime")),
        "agent": _str(_value(ds, "ContrastBolusAgent")),
        "n_frames": _int(_value(ds, "NumberOfFrames")),
        "modality": _str(_value(ds, "Modality")),
        "refs": refs,
        "descriptions": descriptions,
    }


def _part_of(inst):
    """(part, conflict) for one instance (RD-05)."""
    from_tokens = {PART_TOKENS[t] for t in inst["image_type"] if t in PART_TOKENS}
    ge = GE_PART.get(inst["ge_part"]) if inst["ge_part"] is not None else None
    evidence = set(from_tokens)
    if ge is not None:
        evidence.add(ge)
    if len(evidence) > 1:
        return (sorted(from_tokens)[0] if from_tokens else ge), True
    if evidence:
        return evidence.pop(), False
    return "mag", False


def _stack_key(inst):
    iop = tuple(round(v, 3) + 0.0 for v in inst["iop"]) if inst["iop"] else None
    image_type = tuple(t for t in inst["image_type"] if t not in PART_TOKENS)
    return (inst["rows"], inst["cols"], inst["pixel"], iop, inst["tr"], inst["ti"], inst["flip"], image_type)


# ---------------------------------------------------------------------------- series record

def _common(values, field, unclear):
    """The common value, or None with field marked unclear when instances disagree (RD-03)."""
    distinct = set(values)
    if len(distinct) == 1:
        return values[0]
    if not distinct:
        return None
    unclear[field] = "varies_within_series"
    return None


def _manufacturer(raw):
    if raw is None:
        return None
    words = raw.upper().split()
    first = words[0] if words else ""
    if first in ("GE", "SIEMENS", "PHILIPS"):
        return first
    for name in ("SIEMENS", "PHILIPS"):
        if first.startswith(name):
            return name
    return first or None


def _positions(insts, sbs, thick, unclear, readings):
    readings.update({"RD-07", "RD-08", "RD-09"})
    with_ipp = [i for i in insts if i["ipp"] is not None and i["iop"] is not None]
    if not with_ipp:
        for f in ("n_positions", "pos_min", "pos_max", "step_mm", "extent_mm", "uniform_spacing"):
            unclear[f] = "no_position_or_orientation"
        return {"n_positions": None, "pos_min": None, "pos_max": None, "step_mm": None,
                "extent_mm": None, "span_mm": None, "uniform_spacing": None}
    iop = with_ipp[0]["iop"]
    normal = _unit(_cross(iop[0:3], iop[3:6]))
    projected = sorted(_dot(i["ipp"], normal) for i in with_ipp)
    distinct = []
    for p in projected:
        if not distinct or p - distinct[-1] > 0.001:
            distinct.append(p)
    steps = [b - a for a, b in zip(distinct, distinct[1:])]
    out = {"n_positions": len(distinct), "pos_min": distinct[0], "pos_max": distinct[-1],
           "span_mm": distinct[-1] - distinct[0]}
    if steps:
        step = statistics.median(steps)
        out["step_mm"] = step
        out["uniform_spacing"] = (max(steps) - min(steps)) <= 0.05 + 0.01 * step
    else:
        out["step_mm"] = sbs
        unclear["step_mm"] = "fewer_than_two_positions"
        out["uniform_spacing"] = None
        unclear["uniform_spacing"] = "fewer_than_two_positions"
    if thick is not None and out["step_mm"] is not None:
        out["extent_mm"] = (len(distinct) - 1) * out["step_mm"] + thick
    else:
        out["extent_mm"] = None
    return out


def native_sampling(rows, cols, pixel_mm, acq_matrix, phase_dir, thick_mm, step_mm):
    """5.3 native sampling. Returns a dict and a dict of unclear reasons (RD-17)."""
    out = {"native_row_mm": None, "native_col_mm": None, "native_row_if_row": None,
           "native_col_if_row": None, "native_row_if_col": None, "native_col_if_col": None,
           "native_slice_mm": thick_mm, "native_max_mm": None, "native_max_if_row": None,
           "native_max_if_col": None, "gap_mm": None, "interpolated_grid": None,
           "native_inplane": "unknown"}
    unclear = {}
    if step_mm is not None and thick_mm is not None:
        out["gap_mm"] = step_mm - thick_mm
    if acq_matrix is None or rows is None or cols is None or pixel_mm is None:
        return out, unclear
    freq = [v for v in acq_matrix[0:2] if v]
    phase = [v for v in acq_matrix[2:4] if v]
    if len(freq) != 1 or len(phase) != 1:
        unclear["native"] = "acquisition_matrix_entries"
        return out, unclear
    n_freq, n_phase = freq[0], phase[0]
    extent_along_row = pixel_mm[1] * cols    # field of view in the row direction
    extent_along_col = pixel_mm[0] * rows    # field of view in the column direction
    fov_row, fov_col = extent_along_row, extent_along_col
    row_if_row, col_if_row = fov_row / n_phase, fov_col / n_freq
    row_if_col, col_if_col = fov_row / n_freq, fov_col / n_phase
    out.update({"native_row_if_row": row_if_row, "native_col_if_row": col_if_row,
                "native_row_if_col": row_if_col, "native_col_if_col": col_if_col})

    def nmax(r, c):
        vals = [r, c] + ([thick_mm] if thick_mm is not None else [])
        return max(vals)

    out["native_max_if_row"] = nmax(row_if_row, col_if_row)
    out["native_max_if_col"] = nmax(row_if_col, col_if_col)
    if phase_dir == "ROW":
        out["native_row_mm"], out["native_col_mm"] = row_if_row, col_if_row
    elif phase_dir == "COL":
        out["native_row_mm"], out["native_col_mm"] = row_if_col, col_if_col
    if out["native_row_mm"] is not None:
        out["native_inplane"] = "known"
        out["native_max_mm"] = nmax(out["native_row_mm"], out["native_col_mm"])
        out["interpolated_grid"] = (pixel_mm[1] < 0.75 * out["native_row_mm"]
                                    or pixel_mm[0] < 0.75 * out["native_col_mm"])
    else:
        out["native_inplane"] = "both_assignments"
    return out, unclear


def _series_record(key, insts, sop_to_key):
    unclear = {}
    readings = {"RD-02", "RD-03", "RD-04", "RD-14", "RD-15", "RD-18"}
    groups = defaultdict(list)
    for inst in insts:
        groups[_stack_key(inst)].append(inst)
    n_stacks = len(groups)
    mixed = n_stacks > 1

    rec = {"series_key": key, "n_instances": len(insts), "n_stacks": n_stacks, "mixed": mixed}
    rec["series_number"] = _common([i["series_number"] for i in insts], "series_number", unclear)
    manufacturers = [_manufacturer(i["manufacturer"]) for i in insts]
    rec["manufacturer"] = _common(manufacturers, "manufacturer", unclear)
    rec["model"] = _common([i["model"] for i in insts], "model", unclear)
    rec["field_t"] = _common([i["field_t"] for i in insts], "field_t", unclear)
    if mixed:
        out = {k: rec[k] for k in MIXED_FIELDS}
        out["unclear"] = {k: v for k, v in unclear.items() if k in MIXED_FIELDS}
        out["readings"] = sorted(readings)
        return out

    first = insts[0]
    rec["rows"], rec["cols"], rec["pixel_mm"] = first["rows"], first["cols"], (list(first["pixel"]) if first["pixel"] else None)
    rec["tr_ms"], rec["ti_ms"], rec["flip_deg"] = first["tr"], first["ti"], first["flip"]
    rec["orientation"] = orientation_label(first["iop"])

    image_type = set()
    for inst in insts:
        image_type.update(inst["image_type"])
        image_type.update(inst["image_type_text"])
    rec["image_type"] = sorted(image_type) or None
    # ImageType value 1 of each instance (PS3.3 C.7.6.1.1.2: ORIGINAL or DERIVED), as a set.
    rec["image_type_value1"] = sorted({i["image_type"][0] for i in insts if i["image_type"]}) or None
    tokens = image_type
    rec["derived"] = any(i["image_type"][:1] == ("DERIVED",) for i in insts) or bool(
        tokens & {"REFORMATTED", "MPR", "MIP", "MIN IP", "MINIP", "MNIP", "PROJECTION IMAGE",
                  "ADC", "EADC", "TRACEW", "FA"})
    rec["reformat"] = bool(tokens & {"REFORMATTED", "MPR"})
    rec["projection"] = bool(tokens & {"MIP", "MIN IP", "MINIP", "MNIP", "PROJECTION IMAGE"})
    rec["localizer"] = "LOCALIZER" in tokens
    rec["mosaic"] = "MOSAIC" in tokens

    rec["software"] = sorted(set(t for i in insts for t in i["software"])) or None
    if len({i["software"] for i in insts}) > 1:
        unclear["software"] = "varies_within_series"
    for field, src in (("mr_acq", "mr_acq"), ("seq_name", "seq_name"), ("etl", "etl"),
                       ("thick_mm", "thick"), ("sbs_mm", "sbs"), ("acq_matrix", "acq_matrix"),
                       ("phase_dir", "phase_dir"), ("pct_phase_fov", "pct_phase_fov")):
        rec[field] = _common([i[src] for i in insts], field, unclear)
    if rec["mr_acq"] not in (None, "2D", "3D"):
        unclear["mr_acq"] = "value_not_2D_or_3D"
        rec["mr_acq"] = None
    if rec["acq_matrix"] is not None:
        rec["acq_matrix"] = list(rec["acq_matrix"])
    for field in ("scan_seq", "seq_var", "scan_opt"):
        common = _common([i[field] for i in insts], field, unclear)
        rec[field] = sorted(set(common)) if common else None  # absent or empty -> None (RD-18)

    for_uids = [i["for_uid"] for i in insts]
    for_uid = _common(for_uids, "for_hash", unclear)
    rec["for_sha256"] = sha256_hex(for_uid) if for_uid else None
    readings.add("RD-11")

    agents = [bool(i["agent"]) for i in insts]
    rec["contrast_agent"] = _common(agents, "contrast_agent", unclear)

    # Parts (RD-05) and echoes (RD-06).
    readings.update({"RD-05", "RD-06"})
    part_counts = Counter()
    for inst in insts:
        part, conflict = _part_of(inst)
        inst["_part"] = part
        if conflict:
            unclear["parts"] = "conflicting_part_evidence"
        part_counts[part] += 1
    rec["parts"] = dict(sorted(part_counts.items()))
    te_groups = defaultdict(list)
    for inst in insts:
        te_groups[None if inst["te"] is None else round(inst["te"], 4)].append(inst)
    if len(te_groups) == 1:
        rec["te_ms"] = insts[0]["te"]
        rec["echoes"] = None
    else:
        rec["te_ms"] = None
        if None in te_groups:
            unclear["echoes"] = "echo_time_missing_on_some_instances"
        echoes = []
        for te in sorted(t for t in te_groups if t is not None):
            members = te_groups[te]
            numbers = {m["echo_number"] for m in members}
            if len(numbers) > 1:
                unclear["echoes"] = "echo_number_varies_within_echo"
            echo_number = numbers.pop() if len(numbers) == 1 else None
            echoes.append({"te_ms": members[0]["te"], "echo_number": echo_number,
                           "n_instances": len(members)})
        rec["echoes"] = echoes

    # Positions from the first echo's magnitude instances (RD-07).
    lowest = min((t for t in te_groups if t is not None), default=None)
    first_echo = te_groups[lowest]
    mags = [i for i in first_echo if i["_part"] == "mag"]
    if not mags:
        unclear["positions"] = "no_magnitude_instances"
        mags = first_echo
    rec.update(_positions(mags, rec["sbs_mm"], rec["thick_mm"], unclear, readings))

    # Times are filled in at session level (RD-10); keep raw values in private keys.
    acq_times = [i["acq_time"] for i in insts if i["acq_time"] is not None]
    series_time = _common([i["series_time"] for i in insts], "series_rel_s", unclear)
    bolus = _common([i["bolus_time"] for i in insts], "bolus_rel_s", unclear)
    rec["_acq_abs"] = min(acq_times) if acq_times else series_time
    rec["_series_abs"] = series_time
    rec["_bolus_abs"] = bolus

    # References (RD-16), description tokens (RD-12), multiframe (RD-13).
    readings.update({"RD-12", "RD-13", "RD-16"})
    refs = sorted({sop_to_key[u] for i in insts for u in i["refs"] if u in sop_to_key})
    rec["ref_series_keys"] = refs
    kept, dropped = set(), 0
    for text in {i["descriptions"] for i in insts}:
        for token in tokenize_description(text):
            if token in VOCABULARY:
                kept.add(token)
            else:
                dropped += 1
    rec["desc_tokens"] = sorted(kept)
    rec["desc_dropped"] = dropped
    rec["multiframe"] = any((i["n_frames"] or 0) > 1 for i in insts) or any(
        i["sop_class"] in ENHANCED_MR for i in insts)

    nat, nat_unclear = native_sampling(rec["rows"], rec["cols"], first["pixel"], rec["acq_matrix"],
                                       rec["phase_dir"], rec["thick_mm"], rec["step_mm"])
    readings.add("RD-17")
    rec.update(nat)
    unclear.update(nat_unclear)
    rec["unclear"] = unclear
    rec["readings"] = sorted(readings)
    return rec


# ---------------------------------------------------------------------------- session

def read_session(dicom_dir):
    """Read every DICOM file below ``dicom_dir`` and return the session's series facts.

    Returns ``{"reader_version", "counts": {...}, "series": {series_key: record}}``. No value
    in the result is a raw UID, a description, a name, a date or a time of day.
    """
    counts = Counter()
    instances = []
    for root, _dirs, files in os.walk(dicom_dir):
        for name in sorted(files):
            path = os.path.join(root, name)
            counts["files"] += 1
            try:
                try:
                    ds = pydicom.dcmread(path, stop_before_pixels=True)
                except InvalidDicomError:
                    ds = pydicom.dcmread(path, stop_before_pixels=True, force=True)
                    if "SOPInstanceUID" not in ds and "Modality" not in ds:
                        counts["unreadable"] += 1
                        continue
                    counts["forced"] += 1
            except Exception:  # noqa: BLE001 - any read failure is counted, never printed
                counts["unreadable"] += 1
                continue
            inst = _instance(ds, path)
            if inst["modality"] != "MR":
                counts["skipped_non_mr"] += 1
                continue
            if inst["rows"] is None:
                counts["skipped_no_rows"] += 1
                continue
            if not inst["series_uid"]:
                counts["skipped_no_series_uid"] += 1
                continue
            instances.append(inst)
    by_series = defaultdict(list)
    sop_to_key = {}
    for inst in instances:
        key = series_key_of(inst["series_uid"])
        by_series[key].append(inst)
        if inst["sop_uid"]:
            sop_to_key[inst["sop_uid"]] = key
    records = {}
    for key in sorted(by_series):
        insts = sorted(by_series[key], key=lambda i: i["path"])
        records[key] = _series_record(key, insts, sop_to_key)

    # Session time reference (RD-10): earliest series acquisition time, mixed series included.
    acq_all = []
    for key, insts in by_series.items():
        times = [i["acq_time"] for i in insts if i["acq_time"] is not None]
        if times:
            acq_all.append(min(times))
        else:
            st = [i["series_time"] for i in insts if i["series_time"] is not None]
            if st:
                acq_all.append(min(st))
    reference = min(acq_all) if acq_all else None
    for rec in records.values():
        if rec.get("mixed"):
            continue
        rec["readings"] = sorted(set(rec["readings"]) | {"RD-10"})
        for field, raw in (("acq_rel_s", "_acq_abs"), ("series_rel_s", "_series_abs"),
                           ("bolus_rel_s", "_bolus_abs")):
            value = rec.pop(raw)
            rec[field] = None if value is None or reference is None else value - reference
    counts["instances"] = len(instances)
    counts["series"] = len(records)
    return {"reader_version": READER_VERSION, "counts": dict(counts), "series": records}


# ---------------------------------------------------------------------------- comparison (plan 3.4)

EXACT_INT = ("series_number", "rows", "cols", "n_positions", "n_instances", "n_stacks", "etl")
EXACT_STR = ("manufacturer", "model", "mr_acq", "seq_name", "phase_dir", "orientation")
AS_SET = ("image_type", "scan_seq", "seq_var", "scan_opt", "software", "desc_tokens")
TOLERANCE = {"tr_ms": 0.01, "te_ms": 0.01, "ti_ms": 0.01, "flip_deg": 0.01, "field_t": 0.001,
             "thick_mm": 0.001, "sbs_mm": 0.001, "step_mm": 0.001, "pos_min": 0.01,
             "pos_max": 0.01, "extent_mm": 0.01, "acq_rel_s": 0.01, "series_rel_s": 0.01,
             "bolus_rel_s": 0.01, "pct_phase_fov": 0.01}
EXACT_BOOL = ("contrast_agent", "uniform_spacing", "multiframe", "mixed")
COMPARED_FIELDS = (EXACT_INT + EXACT_STR + AS_SET + tuple(TOLERANCE) + EXACT_BOOL
                   + ("pixel_mm", "acq_matrix", "for_hash", "parts", "echoes"))


def _cmp_value(field, mine, theirs):
    """True when equal under plan 3.4, False otherwise."""
    if field in AS_SET:
        # An absent token list and an empty one carry the same information.
        return set(mine or ()) == set(theirs or ())
    if mine is None or theirs is None:
        return mine is None and theirs is None
    if field in EXACT_INT:
        return int(mine) == int(theirs)
    if field in EXACT_STR:
        return str(mine) == str(theirs)
    if field in TOLERANCE:
        return abs(float(mine) - float(theirs)) <= TOLERANCE[field] + 1e-12
    if field in EXACT_BOOL:
        return bool(mine) == bool(theirs)
    if field == "pixel_mm":
        return len(mine) == len(theirs) and all(abs(float(a) - float(b)) <= 0.0001 + 1e-12 for a, b in zip(mine, theirs))
    if field == "acq_matrix":
        return [int(v) for v in mine] == [int(v) for v in theirs]
    if field == "for_hash":
        return len(str(theirs)) >= 8 and str(mine).startswith(str(theirs))
    if field == "parts":
        return {k: int(v) for k, v in mine.items() if int(v)} == {k: int(v) for k, v in theirs.items() if int(v)}
    if field == "echoes":
        if len(mine) != len(theirs):
            return False
        for a, b in zip(sorted(mine, key=lambda e: e["te_ms"]), sorted(theirs, key=lambda e: e["te_ms"])):
            if abs(float(a["te_ms"]) - float(b["te_ms"])) > 0.01 + 1e-12:
                return False
            if a.get("echo_number") != b.get("echo_number") or int(a["n_instances"]) != int(b["n_instances"]):
                return False
        return True
    raise KeyError(field)


def compare_series(mine, theirs, fields=None):
    """Compare one reader record with ``select``'s record for the same series (plan 3.4).

    ``theirs`` must be flat, with the plan 3.4 field names. Returns a list of
    ``{"field", "result", "reader", "select"}`` where result is ``equal``, ``mismatch``,
    ``unclear`` (the reader marked the field unclear), or ``absent`` (a field one side lacks).
    """
    out = []
    mine_values = dict(mine)
    if "for_sha256" in mine_values:
        mine_values["for_hash"] = mine_values["for_sha256"]
    unclear = mine.get("unclear", {})
    wanted = fields or COMPARED_FIELDS
    if mine.get("mixed"):
        wanted = [f for f in wanted if f in MIXED_FIELDS]
    for field in wanted:
        if field not in mine_values and field not in theirs:
            continue
        if field not in mine_values or field not in theirs:
            out.append({"field": field, "result": "absent", "reader": mine_values.get(field),
                        "select": theirs.get(field)})
            continue
        if field in unclear or (field in ("n_positions", "pos_min", "pos_max", "step_mm",
                                          "extent_mm") and "positions" in unclear):
            result = "unclear"
        else:
            result = "equal" if _cmp_value(field, mine_values[field], theirs[field]) else "mismatch"
        out.append({"field": field, "result": result, "reader": mine_values[field],
                    "select": theirs[field]})
    return out


# ---------------------------------------------------------------------------- command line

SUMMARY_FIELDS = ("series_number", "n_instances", "n_stacks", "mixed", "manufacturer", "mr_acq",
                  "orientation", "rows", "cols", "n_positions", "tr_ms", "te_ms", "ti_ms",
                  "flip_deg", "thick_mm", "step_mm", "extent_mm", "acq_rel_s", "contrast_agent")


def _fmt(value):
    if isinstance(value, float):
        return f"{value:.3f}"
    return "" if value is None else str(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Independent header reader (round 4). Prints "
                                     "technical fields and counts only.")
    parser.add_argument("dicom_dir")
    parser.add_argument("--json", help="write the full records to this file")
    args = parser.parse_args(argv)
    result = read_session(args.dicom_dir)
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as f:
            json.dump(result, f, indent=1, sort_keys=True)
    print("counts:", json.dumps(result["counts"], sort_keys=True))
    print("\t".join(("key8",) + SUMMARY_FIELDS + ("unclear",)))
    for key, rec in result["series"].items():
        row = [key[:8]] + [_fmt(rec.get(f)) for f in SUMMARY_FIELDS] + [",".join(sorted(rec.get("unclear", {})))]
        print("\t".join(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
