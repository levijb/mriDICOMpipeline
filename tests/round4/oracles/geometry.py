"""Geometry oracle (spec Detailed Implementation Plan 2.1 item 11; oracle class C).

Calculations from DICOM PS3.3 C.7.6.2 (Image Plane Module) and the NIfTI-1 header definition,
plus the native-sampling rules of ``scan-selection.md`` 5.3. Imports no pipeline module.

Conventions:
*   DICOM patient coordinates are LPS; NIfTI (and NiBabel affines) are RAS. ``lps_to_ras``
    negates x and y.
*   ``iop`` is ImageOrientationPatient: the row direction cosine X (direction of increasing
    column index) then the column direction cosine Y (direction of increasing row index).
*   ``pixel_spacing`` is PixelSpacing: [row spacing (between rows, along Y), column spacing
    (between columns, along X)].
"""

from __future__ import annotations

import math
import statistics

import numpy as np


def unit(v):
    v = np.asarray(v, dtype=float)
    n = float(np.linalg.norm(v))
    if n == 0:
        raise ValueError("zero vector")
    return v / n


def slice_normal(iop):
    """Unit normal X x Y of the image plane (PS3.3 C.7.6.2.1.1)."""
    x = np.asarray(iop[0:3], dtype=float)
    y = np.asarray(iop[3:6], dtype=float)
    return unit(np.cross(x, y))


def pixel_to_patient(ipp, iop, pixel_spacing, row, col):
    """Patient (LPS) position of pixel (row, col): P = S + c*dc*X + r*dr*Y (PS3.3 C.7.6.2.1.1)."""
    s = np.asarray(ipp, dtype=float)
    x = np.asarray(iop[0:3], dtype=float)
    y = np.asarray(iop[3:6], dtype=float)
    dr, dc = float(pixel_spacing[0]), float(pixel_spacing[1])
    return s + col * dc * x + row * dr * y


def lps_to_ras(p):
    p = np.asarray(p, dtype=float)
    return np.array([-p[0], -p[1], p[2]])


def orientation_label(iop):
    """Plane label from the dominant axis of the plane normal: x gives SAG, y COR, z AX; a dominant
    component under 0.95 gives 'oblique' (scan-selection.md 5.1, orientation)."""
    n = np.abs(slice_normal(iop))
    i = int(np.argmax(n))
    if n[i] < 0.95:
        return "oblique"
    return ("SAG", "COR", "AX")[i]


def projected_positions(ipps, iop, tol=0.001):
    """Distinct projections of ImagePositionPatient values on the slice normal, ascending."""
    n = slice_normal(iop)
    values = sorted(float(np.dot(np.asarray(p, dtype=float), n)) for p in ipps)
    out = []
    for v in values:
        if not out or v - out[-1] > tol:
            out.append(v)
    return out


def stack_facts(ipps, iop, thick_mm):
    """n_positions, pos_min, pos_max, step_mm (median), extent_mm (5.3), uniform_spacing (5.3)."""
    pos = projected_positions(ipps, iop)
    steps = [b - a for a, b in zip(pos, pos[1:])]
    step = statistics.median(steps) if steps else None
    extent = None
    if step is not None and thick_mm is not None:
        extent = (len(pos) - 1) * step + thick_mm
    uniform = None
    if steps:
        uniform = (max(steps) - min(steps)) <= 0.05 + 0.01 * step
    return {"n_positions": len(pos), "pos_min": pos[0], "pos_max": pos[-1], "step_mm": step,
            "extent_mm": extent, "uniform_spacing": uniform}


def native_voxel(rows, cols, pixel_spacing, acq_matrix, phase_dir, thick_mm=None):
    """Acquired (native) sampling: the field of view on each in-plane axis divided by the matching
    AcquisitionMatrix entry (scan-selection.md 5.3; PS3.3 C.8.3.1 orders AcquisitionMatrix as
    frequency rows, frequency columns, phase rows, phase columns).

    Returns {"row": ..., "col": ..., "slice": ...}. With ``phase_dir`` None both assignments are
    returned as {"if_row": {...}, "if_col": {...}}. With no matrix, returns None.
    """
    if acq_matrix is None:
        return None
    n_freq = [v for v in acq_matrix[0:2] if v]
    n_phase = [v for v in acq_matrix[2:4] if v]
    if len(n_freq) != 1 or len(n_phase) != 1:
        raise ValueError("AcquisitionMatrix must have one nonzero frequency and one nonzero phase entry")
    n_freq, n_phase = n_freq[0], n_phase[0]
    fov_row = cols * float(pixel_spacing[1])
    fov_col = rows * float(pixel_spacing[0])
    if_row = {"row": fov_row / n_phase, "col": fov_col / n_freq, "slice": thick_mm}
    if_col = {"row": fov_row / n_freq, "col": fov_col / n_phase, "slice": thick_mm}
    if phase_dir == "ROW":
        return if_row
    if phase_dir == "COL":
        return if_col
    return {"if_row": if_row, "if_col": if_col}


def interpolated_grid(pixel_spacing, native):
    """5.3 warning: the grid is finer than 0.75 x native on either axis. native['row'] lies along
    the row direction (column spacing, pixel_spacing[1]); native['col'] along the column direction
    (row spacing, pixel_spacing[0])."""
    return (float(pixel_spacing[1]) < 0.75 * native["row"]) or (float(pixel_spacing[0]) < 0.75 * native["col"])


def native_max(native):
    vals = [native["row"], native["col"]] + ([native["slice"]] if native.get("slice") is not None else [])
    return max(vals)


# ---------------------------------------------------------------------------- NIfTI side

def axis_directions(affine):
    """For each voxel axis of a NIfTI affine (RAS), its unit direction in world space."""
    a = np.asarray(affine, dtype=float)[:3, :3]
    return [unit(a[:, i]) for i in range(3)]


def axis_for_direction(affine, direction_ras):
    """Index of the voxel axis most parallel (either sign) to a world direction (RAS)."""
    d = unit(direction_ras)
    dots = [abs(float(np.dot(v, d))) for v in axis_directions(affine)]
    return int(np.argmax(dots)), max(dots)


def dicom_axes_in_nifti(affine, iop):
    """Map the DICOM row direction (X), column direction (Y) and slice normal to NIfTI voxel axes.

    Returns {"col_index_axis": i, "row_index_axis": j, "slice_axis": k}: the NIfTI axis along
    which the DICOM column index runs (direction X), the row index runs (direction Y), and the
    slice index runs (the normal).
    """
    x_ras = lps_to_ras(iop[0:3])
    y_ras = lps_to_ras(iop[3:6])
    n_ras = lps_to_ras(slice_normal(iop))
    i, _ = axis_for_direction(affine, x_ras)
    j, _ = axis_for_direction(affine, y_ras)
    k, _ = axis_for_direction(affine, n_ras)
    return {"col_index_axis": i, "row_index_axis": j, "slice_axis": k}


def nifti_slice_count(shape, affine, iop):
    """The NIfTI image's size along the axis that carries the DICOM slice normal."""
    k = dicom_axes_in_nifti(affine, iop)["slice_axis"]
    return int(shape[k])


def world_to_voxel(affine, ras):
    """Continuous voxel index of a RAS world position (inverse affine)."""
    inv = np.linalg.inv(np.asarray(affine, dtype=float))
    p = np.append(np.asarray(ras, dtype=float), 1.0)
    return (inv @ p)[:3]


def grid_corners(shape, affine):
    """World positions (RAS) of the 8 corner voxel centres."""
    a = np.asarray(affine, dtype=float)
    out = []
    for i in (0, shape[0] - 1):
        for j in (0, shape[1] - 1):
            for k in (0, shape[2] - 1):
                out.append((a @ np.array([i, j, k, 1.0]))[:3])
    return out


def voxel_sizes(affine):
    a = np.asarray(affine, dtype=float)[:3, :3]
    return [float(np.linalg.norm(a[:, i])) for i in range(3)]


def same_grid(shape_a, affine_a, shape_b, affine_b):
    """BIDS design 4.3 rule as the plan states it (R4-GEO-02): same matrix (the first three
    dimensions, in order), and every corner within half a voxel. Returns (decision, details)."""
    if tuple(shape_a[:3]) != tuple(shape_b[:3]):
        return False, "matrix_differs"
    half = 0.5 * min(voxel_sizes(affine_a) + voxel_sizes(affine_b))
    ca = grid_corners(shape_a, affine_a)
    cb = grid_corners(shape_b, affine_b)
    worst = max(float(np.linalg.norm(p - q)) for p, q in zip(ca, cb))
    return worst <= half, f"worst_corner_mm={worst:.4f}"


def same_physical_grid_any_order(shape_a, affine_a, shape_b, affine_b):
    """True when both images cover the same voxel centres, whatever the axis order or flips."""
    ca = sorted(tuple(round(float(v), 3) for v in p) for p in grid_corners(shape_a, affine_a))
    cb = sorted(tuple(round(float(v), 3) for v in p) for p in grid_corners(shape_b, affine_b))
    if sorted(shape_a[:3]) != sorted(shape_b[:3]):
        return False
    return all(math.dist(p, q) < 0.5 * min(voxel_sizes(affine_a)) for p, q in zip(ca, cb))
