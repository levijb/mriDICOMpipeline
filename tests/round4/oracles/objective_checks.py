"""Objective checks for consequential errors (round 4 plan 3.5; spec D-9; oracle class C).

Run on every acquisition that ``select`` gave fate ``selected`` in a ranked role. The facts used
are the independent header reader's records and NiBabel reads, never ``select``'s own records.
Imports no pipeline module.

Flag categories, as implemented here:
*   contrast: a T1 choice whose bolus started before it was acquired, or which has no bolus time
    but an agent tag that some other MR series of the session lacks; a T1_POST choice whose bolus
    started after it was acquired;
*   derived_as_native: first ImageType value DERIVED, or a reformat or projection token present;
*   modality: structural roles whose parameters fit none of that role's families (our own coding
    of the scan-selection.md 5.4 bands), a DWI with no b-value over 50, or an SWI, QSM or GRE
    choice without GR in ScanningSequence;
*   coverage: a structural choice covering under 110 mm;
*   slice_count: rows, columns and slice count not all found among the NIfTI grid's sizes;
*   gradients: a DWI whose b-value count is not its fourth dimension;
*   copies: a mixed series, or one whose acquisition identity (5.5, rule 2) equals that of some
    other series, unless an override chose it.

Owner-accepted limit (plan 3.5): when a session holds a single MR series, the agent-tag test has
nothing to compare with, so only a bolus time can reveal a wrong precontrast choice there.
"""

from __future__ import annotations

RANKED_ROLES = ("GRE", "QSM", "SWI", "DWI", "FLAIR", "T2", "T1_POST", "T1")
DERIVED_TOKENS = {"PROJECTION IMAGE", "MNIP", "MINIP", "MIN IP", "MIP", "MPR", "REFORMATTED"}
MIN_EXTENT_MM = 110.0
COVERAGE_ROLES = ("T1", "T1_POST", "T2", "FLAIR")


# ---------------------------------------------------------------------------- 5.4 family bands

def _tokens(rec, key):
    return set(rec.get(key) or [])


def _num(rec, key):
    value = rec.get(key)
    return None if value is None else float(value)


def inversion(rec):
    ti = _num(rec, "ti_ms")
    return "IR" in _tokens(rec, "scan_seq") or (ti is not None and ti > 0)


def prepared(rec):
    return inversion(rec) or "MP" in _tokens(rec, "seq_var") or "IR_GEMS" in _tokens(rec, "scan_opt")


def gradient_echo(rec):
    ss = _tokens(rec, "scan_seq")
    return "GR" in ss or "RM" in ss


def spin_echo(rec):
    ss = _tokens(rec, "scan_seq")
    return "SE" in ss or "RM" in ss


def inversion_spin_echo(rec):
    ss = _tokens(rec, "scan_seq")
    return inversion(rec) and (spin_echo(rec) or "IR" in ss) and not ({"GR", "EP"} & ss)


def _between(value, low, high):
    return value is not None and low <= value <= high


def families(rec, te_ms=None):
    """Every structural family whose 5.4 band the parameters fall in. For a volume of a multi-echo
    series, ``te_ms`` gives that volume's echo time (5.4 uses the echo of the volume itself)."""
    acq = rec.get("mr_acq")
    tr, ti = _num(rec, "tr_ms"), _num(rec, "ti_ms")
    te = te_ms if te_ms is not None else _num(rec, "te_ms")
    ss, sv = _tokens(rec, "scan_seq"), _tokens(rec, "seq_var")
    single_echo = not rec.get("echoes")
    no_ti = ti is None or ti == 0
    out = []
    if acq == "3D" and gradient_echo(rec) and prepared(rec) and _between(ti, 300, 1300) and te is not None and te <= 10 and tr is not None and tr < 3000:
        out.append("T1_3D_IRGRE")
    elif acq == "3D" and "GR" in ss and "MP" in sv and no_ti and te is not None and te <= 10 and tr is not None and tr <= 50:
        out.append("T1_3D_IRGRE")
    if acq == "3D" and "GR" in ss and not prepared(rec) and single_echo and te is not None and te <= 10 and tr is not None and tr <= 50:
        out.append("T1_3D_GRE")
    if acq == "2D" and inversion_spin_echo(rec) and _between(ti, 600, 1300) and te is not None and te <= 40:
        out.append("T1_2D_IR")
    if acq == "2D" and "SE" in ss and not inversion(rec) and tr is not None and tr <= 1000 and te is not None and te <= 30:
        out.append("T1_2D_SE")
    flair = inversion_spin_echo(rec) and _between(ti, 1400, 3000) and tr is not None and tr >= 4000 and te is not None and te >= 70
    if acq == "3D" and flair:
        out.append("FLAIR_3D")
    if acq == "2D" and flair:
        out.append("FLAIR_2D")
    t2_base = spin_echo(rec) and not inversion(rec) and "EP" not in ss
    if acq == "3D" and t2_base and tr is not None and tr >= 1500 and _between(te, 60, 500):
        out.append("T2_3D_TSE")
    if acq == "2D" and t2_base and tr is not None and tr >= 2000 and _between(te, 60, 200):
        out.append("T2_2D_TSE")
    return out


T1_FAMILIES = {"T1_2D_SE", "T1_2D_IR", "T1_3D_GRE", "T1_3D_IRGRE"}
ROLE_FAMILIES = {
    "T1": T1_FAMILIES,
    "T1_POST": T1_FAMILIES,
    "FLAIR": {"FLAIR_3D", "FLAIR_2D"},
    "T2": {"T2_3D_TSE", "T2_2D_TSE"},
}


# ---------------------------------------------------------------------------- identity (5.5 rule 2)

def _within_pct(a, b, pct=0.01):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    a, b = float(a), float(b)
    if a == b:
        return True
    return abs(a - b) <= pct * max(abs(a), abs(b))


def same_identity(a, b):
    """5.5 rule 2 acquisition identity, plus the same TE and part (which makes two series
    duplicates rather than components)."""
    if a.get("mixed") or b.get("mixed"):
        return False
    if not a.get("for_sha256") or a.get("for_sha256") != b.get("for_sha256"):
        return False
    if a.get("acq_rel_s") is None or b.get("acq_rel_s") is None or abs(a["acq_rel_s"] - b["acq_rel_s"]) > 1.0:
        return False
    for key in ("orientation", "rows", "cols", "n_positions"):
        if a.get(key) != b.get(key):
            return False
    for key in ("pos_min", "pos_max"):
        if a.get(key) is None or b.get(key) is None or abs(a[key] - b[key]) > 0.1:
            return False
    for key in ("tr_ms", "ti_ms", "flip_deg"):
        if not _within_pct(a.get(key), b.get(key)):
            return False
    if not _within_pct(a.get("te_ms"), b.get("te_ms")):
        return False
    return dict(a.get("parts") or {}).keys() == dict(b.get("parts") or {}).keys()


def duplicates_of(rec, session_records):
    return [o["series_key"] for o in session_records if o["series_key"] != rec["series_key"] and same_identity(rec, o)]


# ---------------------------------------------------------------------------- the checks

def check_choice(role, members, session_records, *, member_te=None, volume=None, extent_mm=None,
                 chosen_by_override=False):
    """Flags for one chosen acquisition.

    ``members``: header reader records of the acquisition's member series.
    ``session_records``: every MR series record of the session (for the agent-tag rule and the
    duplicate check). ``member_te``: {series_key: TE} for multi-echo members. ``volume``:
    {"shape": NIfTI shape, "rows", "cols", "n_slices", "n_bvals"} for the primary volume.
    ``extent_mm``: the geometry oracle's extent (falls back to the member record's extent_mm).
    Returns a list of {"category", "series_key", "detail"}.
    """
    if role not in RANKED_ROLES:
        raise ValueError(f"not a ranked role: {role}")
    flags = []

    def flag(category, rec, detail):
        flags.append({"category": category, "series_key": rec.get("series_key") if rec else None, "detail": detail})

    for rec in members:
        # Contrast.
        bolus, acq = rec.get("bolus_rel_s"), rec.get("acq_rel_s")
        if role == "T1":
            if bolus is not None and acq is not None and bolus < acq:
                flag("contrast", rec, "bolus before acquisition on a precontrast choice")
            elif bolus is None and rec.get("contrast_agent"):
                others = [o for o in session_records if o.get("series_key") != rec.get("series_key")]
                if any(o.get("contrast_agent") is False for o in others):
                    flag("contrast", rec, "agent tag on a precontrast choice, not on every series")
        if role == "T1_POST" and bolus is not None and acq is not None and bolus > acq:
            flag("contrast", rec, "bolus after acquisition on a postcontrast choice")
        # Derived chosen as native.
        value1 = set(rec.get("image_type_value1") or [])
        if "DERIVED" in value1 or (set(rec.get("image_type") or []) & DERIVED_TOKENS):
            flag("derived_as_native", rec, "derived ImageType")
        # Copies.
        if not chosen_by_override:
            if rec.get("mixed"):
                flag("copies", rec, "mixed series")
            elif duplicates_of(rec, session_records):
                flag("copies", rec, "duplicate acquisition identity")

    primary = members[0] if members else None
    # Modality.
    if primary is not None and not primary.get("mixed"):
        if role in ROLE_FAMILIES:
            te = (member_te or {}).get(primary.get("series_key"))
            if not set(families(primary, te)) & ROLE_FAMILIES[role]:
                flag("modality", primary, f"parameters match no {role} family")
        elif role == "DWI":
            n_b = (volume or {}).get("b_values")
            if n_b is not None and not any(b > 50 for b in n_b):
                flag("modality", primary, "no b-value above 50")
        elif role in ("SWI", "QSM", "GRE"):
            if "GR" not in _tokens(primary, "scan_seq"):
                flag("modality", primary, "ScanningSequence lacks GR")
    # Coverage.
    if role in COVERAGE_ROLES and primary is not None:
        extent = extent_mm if extent_mm is not None else primary.get("extent_mm")
        if extent is None or extent < MIN_EXTENT_MM:
            flag("coverage", primary, f"extent {extent} below {MIN_EXTENT_MM}")
    # Slice count and gradients.
    if volume is not None:
        shape = list(volume.get("shape") or [])
        want = [volume.get("rows"), volume.get("cols"), volume.get("n_slices")]
        if None not in want and shape:
            remaining = list(shape[:3])
            for w in want:
                if w in remaining:
                    remaining.remove(w)
                else:
                    flag("slice_count", primary, f"grid {shape[:3]} does not contain {want}")
                    break
        if role == "DWI" and volume.get("n_bvals") is not None and len(shape) >= 4 and volume["n_bvals"] != shape[3]:
            flag("gradients", primary, f"{volume['n_bvals']} b-values for {shape[3]} volumes")
        if role == "DWI" and volume.get("n_bvals") is not None and len(shape) < 4 and volume["n_bvals"] != 1:
            flag("gradients", primary, f"{volume['n_bvals']} b-values for a 3D image")
    return flags
