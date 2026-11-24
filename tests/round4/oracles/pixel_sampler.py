"""Pixel-sampling check (spec D-2, Technical Plan 8.2, plan 3.6; oracle classes C and T).

Checks whether the converter put DICOM pixels where their geometry says they belong: a set of
stored DICOM values is looked up in the NIfTI image at the same world coordinate. The result is
a report for the owner and never gates. Imports no pipeline module.

How it works (our implementation of round 4 plan 3.6):
*   A per-series generator, seeded from SHA-256 of the series key, draws (slice, row, column)
    triples until 20 samples are accepted or 1,000 triples have been tried.
*   For time series and echo-split series the first volume and one other volume (picked by the
    generator first) are used in turn.
*   A sample is accepted when its value is positive, above the median of its slice, unlike the
    values at the row-, column- and slice-mirrored positions, and, once nearly enough samples are
    in, from a new slice until three slices are represented.
*   The world coordinate follows the image plane equation of DICOM PS3.3 (pixel position from
    ImagePositionPatient, the direction cosines and PixelSpacing), with x and y negated for RAS.
*   The NIfTI affine, inverted, maps that coordinate to a voxel index; an index more than 0.1
    from an integer is a mismatch ('off grid').
*   Stored values are compared directly, or rescaled values within half a slope step when the
    rescale parameters change from slice to slice.
*   In a time series, mismatches that all reappear in one other common volume (two samples or
    more per group) are reported as 'volume order differs'.

Named category 'known GE row reversal' (owner decision at Gate G1, 2026-11-24): GE's epi_pepolar
research sequence stores reversed-polarity images with their row order reversed and no other header
change; dcm2niix corrects them on purpose (its issue 532; documented with the public dcm_qa_polar
dataset). A series is reported in this category only when both hold: its headers mark it as GE
epi_pepolar with a reversing mode (PulseSequenceName (0019,109C) containing 'epi_pepolar' and
UserData12 (0019,10B3) of 1, 2 or 3), and every mismatched sample is found in the NIfTI at the
row-mirrored DICOM pixel. A row-mirrored mismatch in any other series stays 'mismatched'.

Side effect of the mirror test, recorded for the owner: when a stack has an odd slice count its
centre slice is its own mirror, so no sample comes from it, and a stack of three slices can never
reach three slices: it ends 'under-sampled'.

Pixel data is read only for the instances that hold sampled slices, and NIfTI values are read one
voxel at a time through ``img.dataobj``. The output holds counts, statuses and voxel indices.
"""

from __future__ import annotations

import hashlib
import math
import random
import statistics

import nibabel as nib
import numpy as np
import pydicom

N_KEEP = 20
MAX_DRAWS = 1000


def seed_for(series_key):
    return int(hashlib.sha256(("r4-pixels-" + series_key).encode("ascii")).hexdigest()[:8], 16)


def _f(v):
    return None if v is None else float(v)


def read_headers(paths):
    out = []
    for path in paths:
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        it = [str(t).upper() for t in (ds.get("ImageType") or [])]
        out.append({
            "path": path,
            "ipp": [float(v) for v in ds.ImagePositionPatient] if "ImagePositionPatient" in ds else None,
            "iop": [float(v) for v in ds.ImageOrientationPatient] if "ImageOrientationPatient" in ds else None,
            "pixel": [float(v) for v in ds.PixelSpacing] if "PixelSpacing" in ds else None,
            "rows": int(ds.Rows), "cols": int(ds.Columns),
            "instance": int(ds.get("InstanceNumber") or 0),
            "te": _f(ds.get("EchoTime")),
            "slope": _f(ds.get("RescaleSlope")) if ds.get("RescaleSlope") is not None else 1.0,
            "inter": _f(ds.get("RescaleIntercept")) if ds.get("RescaleIntercept") is not None else 0.0,
            "frames": int(ds.get("NumberOfFrames") or 1),
            "mosaic": "MOSAIC" in it,
            "ge_pepolar_mode": _ge_pepolar_mode(ds),
        })
    return out


def _ge_pepolar_mode(ds):
    """UserData12 (0019,10B3) for a GE epi_pepolar series (0 fwd, 1 rev, 2 rev-fwd, 3 fwd-rev),
    else None."""
    if not str(ds.get("Manufacturer") or "").upper().startswith("GE"):
        return None
    name = ds.get((0x0019, 0x109C))
    value = name.value if name is not None else None
    if isinstance(value, bytes):
        value = value.decode("latin-1", "replace")
    if not value or "epi_pepolar" not in str(value).lower():
        return None
    mode = ds.get((0x0019, 0x10B3))
    try:
        return int(float(mode.value if not isinstance(mode.value, bytes) else mode.value.decode("latin-1").strip("\x00 ")))
    except (AttributeError, TypeError, ValueError):
        return None


def _stored_from_nifti(img, value):
    proxy = img.dataobj
    slope = getattr(proxy, "slope", 1.0)
    inter = getattr(proxy, "inter", 0.0)
    slope_trivial = slope is None or slope == 0 or (isinstance(slope, float) and math.isnan(slope)) or slope == 1
    inter_trivial = inter is None or inter == 0 or (isinstance(inter, float) and math.isnan(inter))
    if slope_trivial and inter_trivial:
        return float(value)
    stored = (float(value) - float(inter or 0.0)) / float(slope if not slope_trivial else 1.0)
    if np.issubdtype(img.get_data_dtype(), np.integer):
        return float(round(stored))
    return stored


def _voxel_value(img, index, t=None):
    i, j, k = index
    if len(img.shape) >= 4:
        return float(np.asarray(img.dataobj[i, j, k, t if t is not None else 0]))
    return float(np.asarray(img.dataobj[i, j, k]))


def world_ras(ipp, iop, pixel, r, c):
    x = np.array(iop[0:3])
    y = np.array(iop[3:6])
    p = np.array(ipp) + c * pixel[1] * x + r * pixel[0] * y
    return np.array([-p[0], -p[1], p[2]])


def sample_series(dicom_paths, niftis, series_key, n_keep=N_KEEP, max_draws=MAX_DRAWS):
    """Run the check for one series.

    ``niftis``: list of {"path": ..., "echo_ms": TE or None}. One entry (a 3D or 4D image) or one
    per echo when echoes were split into files. Returns a dict with ``status`` (matched,
    mismatched, volume order differs, under-sampled, not compared), counts and coded mismatches.
    """
    heads = read_headers(dicom_paths)
    if not heads:
        return {"status": "not compared", "reason": "no instances"}
    if any(h["frames"] > 1 for h in heads) or any(h["mosaic"] for h in heads):
        return {"status": "not compared", "reason": "mosaic or multi-frame"}
    if any(h["ipp"] is None or h["iop"] is None or h["pixel"] is None for h in heads):
        return {"status": "not compared", "reason": "missing position, orientation or spacing"}

    iop = heads[0]["iop"]
    normal = np.cross(np.array(iop[0:3]), np.array(iop[3:6]))
    # Echo groups: one per NIfTI file when echoes were split; otherwise one group.
    images = [nib.load(n["path"]) for n in niftis]
    if len(niftis) > 1:
        groups = {idx: [] for idx in range(len(niftis))}
        for h in heads:
            match = [idx for idx, n in enumerate(niftis) if n["echo_ms"] is not None and h["te"] is not None and abs(n["echo_ms"] - h["te"]) <= 0.01]
            if len(match) != 1:
                return {"status": "not compared", "reason": "instance echo time matches no single file"}
            groups[match[0]].append(h)
    else:
        groups = {0: heads}

    # Volumes: (file index, rank t). Slices: positions ascending.
    volumes = []
    layout = {}
    for fidx, members in groups.items():
        proj = sorted({round(float(np.dot(h["ipp"], normal)), 3) for h in members})
        by_pos = {p: sorted([h for h in members if round(float(np.dot(h["ipp"], normal)), 3) == p], key=lambda h: h["instance"]) for p in proj}
        n_rank = min(len(v) for v in by_pos.values())
        layout[fidx] = (proj, by_pos)
        for t in range(n_rank):
            volumes.append((fidx, t))
    rng = random.Random(seed_for(series_key))
    chosen = [volumes[0]]
    if len(volumes) >= 2:
        chosen.append(rng.choice(volumes[1:]))

    varies = len({(h["slope"], h["inter"]) for h in heads}) > 1
    cache = {}

    def slice_array(vol, k):
        key = (vol, k)
        if key not in cache:
            fidx, t = vol
            proj, by_pos = layout[fidx]
            h = by_pos[proj[k]][t]
            cache[key] = (pydicom.dcmread(h["path"]).pixel_array, h)
        return cache[key]

    kept, draws, used_slices = [], 0, set()
    while len(kept) < n_keep and draws < max_draws:
        vol = chosen[draws % len(chosen)]
        draws += 1
        proj, _ = layout[vol[0]]
        n = len(proj)
        h0 = layout[vol[0]][1][proj[0]][0]
        R, C = h0["rows"], h0["cols"]
        k, r, c = rng.randrange(n), rng.randrange(R), rng.randrange(C)
        arr, h = slice_array(vol, k)
        v = arr[r, c]
        if not (v > 0 and v > statistics.median(arr.ravel().tolist())):
            continue
        mirror_k, _ = slice_array(vol, n - 1 - k)
        if v == arr[R - 1 - r, c] or v == arr[r, C - 1 - c] or v == mirror_k[r, c]:
            continue
        if n >= 3 and len(kept) >= n_keep - 2 and len(used_slices) < 3 and k in used_slices:
            continue
        kept.append({"vol": vol, "k": k, "r": r, "c": c, "value": float(v), "h": h})
        used_slices.add(k)

    mismatches = []
    for idx, s in enumerate(kept):
        fidx, t = s["vol"]
        img = images[fidx]
        ras = world_ras(s["h"]["ipp"], s["h"]["iop"], s["h"]["pixel"], s["r"], s["c"])
        vox = np.linalg.inv(img.affine) @ np.append(ras, 1.0)
        index = [int(round(float(x))) for x in vox[:3]]
        if any(abs(float(x) - round(float(x))) > 0.1 for x in vox[:3]):
            mismatches.append({"sample": idx, "reason": "off grid", "vol": s["vol"]})
            continue
        if any(i < 0 or i >= img.shape[a] for a, i in enumerate(index)):
            mismatches.append({"sample": idx, "reason": "outside the image", "vol": s["vol"]})
            continue
        t_index = t if len(img.shape) >= 4 else None
        got = _voxel_value(img, index, t_index)
        if varies:
            expected = s["value"] * s["h"]["slope"] + s["h"]["inter"]
            ok = abs(got - expected) <= 0.5 * abs(s["h"]["slope"])
        else:
            ok = _stored_from_nifti(img, got) == s["value"]
        if not ok:
            other = []
            if len(img.shape) >= 4:
                for t2 in range(img.shape[3]):
                    if t2 == t:
                        continue
                    alt = _voxel_value(img, index, t2)
                    alt_ok = (abs(alt - (s["value"] * s["h"]["slope"] + s["h"]["inter"])) <= 0.5 * abs(s["h"]["slope"])) if varies else (_stored_from_nifti(img, alt) == s["value"])
                    if alt_ok:
                        other.append(t2)
            mismatches.append({"sample": idx, "reason": "value differs", "vol": s["vol"], "index": index, "found_in_volumes": other})

    result = {"n_kept": len(kept), "n_draws": draws, "n_mismatch": len(mismatches),
              "volumes": [list(v) for v in chosen], "first_mismatches": mismatches[:3]}
    # Known GE row reversal: every mismatch is found at the row-mirrored pixel, and the headers
    # mark the series as a reversing GE epi_pepolar acquisition.
    if mismatches:
        modes = {h["ge_pepolar_mode"] for h in heads}
        row_mirrored = 0
        for m in mismatches:
            s = kept[m["sample"]]
            img = images[s["vol"][0]]
            mirror_r = s["h"]["rows"] - 1 - s["r"]
            ras = world_ras(s["h"]["ipp"], s["h"]["iop"], s["h"]["pixel"], mirror_r, s["c"])
            vox = np.linalg.inv(img.affine) @ np.append(ras, 1.0)
            idx = [int(round(float(x))) for x in vox[:3]]
            if any(abs(float(x) - round(float(x))) > 0.1 for x in vox[:3]) or any(i < 0 or i >= img.shape[a] for a, i in enumerate(idx)):
                continue
            got = _voxel_value(img, idx, s["vol"][1] if len(img.shape) >= 4 else None)
            if _stored_from_nifti(img, got) == s["value"]:
                row_mirrored += 1
        result["n_row_mirrored"] = row_mirrored
        if row_mirrored == len(mismatches) and modes and modes <= {1, 2, 3}:
            result.update(status="known GE row reversal", ge_pepolar_mode=sorted(modes))
            return result
    if mismatches:
        groups_ = {}
        for m in mismatches:
            groups_.setdefault(tuple(m["vol"]), []).append(m)
        explained = []
        all_explained = True
        for vol, members in groups_.items():
            common = None
            for m in members:
                found = set(m.get("found_in_volumes") or [])
                common = found if common is None else common & found
            if len(members) >= 2 and common and all(m["reason"] == "value differs" for m in members):
                explained.append({"expected_volume": list(vol), "common_volumes": sorted(common)})
            else:
                all_explained = False
        if all_explained:
            result.update(status="volume order differs", groups=explained)
        else:
            result.update(status="mismatched")
    elif len(kept) < n_keep:
        result["status"] = "under-sampled"
    else:
        result["status"] = "matched"
    return result
