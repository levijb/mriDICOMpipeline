"""Pages for checking images by eye: R4-F-027 now, and any series a reviewer wants to look at later.

    python tests/round4/realdata/r4_inspect.py --project D:\\mri_project --private D:\\round4\\private \\
        --visit V077 --focus 500 502 503

Writes <private>/inspect/<visit>/index.html, with PNG slices in img/ beside it; open index.html in a browser.
The page lists every series of the visit (the --focus series first, outlined) and shows for each:

* every NIfTI the series converted to, slice by slice in the plane it was acquired in (mouse wheel over the
  image, the slider, or the arrow keys; Page Up and Page Down jump 10 slices), with the middle slice of the other
  two planes; 4-D files get a volume slider (up to 12 volumes, labelled with b-values when a .bval exists);
* the key DICOM fields, the values that vary across the series' instances, and the first instance's full header;
* the NIfTI header, value statistics (range, percentiles, share of zero and negative voxels, mean of each volume)
  and the sidecar.

Page controls: link all sliders (by relative position), mirror left and right (radiological view), brightness,
contrast, image size, sharp pixels, and showing only the focus series.

Identifiers are left out: person names, dates, clock times (times appear as minutes from the visit's first
series), UIDs other than the storage class, patient and study IDs, institution, station, device serial, and the
pipeline's subject and session sidecar fields. Series descriptions and images are shown, so the page belongs in
the private folder. Read-only toward the project; prints counts only.

render_nifti, dicom_summary and build_page do not depend on round 4, so they can move into the package later.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import struct
import sys
import zlib
from collections import Counter

import numpy as np

MAX_VOLUMES = 12
PLANE_NAMES = {0: "sagittal", 1: "coronal", 2: "axial"}
# Letters at the image edges (top, bottom, left, right) in the neurological view: the subject's left on the left.
PLANE_LABELS = {0: ("S", "I", "P", "A"), 1: ("S", "I", "L", "R"), 2: ("A", "P", "L", "R")}
PLANE_AXES = {0: (1, 2), 1: (0, 2), 2: (0, 1)}  # RAS (column, row) axes of each plane

KEY_FIELDS = [
    ("Series number", "SeriesNumber"), ("Series description", "SeriesDescription"),
    ("Protocol name", "ProtocolName"), ("Image type", "ImageType"),
    ("Siemens image type text (0021,1175)", (0x0021, 0x1175)),
    ("GE image type (0043,102F): 0 magnitude, 1 phase, 2 real, 3 imaginary", (0x0043, 0x102F)),
    ("Derivation description", "DerivationDescription"), ("Image comments", "ImageComments"),
    ("Storage class", "SOPClassUID"), ("Modality", "Modality"), ("Manufacturer", "Manufacturer"),
    ("Model", "ManufacturerModelName"), ("Software", "SoftwareVersions"),
    ("Field strength (T)", "MagneticFieldStrength"), ("Scanning sequence", "ScanningSequence"),
    ("Sequence variant", "SequenceVariant"), ("Scan options", "ScanOptions"), ("2D or 3D", "MRAcquisitionType"),
    ("Sequence name", "SequenceName"), ("GE pulse sequence (0019,109C)", (0x0019, 0x109C)),
    ("GE internal sequence (0019,109E)", (0x0019, 0x109E)), ("Pulse sequence name", "PulseSequenceName"),
    ("Echo time (ms)", "EchoTime"), ("Repetition time (ms)", "RepetitionTime"),
    ("Inversion time (ms)", "InversionTime"), ("Flip angle (degrees)", "FlipAngle"),
    ("Echo train length", "EchoTrainLength"), ("Echo numbers", "EchoNumbers"), ("Averages", "NumberOfAverages"),
    ("Pixel bandwidth (Hz/pixel)", "PixelBandwidth"), ("Diffusion b-value", "DiffusionBValue"),
    ("GE b-value and slope (0043,1039)", (0x0043, 0x1039)), ("Siemens b-value (0019,100C)", (0x0019, 0x100C)),
    ("Contrast agent", "ContrastBolusAgent"), ("Body part", "BodyPartExamined"),
    ("Patient position", "PatientPosition"), ("Rows", "Rows"), ("Columns", "Columns"),
    ("Pixel spacing (mm)", "PixelSpacing"), ("Slice thickness (mm)", "SliceThickness"),
    ("Spacing between slices (mm)", "SpacingBetweenSlices"), ("Acquisition matrix", "AcquisitionMatrix"),
    ("Phase-encoding direction", "InPlanePhaseEncodingDirection"),
    ("Orientation (row and column cosines)", "ImageOrientationPatient"),
    ("Photometric interpretation", "PhotometricInterpretation"), ("Samples per pixel", "SamplesPerPixel"),
    ("Bits stored", "BitsStored"), ("Signed pixel values (1 = yes)", "PixelRepresentation"),
    ("Rescale slope", "RescaleSlope"), ("Rescale intercept", "RescaleIntercept"),
    ("Window centre", "WindowCenter"), ("Window width", "WindowWidth"),
]
VARY_FIELDS = [
    ("Image type", "ImageType"), ("Siemens image type text", (0x0021, 0x1175)),
    ("GE image type", (0x0043, 0x102F)), ("Echo time", "EchoTime"), ("Echo number", "EchoNumbers"),
    ("Inversion time", "InversionTime"), ("Diffusion b-value", "DiffusionBValue"),
    ("GE b-value and slope", (0x0043, 0x1039)), ("Siemens b-value", (0x0019, 0x100C)),
    ("Acquisition number", "AcquisitionNumber"), ("Temporal position", "TemporalPositionIdentifier"),
    ("Rows", "Rows"), ("Columns", "Columns"), ("Pixel spacing", "PixelSpacing"),
    ("Slice thickness", "SliceThickness"), ("Orientation (rounded)", "ImageOrientationPatient"),
    ("Photometric interpretation", "PhotometricInterpretation"), ("Storage class", "SOPClassUID"),
    ("Series description", "SeriesDescription"), ("Sequence name", "SequenceName"),
    ("Scanning sequence", "ScanningSequence"), ("Manufacturer", "Manufacturer"),
]
HIDDEN_VR = {"PN", "DA", "DT", "TM", "UI"}
SHOWN_UIDS = {"SOPClassUID", "ReferencedSOPClassUID", "TransferSyntaxUID"}
HIDDEN_KEYWORDS = {
    "PatientID", "OtherPatientIDs", "OtherPatientIDsSequence", "PatientAddress", "PatientTelephoneNumbers",
    "PatientAge", "PatientSex", "PatientSize", "PatientWeight", "PatientComments", "PatientState",
    "AdditionalPatientHistory", "MedicalAlerts", "Allergies", "EthnicGroup", "Occupation", "MilitaryRank",
    "BranchOfService", "CountryOfResidence", "RegionOfResidence", "PregnancyStatus", "AccessionNumber", "StudyID",
    "InstitutionName", "InstitutionAddress", "InstitutionalDepartmentName", "InstitutionCodeSequence",
    "StationName", "DeviceSerialNumber", "RequestingService", "RequestedProcedureID", "ScheduledProcedureStepID",
    "PerformedProcedureStepID", "RequestAttributesSequence", "ReferencedPatientSequence",
    "ReferencedStudySequence", "OperatorIdentificationSequence", "PerformingPhysicianIdentificationSequence",
    "ReferringPhysicianIdentificationSequence", "PhysiciansOfRecordIdentificationSequence",
    "ReferringPhysicianAddress", "ReferringPhysicianTelephoneNumbers", "PersonIdentificationCodeSequence",
    "ResponsibleOrganization", "PatientInsurancePlanCodeSequence", "MedicalRecordLocator", "StudyComments",
}
SIDECAR_HIDDEN = {"subject_id", "session", "session_date", "dicom_study_date", "AcquisitionTime", "SeriesTime",
                  "StudyTime", "ContentTime"}
SIDECAR_HIDDEN_PATTERN = re.compile(r"Date|UID|PatientName|PatientID|PatientBirth|PatientSex|PatientAge|"
                                    r"PatientWeight|PatientSize|Institution|StationName|DeviceSerial|Accession|"
                                    r"Physician|Operator", re.I)


# ---------------------------------------------------------------- images

def png(arr):
    """PNG bytes of a uint8 image: 2-D grey, or rows x columns x 3 colour."""
    arr = np.ascontiguousarray(arr, dtype=np.uint8)
    h, w = arr.shape[:2]
    rows = np.hstack([np.zeros((h, 1), np.uint8), arr.reshape(h, -1)])

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    head = struct.pack(">IIBBBBB", w, h, 8, 0 if arr.ndim == 2 else 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", zlib.compress(rows.tobytes(), 6))
            + chunk(b"IEND", b""))


def _ras(img):
    """Data as X x Y x Z x volumes (x 3 for colour) in RAS order, RAS voxel sizes, slice axis, scaling, colour."""
    from nibabel import orientations

    proxy = img.dataobj
    raw = np.asanyarray(proxy.get_unscaled() if hasattr(proxy, "get_unscaled") else proxy)
    slope, inter = float(getattr(proxy, "slope", 1.0)), float(getattr(proxy, "inter", 0.0))
    slope = slope if np.isfinite(slope) and slope != 0 else 1.0
    inter = inter if np.isfinite(inter) else 0.0
    rgb = raw.dtype.names is not None
    if rgb:
        raw = np.stack([raw[name] for name in raw.dtype.names[:3]], axis=-1)
    spatial, tail = (raw.shape[:-1], raw.shape[-1:]) if rgb else (raw.shape, ())
    if len(spatial) < 4:
        spatial = spatial + (1,) * (4 - len(spatial))
    elif len(spatial) > 4:
        spatial = spatial[:3] + (int(np.prod(spatial[3:])),)
    raw = raw.reshape(spatial + tail)
    ornt = orientations.io_orientation(img.affine)
    if np.isnan(ornt).any():
        ornt = np.array([[0, 1], [1, 1], [2, 1]], float)
    data = orientations.apply_orientation(raw, ornt)
    zooms = [float(z) for z in img.header.get_zooms()[:3]] + [1.0, 1.0, 1.0]
    ras_zooms = [1.0, 1.0, 1.0]
    for axis in range(3):
        ras_zooms[int(ornt[axis, 0])] = zooms[axis] or 1.0
    return data, ras_zooms, int(ornt[2, 0]), slope, inter, rgb


def _fill(v):
    """The value that fills at least 10 % of the voxels (a background such as 0, or a no-data value such as
    -32768 outside a mask), with its share; (None, 0.0) when there is none."""
    values, counts = np.unique(v, return_counts=True)
    top = int(np.argmax(counts))
    share = counts[top] / v.size
    return (float(values[top]), float(share)) if share >= 0.10 else (None, 0.0)


def _window(vol):
    """Display range, leaving out a fill value: symmetric about zero for signed data such as phase or QSM, else
    from the lowest value (or a zero background) to the 99.5th percentile."""
    v = vol[np.isfinite(vol)]
    if v.size > 2_000_000:
        v = v[:: v.size // 2_000_000 + 1]
    if not v.size:
        return 0.0, 1.0
    fill, _share = _fill(v)
    rest = v[v != fill] if fill is not None else v
    if not rest.size:
        return fill - 1.0, fill + 1.0
    lo, hi = float(rest.min()), float(rest.max())
    if lo < 0 and -lo > 0.1 * max(hi, 0.0):
        m = float(np.percentile(np.abs(rest), 99.5)) or max(-lo, hi) or 1.0
        return -m, m
    hi = float(np.percentile(rest, 99.5))
    if fill is not None and 0 <= fill <= lo:
        lo = fill
    return (lo, hi) if hi > lo else (lo, float(rest.max()) if rest.max() > lo else lo + 1.0)


def _plane(vol, axis, i):
    """One slice of a RAS volume, rows from top (anterior or superior) down, ready to save."""
    s = vol[:, :, i] if axis == 2 else vol[:, i, :] if axis == 1 else vol[i, :, :]
    return np.swapaxes(s, 0, 1)[::-1]


def _mm(shape, zooms, axis):
    c, r = PLANE_AXES[axis]
    return max(shape[c] * zooms[c], 0.1), max(shape[r] * zooms[r], 0.1)


def _sibling(path, ext):
    stem = path[:-7] if path.endswith(".nii.gz") else os.path.splitext(path)[0]
    return stem + ext


def _numbers(path):
    try:
        with open(path, encoding="ascii") as f:
            return [float(x) for x in f.read().split()]
    except (OSError, ValueError):
        return []


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def _stats(data, slope, inter, rgb):
    if rgb:
        return [("Values", "colour (RGB) image; statistics not computed")]
    nvol = data.shape[3]
    use = range(nvol) if nvol <= 400 else range(0, nvol, nvol // 400 + 1)
    budget = max(1, 4_000_000 // len(use))
    samples, means = [], []
    for v in use:
        flat = (np.asarray(data[:, :, :, v], dtype=np.float32) * slope + inter).ravel()
        finite = flat[np.isfinite(flat)]
        means.append(float(finite.mean(dtype=np.float64)) if finite.size else float("nan"))
        samples.append(finite[:: max(1, finite.size // budget)])
    x = np.concatenate(samples).astype(np.float64) if samples else np.empty(0)
    if not x.size:
        return [("Values", "no finite values")]
    p1, p50, p99 = np.percentile(x, [1, 50, 99])
    rows = [("Minimum / maximum", f"{x.min():.6g} / {x.max():.6g}"),
            ("Mean / standard deviation", f"{x.mean():.6g} / {x.std():.6g}"),
            ("1st / 50th / 99th percentile", f"{p1:.6g} / {p50:.6g} / {p99:.6g}"),
            ("Zero voxels", f"{np.mean(x == 0) * 100:.1f}%"), ("Negative voxels", f"{np.mean(x < 0) * 100:.1f}%")]
    fill, share = _fill(x)
    if fill is not None:
        rows.append(("Most common value", f"{fill:g} in {share * 100:.1f}% of voxels (left out of the display range)"))
        rest = x[x != fill]
        if rest.size:
            q1, q50, q99 = np.percentile(rest, [1, 50, 99])
            rows.append(("Without it: min / 1st / 50th / 99th percentile / max",
                         f"{rest.min():.6g} / {q1:.6g} / {q50:.6g} / {q99:.6g} / {rest.max():.6g}"))
    head = x[:: max(1, x.size // 500_000)]
    distinct = np.unique(head)
    if distinct.size <= 12:
        rows.append(("Distinct values", " ".join(f"{d:g}" for d in distinct)))
    elif np.all(np.mod(head, 1) == 0):
        rows.append(("Values", "whole numbers"))
    if nvol > 1:
        rows.append(("Mean of each volume" + ("" if nvol <= 400 else " (every few volumes)"),
                     " ".join(f"{m:.4g}" for m in means)))
    return rows


def render_nifti(path, out_dir, prefix, max_volumes=MAX_VOLUMES):
    """Write one PNG per slice (up to max_volumes volumes) and the middle slices of the other planes.

    Returns what the page needs: the viewer's settings, a one-line summary and the header and value rows."""
    import nibabel as nib

    img = nib.load(path)
    data, zooms, axis, slope, inter, rgb = _ras(img)
    nvol, n = data.shape[3], data.shape[axis]
    vols = (list(range(nvol)) if nvol <= max_volumes
            else sorted({round(k * (nvol - 1) / (max_volumes - 1)) for k in range(max_volumes)}))
    bvals = _numbers(_sibling(path, ".bval"))
    os.makedirs(out_dir, exist_ok=True)
    windows, mids, images = [], [], 0
    for v in vols:
        if rgb:
            vol8, lo, hi = np.asarray(data[:, :, :, v], dtype=np.uint8), 0.0, 255.0
        else:
            vol = np.asarray(data[:, :, :, v], dtype=np.float32) * slope + inter
            lo, hi = _window(vol)
            vol8 = np.clip((np.nan_to_num(vol) - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)
        windows.append((lo, hi))
        for i in range(n):
            _write(os.path.join(out_dir, f"v{v}_s{i}.png"), png(_plane(vol8, axis, i)))
        images += n
        if v == vols[0]:
            for other in (a for a in range(3) if a != axis):
                name = f"mid{other}.png"
                _write(os.path.join(out_dir, name), png(_plane(vol8, other, vol8.shape[other] // 2)))
                mids.append({"src": prefix + name, "plane": PLANE_NAMES[other], "mm": _mm(vol8.shape, zooms, other)})
                images += 1
    hdr = img.header
    descrip = hdr["descrip"].item() if "descrip" in hdr else b""
    descrip = descrip.decode("latin-1", "replace") if isinstance(descrip, bytes) else str(descrip)
    descrip = re.sub(r";?\s*Time=[^;]*", "", descrip.strip("\x00 "))
    shape = "×".join(str(x) for x in img.shape)
    voxel = "×".join(f"{z:.3g}" for z in hdr.get_zooms()[:3])
    info = [("Shape", shape), ("Voxel size (mm)", voxel), ("Data type on disk", str(img.get_data_dtype())),
            ("Voxel axes point toward", "".join(nib.aff2axcodes(img.affine))),
            ("Slices shown", f"{PLANE_NAMES[axis]}, {n} slices" + (f", {len(vols)} of {nvol} volumes" if nvol > 1 else "")),
            ("Scaling", f"slope {slope:g}, intercept {inter:g}")]
    if "qform_code" in hdr:
        info.append(("qform / sform code", f"{int(hdr['qform_code'])} / {int(hdr['sform_code'])}"))
    if hasattr(hdr, "get_xyzt_units"):
        info.append(("Units (space / time)", " / ".join(str(u) for u in hdr.get_xyzt_units())))
    if len(img.shape) > 3:
        info.append(("pixdim[4] (repetition time for 4-D)", f"{float(hdr['pixdim'][4]):g}"))
    if descrip:
        info.append(("Header description (clock time removed)", descrip))
    info.append(("Affine (voxel to mm)", "\n".join(" ".join(f"{x:9.3f}" for x in row) for row in img.affine)))
    if bvals:
        info.append(("b-values", " ".join(f"{b:g}" for b in bvals)))
    if os.path.exists(_sibling(path, ".bvec")):
        info.append(("b-vectors", "present"))
    info += _stats(data, slope, inter, rgb)
    return {"prefix": prefix, "n": n, "vols": vols, "labels": PLANE_LABELS[axis], "mm": _mm(data.shape, zooms, axis),
            "mids": mids, "images": images, "info": info,
            "summary": f"{shape} · voxel {voxel} mm · {PLANE_NAMES[axis]} slices",
            "vlabels": [f"volume {v + 1} of {nvol}" + (f", b = {bvals[v]:g}" if len(bvals) == nvol else "")
                        for v in vols],
            "wlabels": ["colour image" if rgb else f"display range {lo:.4g} to {hi:.4g}" for lo, hi in windows]}


# ---------------------------------------------------------------- DICOM headers

def _bytes(b):
    text = bytes(b).rstrip(b"\x00 ")
    if len(text) <= 64 and all(32 <= c < 127 for c in text):
        return text.decode("ascii")
    return f"<{len(b)} bytes>"


def _fmt(value, digits=None):
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return _bytes(value)
    if isinstance(value, (list, tuple)) or type(value).__name__ == "MultiValue":
        return "\\".join(_fmt(v, digits) for v in value)
    if type(value).__name__ == "UID":
        return value.name
    if digits is not None:
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            pass
    if isinstance(value, float):
        return f"{value:.6g}"
    text = str(value).strip()
    return text if len(text) <= 300 else text[:300] + "…"


def _value(ds, tag):
    from pydicom.tag import Tag

    try:
        t = Tag(tag)
        return ds[t].value if t in ds else None
    except Exception:  # noqa: BLE001 - an unreadable element counts as absent
        return None


def _int(value, default=10**9):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _secs(tm):
    m = re.fullmatch(r"(\d{2})(\d{2})?(\d{2})?(\.\d+)?", str(tm or "").strip().replace(":", ""))
    if not m:
        return None
    return int(m[1]) * 3600 + int(m[2] or 0) * 60 + int(m[3] or 0) + float(m[4] or 0)


def _dump(ds, depth=0, out=None):
    """Rows (depth, tag, name, VR, value) of a header without identifiers or pixel data."""
    out = [] if out is None else out
    for elem in ds:
        try:
            keyword = elem.keyword or ""
            if (elem.tag == 0x7FE00010 or keyword in HIDDEN_KEYWORDS or keyword.startswith(("ClinicalTrial", "Issuer"))
                    or (elem.VR in HIDDEN_VR and keyword not in SHOWN_UIDS)):
                continue
            tag = f"({elem.tag.group:04X},{elem.tag.element:04X})"
            if elem.VR == "SQ":
                items = list(elem.value)
                out.append((depth, tag, elem.name, "SQ", f"{len(items)} item(s)"))
                if depth < 4:
                    for k, item in enumerate(items[:3]):
                        out.append((depth + 1, "", f"item {k + 1}", "", ""))
                        _dump(item, depth + 2, out)
                    if len(items) > 3:
                        out.append((depth + 1, "", f"… {len(items) - 3} more items", "", ""))
                continue
            out.append((depth, tag, elem.name, elem.VR, _fmt(elem.value)))
        except Exception as e:  # noqa: BLE001 - shown as unreadable
            out.append((depth, "", "unreadable element", "", type(e).__name__))
    return out


def dicom_summary(paths):
    """Key fields, the values that vary across instances, and the full header of one series' DICOM files."""
    import pydicom
    from pydicom.tag import Tag

    wanted = sorted({Tag(t) for _label, t in VARY_FIELDS}
                    | {Tag(k) for k in ("InstanceNumber", "ImagePositionPatient", "AcquisitionTime", "SeriesTime")})
    seen, unreadable = [], 0
    for path in paths:
        try:
            seen.append((path, pydicom.dcmread(path, stop_before_pixels=True, specific_tags=wanted)))
        except Exception:  # noqa: BLE001 - counted
            unreadable += 1
    if not seen:
        return None
    vary = []
    for label, tag in VARY_FIELDS:
        digits = 3 if tag == "ImageOrientationPatient" else None
        counts = Counter(_fmt(_value(ds, tag), digits) for _p, ds in seen)
        if set(counts) != {""}:
            vary.append((label, counts.most_common()))
    along = []
    for _p, ds in seen:
        try:
            iop, ipp = _value(ds, "ImageOrientationPatient"), _value(ds, "ImagePositionPatient")
            row, col = np.array([float(x) for x in iop[:3]]), np.array([float(x) for x in iop[3:6]])
            along.append(float(np.dot(np.cross(row, col), [float(x) for x in ipp])))
        except Exception:  # noqa: BLE001 - no position
            pass
    acq = [t for t in (_secs(_value(ds, "AcquisitionTime")) for _p, ds in seen) if t is not None]
    first = min(seen, key=lambda pd: (_int(_value(pd[1], "InstanceNumber")), pd[0]))[0]
    full = pydicom.dcmread(first, stop_before_pixels=True)
    key = [(label, v) for label, tag in KEY_FIELDS for v in [_fmt(_value(full, tag))] if v != ""]
    key.append(("Instances", str(len(seen)) + (f" ({unreadable} unreadable)" if unreadable else "")))
    if along:
        key.append(("Distinct slice positions", str(len({round(a, 2) for a in along}))))
        key.append(("Slice positions along the normal (mm)", f"{min(along):.2f} to {max(along):.2f}"))
    return {"number": _fmt(_value(full, "SeriesNumber")), "description": _fmt(_value(full, "SeriesDescription")),
            "image_type": _fmt(_value(full, "ImageType")), "te": _fmt(_value(full, "EchoTime")),
            "tr": _fmt(_value(full, "RepetitionTime")), "n": len(seen), "key": key, "vary": vary,
            "dump": _dump(full), "acq_min": min(acq) if acq else None, "acq_max": max(acq) if acq else None,
            "series_time": _secs(_value(full, "SeriesTime"))}


def clean_sidecar(side):
    return {k: v for k, v in side.items() if k not in SIDECAR_HIDDEN and not SIDECAR_HIDDEN_PATTERN.search(k)}


# ---------------------------------------------------------------- page

CSS = """
:root { --size: 360px; --b: 1; --c: 1; color-scheme: dark; }
[hidden] { display: none !important; }
body { margin: 0; background: #101114; color: #d8dade; font: 14px/1.45 system-ui, "Segoe UI", sans-serif; }
header.bar { position: sticky; top: 0; z-index: 5; background: #181a1f; border-bottom: 1px solid #2a2d34;
  padding: 8px 16px; display: flex; flex-wrap: wrap; gap: 6px 18px; align-items: center; }
header.bar h1 { font-size: 16px; margin: 0 8px 0 0; }
header.bar label { display: inline-flex; gap: 6px; align-items: center; white-space: nowrap; }
main { padding: 8px 16px 60px; max-width: 1800px; }
a { color: #6ea8fe; }
table { border-collapse: collapse; margin: 4px 0; }
td, th { border-bottom: 1px solid #262930; padding: 3px 10px 3px 0; text-align: left; vertical-align: top; }
th { color: #9aa0aa; font-weight: 600; }
.index tr.focus td { color: #e3b341; }
.kv td:first-child { color: #9aa0aa; min-width: 220px; }
.kv td:last-child { white-space: pre-wrap; font-variant-numeric: tabular-nums; }
.vary-table tr.varies td:first-child { color: #e3b341; }
.dump td { font: 12px/1.35 ui-monospace, Consolas, monospace; }
.series { background: #16181d; border: 1px solid #2a2d34; border-radius: 8px; margin: 18px 0; padding: 10px 14px; }
.series.focus { border: 2px solid #d29922; }
.series h2 { font-size: 15px; margin: 0 0 4px; }
.badge { background: #d29922; color: #111; border-radius: 4px; padding: 0 6px; font-size: 12px; margin-left: 6px; }
.chips span { display: inline-block; background: #22252c; border-radius: 4px; padding: 1px 7px; margin: 0 6px 4px 0;
  font-size: 12px; color: #b9c0ca; }
.viewers { display: flex; flex-wrap: wrap; gap: 14px; margin: 6px 0; }
.viewer { background: #0b0c0e; border: 1px solid #2a2d34; border-radius: 6px; padding: 8px; outline: none; }
.viewer.active { border-color: #4c8dff; }
.vt { font-size: 12px; color: #9aa0aa; max-width: calc(var(--size) + 32px); overflow-wrap: anywhere; }
.frame { position: relative; display: inline-block; padding: 16px 18px; }
.frame img.slice { display: block; width: var(--size); height: auto; background: #000;
  filter: brightness(var(--b)) contrast(var(--c)); }
.mids img { height: 96px; width: auto; background: #000; filter: brightness(var(--b)) contrast(var(--c)); }
body.pix img.slice, body.pix .mids img { image-rendering: pixelated; }
body.radio .frame img.slice, body.radio .mids img { transform: scaleX(-1); }
.lab { position: absolute; font: 12px ui-monospace, Consolas, monospace; color: #e3b341; }
.lab.t { top: 0; left: 50%; transform: translateX(-50%); }
.lab.b { bottom: 0; left: 50%; transform: translateX(-50%); }
.lab.l { left: 3px; top: 50%; transform: translateY(-50%); }
.lab.r { right: 3px; top: 50%; transform: translateY(-50%); }
.ctl { display: flex; gap: 8px; align-items: center; font-size: 12px; color: #b9c0ca; }
.ctl input[type=range] { flex: 1; }
.mids { display: flex; gap: 8px; margin-top: 6px; }
.mids figure { margin: 0; font-size: 11px; color: #9aa0aa; }
details { margin-top: 6px; }
summary { cursor: pointer; color: #b9c0ca; }
pre { white-space: pre-wrap; font-size: 12px; background: #0b0c0e; padding: 8px; border-radius: 6px;
  max-height: 420px; overflow: auto; }
.note { color: #9aa0aa; font-size: 12.5px; max-width: 1100px; }
.err { color: #f0883e; }
"""

JS = """
(function () {
  "use strict";
  var body = document.body, $ = function (id) { return document.getElementById(id); };
  var states = [], active = null;
  function clamp(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }
  function file(st, s) { return st.p + "v" + st.v + "_s" + s + ".png"; }
  function draw(st) {
    st.img.src = file(st, st.s);
    st.sr.value = st.s;
    st.sp.textContent = (st.s + 1) + " / " + st.n;
    if (st.vr) { st.vr.value = st.k; st.vp.textContent = st.vl[st.k]; }
    st.win.textContent = st.wl[st.k];
  }
  function preload(st) {
    if (st.done[st.v]) return;
    st.done[st.v] = true;
    for (var s = 0; s < st.n; s++) { new Image().src = file(st, s); }
  }
  function setSlice(st, s, follow) {
    st.s = clamp(Math.round(s), 0, st.n - 1);
    draw(st);
    if (follow && $("link").checked) {
      var f = st.n > 1 ? st.s / (st.n - 1) : 0.5;
      states.forEach(function (o) {
        if (o !== st && !o.el.closest(".series").hidden) { o.s = clamp(Math.round(f * (o.n - 1)), 0, o.n - 1); draw(o); }
      });
    }
  }
  function setVolume(st, k) {
    st.k = clamp(k, 0, st.vols.length - 1);
    st.v = st.vols[st.k];
    draw(st);
    preload(st);
  }
  function activate(st) {
    if (active) active.el.classList.remove("active");
    active = st;
    if (st) st.el.classList.add("active");
  }
  document.querySelectorAll(".viewer[data-n]").forEach(function (el) {
    var st = {
      el: el, p: el.dataset.p, n: +el.dataset.n, vols: JSON.parse(el.dataset.vols),
      vl: JSON.parse(el.dataset.vl), wl: JSON.parse(el.dataset.w), k: 0, done: {},
      img: el.querySelector("img.slice"), sr: el.querySelector("input.s"), sp: el.querySelector(".sp"),
      vr: el.querySelector("input.v"), vp: el.querySelector(".vp"), win: el.querySelector(".win")
    };
    st.v = st.vols[0];
    st.s = Math.floor(st.n / 2);
    el._st = st;
    states.push(st);
    st.sr.addEventListener("input", function () { setSlice(st, +st.sr.value, true); });
    if (st.vr) st.vr.addEventListener("input", function () { setVolume(st, +st.vr.value); });
    el.querySelector(".frame").addEventListener("wheel", function (e) {
      e.preventDefault();
      setSlice(st, st.s + (e.deltaY > 0 ? -1 : 1), true);
    }, { passive: false });
    el.addEventListener("mouseenter", function () { activate(st); });
    el.addEventListener("focusin", function () { activate(st); });
    draw(st);
  });
  document.addEventListener("keydown", function (e) {
    if (!active || (e.target && e.target.tagName === "INPUT" && e.target.type !== "checkbox")) return;
    var step = { ArrowUp: 1, ArrowDown: -1, PageUp: 10, PageDown: -10 }[e.key];
    if (step) { e.preventDefault(); setSlice(active, active.s + step, true); return; }
    var vstep = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (vstep && active.vr) { e.preventDefault(); setVolume(active, active.k + vstep); }
    if (e.key === "Escape") activate(null);
  });
  function vars() {
    body.style.setProperty("--b", $("bri").value / 100);
    body.style.setProperty("--c", $("con").value / 100);
    body.style.setProperty("--size", $("size").value + "px");
  }
  ["bri", "con", "size"].forEach(function (id) { $(id).addEventListener("input", vars); });
  $("reset").addEventListener("click", function () { $("bri").value = 100; $("con").value = 100; vars(); });
  $("pix").addEventListener("change", function () { body.classList.toggle("pix", $("pix").checked); });
  $("radio").addEventListener("change", function () {
    body.classList.toggle("radio", $("radio").checked);
    document.querySelectorAll(".frame").forEach(function (f) {
      var l = f.querySelector(".lab.l"), r = f.querySelector(".lab.r"), t = l.textContent;
      l.textContent = r.textContent;
      r.textContent = t;
    });
  });
  var only = $("only");
  if (only) only.addEventListener("change", function () {
    document.querySelectorAll(".series:not(.focus)").forEach(function (s) { s.hidden = only.checked; });
  });
  if ("IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) preload(e.target._st); });
    }, { rootMargin: "300px" });
    states.forEach(function (st) { io.observe(st.el); });
  }
  vars();
})();
"""

HELP = ("Scroll through slices with the mouse wheel over an image, with its slider, or with the arrow keys ↑ ↓ "
        "after pointing at a viewer (Page Up and Page Down move 10 slices; ← → change the volume of a 4-D file). "
        "Images are drawn to scale in millimetres, and the letters at their edges give the subject's directions "
        "(A anterior, P posterior, L left, R right, S superior, I inferior). Each volume is shown with its own display "
        "range, printed under it; brightness and contrast change only the display. Identifiers are removed, but "
        "series descriptions and images remain, so keep this page in the private folder.")


def _e(x):
    return html.escape(str(x), quote=True)


def _kv(rows):
    return '<table class="kv">' + "".join(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>" for k, v in rows) + "</table>"


def _offset(sec, t0):
    if sec is None or t0 is None:
        return ""
    diff = sec - t0
    diff = diff + 86400 if diff < 0 else diff
    m, s = divmod(int(round(diff)), 60)
    return f"{m}:{s:02d}"


def _viewer_html(r):
    if r.get("error"):
        return (f'<div class="viewer"><div class="vt">{_e(r["label"])}</div>'
                f'<p class="err">This file could not be shown ({_e(r["error"])}).</p></div>')
    top, bottom, left, right = r["labels"]
    w, h = r["mm"]
    start = r["n"] // 2
    mids = "".join(f'<figure><img src="{_e(m["src"])}" style="aspect-ratio:{m["mm"][0]:.2f}/{m["mm"][1]:.2f}" alt="">'
                   f'<figcaption>{_e(m["plane"])} (middle)</figcaption></figure>' for m in r["mids"])
    volume = (f'<div class="ctl">volume <input type="range" class="v" min="0" max="{len(r["vols"]) - 1}" value="0">'
              f'<span class="vp"></span></div>' if len(r["vols"]) > 1 else "")
    return (f'<div class="viewer" tabindex="0" data-p="{_e(r["prefix"])}" data-n="{r["n"]}" '
            f'data-vols="{_e(json.dumps(r["vols"]))}" data-vl="{_e(json.dumps(r["vlabels"]))}" '
            f'data-w="{_e(json.dumps(r["wlabels"]))}">'
            f'<div class="vt">{_e(r["label"])}<br>{_e(r["summary"])}</div>'
            f'<div class="frame"><img class="slice" src="{_e(r["prefix"])}v{r["vols"][0]}_s{start}.png" '
            f'style="aspect-ratio:{w:.2f}/{h:.2f}" alt="slice">'
            f'<span class="lab t">{top}</span><span class="lab b">{bottom}</span>'
            f'<span class="lab l">{left}</span><span class="lab r">{right}</span></div>'
            f'<div class="ctl">slice <input type="range" class="s" min="0" max="{r["n"] - 1}" value="{start}">'
            f'<span class="sp"></span></div>{volume}<div class="win note"></div><div class="mids">{mids}</div></div>')


def _vary_html(vary):
    rows = []
    for label, counts in vary:
        shown = "; ".join(f"{v or '(absent)'} ×{c}" for v, c in counts[:8])
        if len(counts) > 8:
            shown += f"; … {len(counts) - 8} more values"
        rows.append(f'<tr class="{"varies" if len(counts) > 1 else ""}"><td>{_e(label)}</td><td>{_e(shown)}</td></tr>')
    return ('<p class="note">Fields in yellow take more than one value within the series.</p>'
            f'<table class="kv vary-table">{"".join(rows)}</table>')


def _dump_html(rows):
    body = "".join(f'<tr><td style="padding-left:{depth * 14}px">{_e(tag)}</td><td>{_e(name)}</td><td>{_e(vr)}</td>'
                   f"<td>{_e(value)}</td></tr>" for depth, tag, name, vr, value in rows)
    return f'<table class="dump"><tr><th>Tag</th><th>Name</th><th>VR</th><th>Value</th></tr>{body}</table>'


def _section(it, t0):
    d, renders = it["dicom"], it["renders"]
    chips = [f'{d["n"]} instances', d["image_type"] or "no ImageType"] if d else ["no DICOM instances found"]
    chips.append(f"{len(renders)} NIfTI file(s)" if renders else "not converted (no NIfTI)")
    start = _offset(d["acq_min"] if d and d["acq_min"] is not None else (d or {}).get("series_time"), t0)
    if start:
        chips.append(f"starts {start} (min:s) after the visit's first series")
    title = f"Series {_e(it['number'])}" + (f" — {_e(d['description'])}" if d and d["description"] else "")
    parts = [f'<section class="series{" focus" if it["focus"] else ""}" id="{it["anchor"]}">',
             f'<h2>{title}{"<span class=badge>focus</span>" if it["focus"] else ""}</h2>',
             '<p class="chips">' + "".join(f"<span>{_e(c)}</span>" for c in chips) + "</p>"]
    if renders:
        parts.append('<div class="viewers">' + "".join(_viewer_html(r) for r in renders) + "</div>")
    if d:
        times = []
        if d["acq_min"] is not None:
            times.append(("Acquisition time (min:s from the visit's first series)",
                          f"{_offset(d['acq_min'], t0)} to {_offset(d['acq_max'], t0)}"))
        if d["series_time"] is not None:
            times.append(("Series time (min:s from the visit's first series)", _offset(d["series_time"], t0)))
        parts.append(f'<details open><summary>Key DICOM fields</summary>{_kv(d["key"] + times)}</details>')
        parts.append(f'<details><summary>Values across the {d["n"]} instances</summary>{_vary_html(d["vary"])}</details>')
        parts.append("<details><summary>Full DICOM header of the first instance (identifiers removed)</summary>"
                     f'{_dump_html(d["dump"])}</details>')
    for r in renders:
        if r.get("info"):
            parts.append(f'<details><summary>NIfTI {_e(r["label"])}: header and values</summary>{_kv(r["info"])}'
                         f'<p class="note">Sidecar (identifying fields removed)</p>'
                         f'<pre>{_e(json.dumps(r["sidecar"], indent=1, default=str))}</pre></details>')
    parts.append("</section>")
    return "".join(parts)


def build_page(out_dir, title, series, focus=(), note=""):
    """Write out_dir/index.html and its images for a list of series.

    Each series is {"dicom_paths": [...], "niftis": [{"path", "label", "sidecar"}]}; focus holds series numbers
    to show first and outline. Returns counts."""
    os.makedirs(os.path.join(out_dir, "img"), exist_ok=True)
    focus = {int(f) for f in focus}
    items, n_images, n_niftis = [], 0, 0
    for i, s in enumerate(series):
        d = dicom_summary(s["dicom_paths"]) if s.get("dicom_paths") else None
        renders = []
        for j, nf in enumerate(s.get("niftis", [])):
            prefix = f"img/{i}_{j}/"
            try:
                r = render_nifti(nf["path"], os.path.join(out_dir, "img", f"{i}_{j}"), prefix)
            except Exception as e:  # noqa: BLE001 - shown on the page
                r = {"error": type(e).__name__}
            r["label"], r["sidecar"] = nf["label"], clean_sidecar(nf.get("sidecar") or {})
            renders.append(r)
            n_images += r.get("images", 0)
            n_niftis += 1
        number = d["number"] if d else ""
        if number == "":
            number = next((str(nf["sidecar"].get("SeriesNumber")) for nf in s.get("niftis", [])
                           if (nf.get("sidecar") or {}).get("SeriesNumber") is not None), "?")
        items.append({"i": i, "number": number, "dicom": d, "renders": renders, "focus": _int(number) in focus,
                      "anchor": f"series-{i}"})
    starts = [t for it in items if it["dicom"] for t in (it["dicom"]["acq_min"], it["dicom"]["series_time"])
              if t is not None]
    t0 = min(starts) if starts else None
    items.sort(key=lambda it: (not it["focus"], _int(it["number"]), it["i"]))
    index_rows = []
    for it in items:
        d = it["dicom"] or {}
        folders = ", ".join(sorted({r["label"].split("/")[0] for r in it["renders"]})) or "none"
        start = d.get("acq_min") if d.get("acq_min") is not None else d.get("series_time")
        index_rows.append(f'<tr class="{"focus" if it["focus"] else ""}"><td><a href="#{it["anchor"]}">'
                          f'{_e(it["number"])}</a></td><td>{_e(d.get("description", ""))}</td>'
                          f'<td>{_e(d.get("image_type", ""))}</td><td>{d.get("n", "")}</td><td>{_e(folders)}</td>'
                          f'<td>{_e(d.get("te", ""))}</td><td>{_e(d.get("tr", ""))}</td><td>{_offset(start, t0)}</td></tr>')
    sections, divided = [], False
    for it in items:
        if focus and not it["focus"] and not divided:
            sections.append("<h2>Other series in this visit</h2>")
            divided = True
        sections.append(_section(it, t0))
    only = ('<label><input type="checkbox" id="only"> Only the focus series</label>' if focus else "")
    page = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{_e(title)}</title><style>{CSS}</style></head><body><header class=\"bar\"><h1>{_e(title)}</h1>"
            "<label><input type=\"checkbox\" id=\"link\"> Link sliders</label>"
            "<label><input type=\"checkbox\" id=\"radio\"> Mirror left/right (radiological)</label>"
            "<label>Brightness <input type=\"range\" id=\"bri\" min=\"30\" max=\"300\" value=\"100\"></label>"
            "<label>Contrast <input type=\"range\" id=\"con\" min=\"30\" max=\"400\" value=\"100\"></label>"
            "<button id=\"reset\" type=\"button\">Reset</button>"
            "<label>Size <input type=\"range\" id=\"size\" min=\"160\" max=\"900\" value=\"360\"></label>"
            f"<label><input type=\"checkbox\" id=\"pix\"> Sharp pixels</label>{only}</header><main>"
            + (f"<p>{_e(note)}</p>" if note else "")
            + f'<p class="note">{_e(HELP)}</p><h2>Series in this visit</h2><table class="index"><tr><th>Series</th>'
            "<th>Description</th><th>Image type</th><th>Instances</th><th>NIfTI folders</th><th>TE</th><th>TR</th>"
            f"<th>Starts (min:s)</th></tr>{''.join(index_rows)}</table>{''.join(sections)}</main>"
            f"<script>{JS}</script></body></html>")
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    return {"series": len(items), "niftis": n_niftis, "images": n_images}


# ---------------------------------------------------------------- round 4 entry point

def main(argv=None):
    p = argparse.ArgumentParser(description="Pages for checking a visit's images by eye")
    p.add_argument("--project", required=True)
    p.add_argument("--private", required=True)
    p.add_argument("--visit", required=True, help="visit or session code, such as V077")
    p.add_argument("--focus", nargs="*", type=int, default=[], help="series numbers to show first and outline")
    p.add_argument("--note", default="", help="a line shown at the top of the page")
    a = p.parse_args(argv)
    import r4real

    r4real.check_outputs(a.project, a.private)
    with open(os.path.join(a.private, "visit_codes.json"), encoding="utf-8") as f:
        sessions = json.load(f)["sessions"]
    found = [k for k, c in sessions.items() if c == a.visit]
    if not found:
        raise SystemExit(f"{a.visit} is not a session code in visit_codes.json")
    sid, label = found[0].split("/", 1)
    with open(os.path.join(a.private, "index", f"{a.visit}.json"), encoding="utf-8") as f:
        index = json.load(f)
    by_key = {}
    for dirpath, _dirs, files in os.walk(os.path.join(a.project, "Niftis", "Subjects", sid, label)):
        for name in sorted(files):
            if not name.endswith(".json"):
                continue
            stem = name[:-5]
            nifti = next((os.path.join(dirpath, stem + ext) for ext in (".nii.gz", ".nii")
                          if os.path.exists(os.path.join(dirpath, stem + ext))), None)
            if not nifti:
                continue
            with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                side = json.load(f)
            shown = os.path.basename(dirpath) + "/" + stem.removeprefix(f"{sid}_{label}_")
            by_key.setdefault(side.get("source_series_key") or stem, []).append(
                {"path": nifti, "label": shown, "sidecar": side})
    series = [{"dicom_paths": index.get(k, []), "niftis": by_key.get(k, [])} for k in sorted(set(index) | set(by_key))]
    out = os.path.join(a.private, "inspect", a.visit)
    counts = build_page(out, f"Visit {a.visit}", series, focus=a.focus, note=a.note)
    print(f"{a.visit}: series={counts['series']} niftis={counts['niftis']} images={counts['images']}")
    print(f"open {os.path.join(out, 'index.html')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
