"""Scan selection: normalized header records and published-volume facts, cached per session (design: 5.1, 5.2, 7.3).

The header pass reads each extracted DICOM once, before pixel data, and keeps
technical fields only. Descriptions are reduced to vocabulary tokens and a
contrast hint in memory (D6); times of day become seconds relative to the
session's first series; UIDs are kept only as hash prefixes.
"""

from __future__ import annotations

from collections import Counter
import gzip
import hashlib
import math
from pathlib import Path
import re
import struct
from statistics import median

from . import fsutil, receipts
from .selection_identity import PART_TOKENS, description_phase, description_tokens, gradient_stats, orientation

CACHE_VERSION = 1
IMAGE_TYPE_TEXT = 0x00211175  # Siemens XA ImageTypeText
GE_COMPLEX = 0x0043102F       # GE magnitude/phase/real/imaginary flag
GE_PARTS = {0: "mag", 1: "phase", 2: "real", 3: "imag"}
SELECT_TAGS = [
    "SOPInstanceUID", "SOPClassUID", "SeriesInstanceUID", "SeriesNumber", "InstanceNumber", "Modality", "Rows",
    "Columns", "PixelSpacing", "ImageType", "Manufacturer", "ManufacturerModelName", "MagneticFieldStrength",
    "SoftwareVersions", "MRAcquisitionType", "ScanningSequence", "SequenceVariant", "ScanOptions", "SequenceName",
    "RepetitionTime", "EchoTime", "InversionTime", "FlipAngle", "EchoTrainLength", "EchoNumbers", "SliceThickness",
    "SpacingBetweenSlices", "AcquisitionMatrix", "InPlanePhaseEncodingDirection", "PercentPhaseFieldOfView",
    "ImageOrientationPatient", "ImagePositionPatient", "FrameOfReferenceUID", "AcquisitionTime", "SeriesTime",
    "ContrastBolusAgent", "ContrastBolusStartTime", "NumberOfFrames", "SeriesDescription", "ProtocolName",
    "ReferencedImageSequence", "SourceImageSequence", IMAGE_TYPE_TEXT, GE_COMPLEX]
ENHANCED_MR = {"1.2.840.10008.5.1.4.1.1.4.1", "1.2.840.10008.5.1.4.1.1.4.3"}
SIDECAR_FIELDS = ("source_series_key", "SeriesNumber", "EchoTime", "EchoNumber", "ImageType", "RepetitionTime",
                  "InversionTime", "FlipAngle", "MRAcquisitionType", "ScanningSequence", "SequenceVariant",
                  "PhaseEncodingDirection", "TotalReadoutTime")
COMPANION = re.compile(r"_(ROI\d+|Eq_\d+|Tilt_\d+|ADC)\.nii\.gz")
COMPANION_KINDS = {"ROI": "roi", "Eq": "eq", "Tilt": "tilt", "ADC": "adc"}
MIXED_KEEP = ("series_key", "series_number", "manufacturer", "model", "field_t", "n_instances", "n_stacks")


def _hash(text):
    return hashlib.sha256(str(text).encode()).hexdigest()[:20]


def _float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _floats(value):
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        value = [value]
    try:
        values = [float(x) for x in value]
    except (TypeError, ValueError):
        return None
    return values if all(math.isfinite(x) for x in values) else None


def _text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("ascii", errors="replace").strip("\0 ")
    return str(value).strip()


def _tokens(value):
    if value is None:
        return []
    if isinstance(value, bytes):
        value = _text(value)
    if isinstance(value, str):
        return [t.strip().upper() for t in value.split("\\") if t.strip()]
    return [str(t).strip().upper() for t in value if str(t).strip()]


def _seconds(value):
    """Seconds after midnight from a DICOM TM value, or None."""
    text = _text(value).replace(":", "")
    m = re.fullmatch(r"(\d{2})(\d{2})?(\d{2})?(\.\d+)?", text)
    if not m:
        return None
    return int(m[1]) * 3600 + int(m[2] or 0) * 60 + int(m[3] or 0) + float(m[4] or 0)


def _ge_part(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        if len(value) < 2:
            return None
        value = struct.unpack("<h", value[:2])[0]
    try:
        return GE_PARTS.get(int(value[0] if isinstance(value, (list, tuple)) else value))
    except (TypeError, ValueError):
        return None


def _instance(ds):
    """Technical facts of one image object; free text stays in memory (D6)."""
    def get(keyword):
        try:
            return ds.get(keyword)
        except Exception:
            return None

    image_type = _tokens(get("ImageType"))
    element = ds.get(IMAGE_TYPE_TEXT)
    text_tokens = _tokens(element.value) if element is not None else []
    part_tokens = {PART_TOKENS[t] for t in image_type + text_tokens if t in PART_TOKENS}
    ge = ds.get(GE_COMPLEX)
    part = next(iter(part_tokens)) if len(part_tokens) == 1 else (_ge_part(ge.value) if ge is not None else None)
    refs = []
    for keyword in ("ReferencedImageSequence", "SourceImageSequence"):
        for item in get(keyword) or []:
            uid = item.get("ReferencedSOPInstanceUID")
            if uid:
                refs.append(str(uid))
    iop = _floats(get("ImageOrientationPatient"))
    pixel = _floats(get("PixelSpacing"))
    matrix = get("AcquisitionMatrix")
    description, protocol = _text(get("SeriesDescription")), _text(get("ProtocolName"))
    return {
        "sop": str(get("SOPInstanceUID") or ""), "series": str(get("SeriesInstanceUID") or ""), "refs": refs,
        "image_type": image_type, "image_type_text": text_tokens, "part": part or "mag",
        "rows": get("Rows"), "cols": get("Columns"),
        "pixel": [round(x, 6) for x in pixel] if pixel and len(pixel) == 2 else None,
        "iop": [round(x, 3) for x in iop] if iop and len(iop) == 6 else None,
        "ipp": _floats(get("ImagePositionPatient")),
        "tr": _float(get("RepetitionTime")), "te": _float(get("EchoTime")), "ti": _float(get("InversionTime")),
        "flip": _float(get("FlipAngle")), "etl": _float(get("EchoTrainLength")),
        "echo_number": _float(get("EchoNumbers")), "thick": _float(get("SliceThickness")),
        "sbs": _float(get("SpacingBetweenSlices")),
        "matrix": [int(x) for x in matrix] if matrix is not None and len(list(matrix)) == 4 else None,
        "phase_dir": _text(get("InPlanePhaseEncodingDirection")).upper() or None,
        "pct_phase_fov": _float(get("PercentPhaseFieldOfView")),
        "manufacturer": _text(get("Manufacturer")), "model": _text(get("ManufacturerModelName")),
        "field_t": _float(get("MagneticFieldStrength")), "software": "\\".join(_tokens(get("SoftwareVersions"))),
        "mr_acq": _text(get("MRAcquisitionType")).upper() or None, "scan_seq": _tokens(get("ScanningSequence")),
        "seq_var": _tokens(get("SequenceVariant")), "scan_opt": _tokens(get("ScanOptions")),
        "seq_name": _text(get("SequenceName")), "for": _text(get("FrameOfReferenceUID")),
        "acq_t": _seconds(get("AcquisitionTime")), "ser_t": _seconds(get("SeriesTime")),
        "bolus_t": _seconds(get("ContrastBolusStartTime")), "agent": bool(_text(get("ContrastBolusAgent"))),
        "series_number": int(_float(get("SeriesNumber"))) if _float(get("SeriesNumber")) is not None else None,
        "multiframe": (_float(get("NumberOfFrames")) or 1) > 1 or str(get("SOPClassUID") or "") in ENHANCED_MR,
        "desc": description_tokens(description, protocol)[0], "desc_phase": description_phase(description, protocol),
    }


def _manufacturer(text):
    upper = text.upper()
    for name in ("GE", "SIEMENS", "PHILIPS"):
        if upper.startswith(name) or (name == "GE" and "GENERAL ELECTRIC" in upper):
            return name
    return upper.split()[0] if upper.split() else ""


def _first(instances, key):
    return next((i[key] for i in instances if i[key] not in (None, "", [])), None)


def _series_record(key, instances, t0):
    """One normalized record per series (5.1), from its instances."""
    def stack_key(i):
        return (i["rows"], i["cols"], tuple(i["pixel"] or ()), tuple(i["iop"] or ()),
                i["tr"], i["ti"], i["flip"], tuple(t for t in i["image_type"] if t not in PART_TOKENS))

    stacks = Counter(stack_key(i) for i in instances)
    record = {"series_key": key, "series_number": _first(instances, "series_number"),
              "manufacturer": _manufacturer(_first(instances, "manufacturer") or ""),
              "model": _first(instances, "model") or "", "field_t": _first(instances, "field_t"),
              "n_instances": len(instances), "n_stacks": len(stacks), "mixed": len(stacks) > 1}
    if record["mixed"]:
        return {k: record[k] for k in MIXED_KEEP} | {"mixed": True}
    image_type = []
    for i in instances:
        for t in i["image_type"] + i["image_type_text"]:
            if t not in image_type:
                image_type.append(t)
    tes = sorted({round(i["te"], 2) for i in instances if i["te"] is not None})
    echoes = []
    if len(tes) > 1:
        for te in tes:
            members = [i for i in instances if i["te"] is not None and round(i["te"], 2) == te]
            numbers = Counter(i["echo_number"] for i in members if i["echo_number"] is not None)
            echoes.append({"te_ms": te, "echo_number": int(numbers.most_common(1)[0][0]) if numbers else None,
                           "n_instances": len(members)})
    parts = Counter(i["part"] for i in instances)
    first_te = tes[0] if tes else None
    base = [i for i in instances if i["part"] == "mag" and (first_te is None or i["te"] is None
                                                             or round(i["te"], 2) == first_te)] or instances
    iop = _first(base, "iop")
    mosaic = "MOSAIC" in image_type
    positions = []
    if iop and not mosaic:
        r, c = iop[:3], iop[3:]
        n = (r[1] * c[2] - r[2] * c[1], r[2] * c[0] - r[0] * c[2], r[0] * c[1] - r[1] * c[0])
        positions = sorted({round(sum(a * b for a, b in zip(i["ipp"], n)), 2) for i in base
                            if i["ipp"] and len(i["ipp"]) == 3})
    steps = [round(b - a, 4) for a, b in zip(positions, positions[1:])]
    step = median(steps) if steps else None
    thick = _first(instances, "thick")
    times = [i["acq_t"] for i in instances if i["acq_t"] is not None] or \
        [i["ser_t"] for i in instances if i["ser_t"] is not None]
    series_times = [i["ser_t"] for i in instances if i["ser_t"] is not None]
    bolus = _first(instances, "bolus_t")

    def rel(t):
        return round(t - t0, 3) if t is not None and t0 is not None else None

    record.update({
        "image_type": image_type, "localizer": "LOCALIZER" in image_type, "mosaic": mosaic,
        "software": _first(instances, "software") or "", "mr_acq": _first(instances, "mr_acq"),
        "scan_seq": _first(instances, "scan_seq") or [], "seq_var": _first(instances, "seq_var") or [],
        "scan_opt": _first(instances, "scan_opt") or [], "seq_name": _first(instances, "seq_name") or "",
        "tr_ms": _first(instances, "tr"), "te_ms": first_te if len(tes) == 1 else None,
        "ti_ms": _first(instances, "ti"), "flip_deg": _first(instances, "flip"), "etl": _first(instances, "etl"),
        "rows": _first(instances, "rows"), "cols": _first(instances, "cols"), "pixel_mm": _first(instances, "pixel"),
        "thick_mm": thick, "sbs_mm": _first(instances, "sbs"), "acq_matrix": _first(instances, "matrix"),
        "phase_dir": _first(instances, "phase_dir"), "pct_phase_fov": _first(instances, "pct_phase_fov"),
        "orientation": orientation(iop), "n_positions": len(positions) or None,
        "pos_min": positions[0] if positions else None, "pos_max": positions[-1] if positions else None,
        "step_mm": step,
        "uniform_spacing": (max(steps) - min(steps) <= 0.05 + 0.01 * abs(step)) if steps else None,
        "extent_mm": round((len(positions) - 1) * step + thick, 3) if step is not None and thick is not None else None,
        "for_hash": _hash(_first(instances, "for")) if _first(instances, "for") else None,
        "acq_rel_s": rel(min(times) if times else None), "series_rel_s": rel(min(series_times) if series_times else None),
        "bolus_rel_s": rel(bolus), "contrast_agent": any(i["agent"] for i in instances),
        "parts": dict(sorted(parts.items())), "echoes": echoes,
        "desc_tokens": sorted({t for i in instances for t in i["desc"]}),
        "desc_phase": _first(instances, "desc_phase"),
        "multiframe": any(i["multiframe"] for i in instances),
        "has_image_type_text": any(i["image_type_text"] for i in instances),
    })
    return record


def read_series(root, inventory, stop=None):
    """{series_key: record} for one extraction (5.1). Non-MR objects and objects without Rows are skipped."""
    import pydicom
    by_series, sop_to_series = {}, {}
    for item in inventory["files"]:
        if stop is not None and stop.is_set():
            raise InterruptedError("Selection interrupted")
        ds = pydicom.dcmread(Path(root) / item["name"], force=True, stop_before_pixels=True, specific_tags=SELECT_TAGS)
        if "Rows" not in ds or str(ds.get("Modality", "MR")) != "MR":
            continue
        inst = _instance(ds)
        key = _hash(inst["series"])
        by_series.setdefault(key, []).append(inst)
        if inst["sop"]:
            sop_to_series[inst["sop"]] = key
    times = [i["acq_t"] if i["acq_t"] is not None else i["ser_t"] for group in by_series.values() for i in group]
    times = [t for t in times if t is not None]
    t0 = min(times) if times else None
    records = {}
    for key, instances in sorted(by_series.items()):
        record = _series_record(key, instances, t0)
        if not record.get("mixed"):
            refs = {sop_to_series[uid] for i in instances for uid in i["refs"] if uid in sop_to_series}
            record["ref_series_keys"] = sorted(refs - {key})
        records[key] = record
    return records


def nifti_header(path):
    """(dims, pixdim) from a gzipped single-file NIfTI-1 or NIfTI-2 header."""
    with gzip.open(path, "rb") as f:
        head = f.read(540)
    if len(head) < 348:
        raise ValueError("Truncated NIfTI header")
    endian = "<" if struct.unpack("<i", head[:4])[0] in (348, 540) else ">"
    if struct.unpack(endian + "i", head[:4])[0] == 348:
        return list(struct.unpack_from(endian + "8h", head, 40)), [round(x, 6) for x in struct.unpack_from(endian + "8f", head, 76)]
    if len(head) < 540:
        raise ValueError("Truncated NIfTI header")
    return list(struct.unpack_from(endian + "8q", head, 16)), [round(x, 6) for x in struct.unpack_from(endian + "8d", head, 104)]


def _numbers(path):
    return [float(x) for x in Path(path).read_text().split()]


def read_volumes(folder, nifti_root):
    """Facts of each published volume in a NIfTI session folder (5.2), from its inventory."""
    folder, nifti_root = Path(folder), Path(nifti_root)
    listed = {item["name"]: item for item in fsutil.read_json(folder / receipts.INVENTORY)["files"]}
    volumes = []
    for name in sorted(listed):
        if not name.endswith(".nii.gz") or name[:-7] + ".json" not in listed:
            continue
        stem = name[:-7]
        companions = []
        for other in sorted(listed):
            suffix = other[len(stem):] if other.startswith(stem) else None
            if not suffix or other == name or suffix == ".json":
                continue
            if suffix in (".bval", ".bvec"):
                kind = suffix[1:]
            elif (m := COMPANION.fullmatch(suffix)):
                kind = COMPANION_KINDS[re.match(r"[A-Za-z]+", m[1])[0]]
            else:
                continue
            companions.append({"name": other, "kind": kind, "sha256": listed[other]["sha256"]})
        raw = fsutil.read_json(folder / (stem + ".json"))
        sidecar = {k: raw[k] for k in SIDECAR_FIELDS if k in raw} if isinstance(raw, dict) else {}
        dims, pixdim = nifti_header(folder / name)
        gradients = None
        bval = next((c for c in companions if c["kind"] == "bval"), None)
        bvec = next((c for c in companions if c["kind"] == "bvec"), None)
        if bval and bvec:
            values = _numbers(folder / bval["name"])
            vectors = _numbers(folder / bvec["name"])
            rows = [vectors[i * len(values):(i + 1) * len(values)] for i in range(3)] \
                if values and len(vectors) == 3 * len(values) else [vectors]
            gradients = gradient_stats(values, rows, dims[4] if dims[0] >= 4 else 1)
        volumes.append({"relpath": (folder / name).relative_to(nifti_root).as_posix(), "name": name,
                        "sha256": listed[name]["sha256"], "json_sha256": listed[stem + ".json"]["sha256"],
                        "folder": name.split("/", 1)[0] if "/" in name else "", "token": stem.rsplit("_", 1)[-1],
                        "companions": companions, "sidecar": sidecar, "dims": dims, "pixdim": pixdim,
                        "gradients": gradients})
    return volumes


def session_metadata(entry, conversion, cfg, stop=None):
    """(series, volumes, cache_hit) for one ready session, from the cache when its keys match (7.3)."""
    sid, label = entry["subject_id"], entry["label"]
    path = cfg.reports / ".selection_cache" / sid / f"{label}.json"
    keys = {"cache_version": CACHE_VERSION, "extraction_content_sha256": entry["content_sha256"],
            "conversion_receipt_id": conversion.get("receipt_id")}
    if not cfg.audit:
        try:
            cached = fsutil.read_json(path)
            if all(cached.get(k) == v for k, v in keys.items()):
                return cached["series"], cached["volumes"], True
        except (OSError, ValueError, KeyError, AttributeError):
            pass
    inventory = fsutil.read_json(entry["extraction"]["inventory"])
    series = read_series(entry["extraction"]["root"], inventory, stop)
    volumes = read_volumes(Path(conversion["root"]), cfg.niftis)
    if not cfg.dry_run:
        fsutil.atomic_json(path, dict(keys, series=series, volumes=volumes))
    return series, volumes, False
