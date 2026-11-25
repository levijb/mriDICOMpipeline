"""Tier 2 converter for round 4 (plan 2.1 item 6). Run as a script:

    python fake_dcm2niix.py [dcm2niix options ...] -f PATTERN -o OUTPUT INPUT

The pipeline runs a converter path that ends in ``.py`` with ``sys.executable`` (its
``--dcm2niix-bin`` option), so tier 2 projects pass this file there.

What it writes, from the DICOM headers below INPUT (read with pydicom):

*   one uncompressed NIfTI-1 image and one JSON sidecar per series, echo and part, named by the
    ``-f`` pattern (``%o`` first instance's SOPInstanceUID, ``%s`` SeriesNumber, ``%e`` echo number;
    a phase part gets ``_ph`` appended, as dcm2niix does);
*   the voxel array exactly as stored (no axis rotation, no flips): nifti[i, j, k(, t)] is column i,
    row j of slice k, slices ordered along the slice normal, volumes in InstanceNumber order;
*   an affine from ImageOrientationPatient, ImagePositionPatient and PixelSpacing, LPS turned to RAS;
*   ``.bval``/``.bvec`` only for a series whose objects carry DiffusionBValue (the catalog writes
    that tag only for series it gives gradients);
*   sidecar fields: SeriesDescription, ProtocolName, ImageType, SeriesNumber, EchoTime,
    EchoNumber, RepetitionTime, InversionTime, FlipAngle, MRAcquisitionType, ScanningSequence,
    SequenceVariant, Manufacturer (times in seconds), plus the fields a catalog series lists in its
    ``sidecar_hints``, passed through the JSON file named by R4_FAKE_SIDECARS
    ({SeriesInstanceUID: {field: value}}).

Tests may use it only where the outcome is independent of the exact files a real converter writes
(spec, Technical Plan section 3). It shares no code with the pipeline.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

import numpy as np


def _args(argv):
    pattern, out = "%s", None
    i = 0
    rest = []
    while i < len(argv):
        a = argv[i]
        if a in ("-f", "-o", "-z", "-b", "-ba", "-l", "-c", "-d", "-x", "-m", "-p"):
            value = argv[i + 1] if i + 1 < len(argv) else ""
            if a == "-f":
                pattern = value
            elif a == "-o":
                out = value
            i += 2
            continue
        rest.append(a)
        i += 1
    if out is None or not rest:
        raise SystemExit("usage: fake_dcm2niix.py ... -o OUTPUT INPUT")
    return pattern, out, rest[-1]


def _seconds(ms):
    return None if ms is None else round(float(ms) / 1000.0, 6)


def _part(ds):
    tokens = [str(t).upper() for t in (ds.get("ImageType") or [])]
    if "P" in tokens or "PHASE" in tokens:
        return "phase"
    ge = ds.get((0x0043, 0x102F))
    if ge is not None:
        value = ge.value
        if isinstance(value, bytes):
            value = int.from_bytes(value[:2], "little", signed=True)
        if isinstance(value, (list, tuple)):
            value = value[0]
        if int(value) == 1:
            return "phase"
    return "mag"


def main(argv=None):
    import nibabel as nib
    import pydicom

    pattern, out, src = _args(list(sys.argv[1:] if argv is None else argv))
    hints = {}
    if os.environ.get("R4_FAKE_SIDECARS"):
        with open(os.environ["R4_FAKE_SIDECARS"], encoding="utf-8") as f:
            hints = json.load(f)
    groups = defaultdict(list)
    for dirpath, _dirs, files in os.walk(src):
        for name in sorted(files):
            path = os.path.join(dirpath, name)
            try:
                ds = pydicom.dcmread(path)
            except Exception:  # noqa: BLE001 - not DICOM
                continue
            if "PixelData" not in ds:
                continue
            te = ds.get("EchoTime")
            key = (str(ds.SeriesInstanceUID), round(float(te), 3) if te is not None else None, _part(ds))
            groups[key].append(ds)
    os.makedirs(out, exist_ok=True)
    by_series = defaultdict(list)
    for key in groups:
        by_series[key[0]].append(key)
    for uid, keys in sorted(by_series.items()):
        tes = sorted({k[1] for k in keys if k[1] is not None})
        for key in sorted(keys, key=lambda k: (k[1] or 0, k[2])):
            _write(nib, groups[key], key, tes, pattern, out, hints.get(uid, {}))
    return 0


def _write(nib, instances, key, tes, pattern, out, hints):
    instances.sort(key=lambda d: int(d.InstanceNumber))
    first = instances[0]
    iop = np.array([float(v) for v in first.ImageOrientationPatient])
    x, y = iop[:3], iop[3:]
    normal = np.cross(x, y)
    pos = {}
    for d in instances:
        p = round(float(np.dot([float(v) for v in d.ImagePositionPatient], normal)), 3)
        pos.setdefault(p, []).append(d)
    positions = sorted(pos)
    n_vol = min(len(v) for v in pos.values())
    rows, cols = int(first.Rows), int(first.Columns)
    data = np.zeros((cols, rows, len(positions), n_vol), dtype=np.int16)
    for k, p in enumerate(positions):
        for t, d in enumerate(pos[p][:n_vol]):
            data[:, :, k, t] = d.pixel_array.astype(np.int16).T
    if n_vol == 1:
        data = data[..., 0]
    spacing = [float(v) for v in first.PixelSpacing]
    origin = np.array([float(v) for v in pos[positions[0]][0].ImagePositionPatient])
    step = (positions[1] - positions[0]) if len(positions) > 1 else float(first.get("SliceThickness") or 1.0)
    lps = np.eye(4)
    lps[:3, 0] = x * spacing[1]
    lps[:3, 1] = y * spacing[0]
    lps[:3, 2] = normal * step
    lps[:3, 3] = origin
    affine = np.diag([-1.0, -1.0, 1.0, 1.0]) @ lps
    img = nib.Nifti1Image(data, affine)
    img.header.set_xyzt_units("mm", "sec")
    echo = (tes.index(key[1]) + 1) if key[1] is not None and key[1] in tes else 1
    name = (pattern.replace("%o", str(first.SOPInstanceUID)).replace("%s", str(int(first.get("SeriesNumber") or 0)))
            .replace("%e", str(echo)))
    if key[2] == "phase":
        name += "_ph"
    nib.save(img, os.path.join(out, name + ".nii"))
    sidecar = {"Modality": "MR", "Manufacturer": str(first.get("Manufacturer") or ""),
               "SeriesDescription": str(first.get("SeriesDescription") or ""),
               "ProtocolName": str(first.get("ProtocolName") or ""),
               "ImageType": [str(t) for t in (first.get("ImageType") or [])],
               "SeriesNumber": int(first.get("SeriesNumber") or 0),
               "ConversionSoftware": "r4-fake-dcm2niix", "ConversionSoftwareVersion": "r4fake1"}
    for field, keyword in (("EchoTime", "EchoTime"), ("RepetitionTime", "RepetitionTime"),
                           ("InversionTime", "InversionTime")):
        if first.get(keyword) is not None:
            sidecar[field] = _seconds(first.get(keyword))
    if len(tes) > 1:
        sidecar["EchoNumber"] = echo
    if first.get("FlipAngle") is not None:
        sidecar["FlipAngle"] = float(first.FlipAngle)
    for field in ("MRAcquisitionType", "ScanningSequence", "SequenceVariant"):
        value = first.get(field)
        if value is not None:
            sidecar[field] = "_".join(str(v) for v in value) if not isinstance(value, str) else str(value)
    sidecar.update(hints)
    with open(os.path.join(out, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=1)
    if first.get((0x0018, 0x9087)) is not None:
        vols = [pos[positions[0]][t] for t in range(n_vol)]
        bvals = [float(d[(0x0018, 0x9087)].value) for d in vols]
        bvecs = [[float(c) for c in d[(0x0018, 0x9089)].value] if (0x0018, 0x9089) in d else [0.0, 0.0, 0.0]
                 for d in vols]
        with open(os.path.join(out, name + ".bval"), "w", encoding="ascii") as f:
            f.write(" ".join(f"{b:g}" for b in bvals) + "\n")
        with open(os.path.join(out, name + ".bvec"), "w", encoding="ascii") as f:
            for i in range(3):
                f.write(" ".join(f"{v[i]:g}" for v in bvecs) + "\n")


if __name__ == "__main__":
    sys.exit(main())
