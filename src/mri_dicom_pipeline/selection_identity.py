"""Scan selection, part 1: what each published volume is (design: scan selection, section 5).

Pure functions over normalized records: native sampling, sequence families,
volume roles, acquisitions (echoes and magnitude/phase parts of one run),
links from derived volumes to their source, and contrast phase. Records come
from selection_metadata; decisions are made in selection_rules.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import math
import re

# Description tokens kept in reports; every other word is dropped (D6).
VOCABULARY = frozenset(
    "T1 T2 FLAIR MPRAGE RAGE BRAVO SPGR FSPGR IR TFE TFL SPACE CUBE VISTA 3D 2D VOL ISO "
    "AX AXIAL SAG SAGITTAL COR CORONAL OBL PRE POST GAD GADO CONTRAST C REFORMAT MPR MIP MINIP "
    "DWI DTI DIFFUSION TRACE ADC EADC SWI SWAN QSM PHA PHASE MAG REPEAT RPT REDO RETAKE MOCO".split())

# Classifier labels that keep their role whatever the header parameters say (5.4, rule 2).
SPECIALIZED = frozenset({"ADC", "EADC", "SWI", "SWI_PHASE", "SWI_MINIP", "QSM", "GRE", "MRA", "MRV", "PERFUSION"})
STRUCTURAL_LABELS = frozenset({"T1", "T1_POST", "T2", "FLAIR", "DWI"})
# Roles whose volumes are derived by definition (5.4).
DERIVED_ROLES = frozenset({"ADC", "EADC", "SWI_MINIP"})
DERIVED_TOKENS = frozenset({"REFORMATTED", "MPR", "MIP", "MIN IP", "MINIP", "MNIP", "PROJECTION IMAGE",
                            "ADC", "EADC", "TRACEW", "FA"})
REFORMAT_TOKENS = frozenset({"REFORMATTED", "MPR"})
PROJECTION_TOKENS = frozenset({"MIP", "MIN IP", "MINIP", "MNIP", "PROJECTION IMAGE"})
VARIANT_TOKENS = frozenset({"DIS2D", "DIS3D", "ND", "NORM"})
PART_TOKENS = {"M": "mag", "MAGNITUDE": "mag", "P": "phase", "PHASE": "phase", "R": "real", "REAL": "real",
               "I": "imag", "IMAGINARY": "imag"}
# Roles that never get acquisitions: every volume is kept (6.1).
KEPT_ROLES = frozenset({"LOCALIZER", "MRA", "MRV", "PERFUSION"})
# Parent roles of a derived group's role (5.6).
PARENT_ROLES = {"T1": {"T1", "T1_POST"}, "T1_POST": {"T1", "T1_POST"}, "T2": {"T2"}, "FLAIR": {"FLAIR"},
                "GRE": {"GRE"}, "DWI": {"DWI"}, "SWI": {"SWI"}, "SWI_PHASE": {"SWI"}, "ADC": {"DWI"},
                "EADC": {"DWI"}, "SWI_MINIP": {"SWI"}, "QSM": {"QSM", "SWI", "GRE"}}
# Ladder rungs per derived kind (5.6).
RUNGS = {"reformat": ("reference", "frame_of_reference", "parameters", "acquisition_time", "series_order"),
         "projection": ("reference", "frame_of_reference", "z_range", "parameters", "acquisition_time",
                        "series_order"),
         "map": ("reference", "frame_of_reference", "geometry", "parameters", "acquisition_time", "series_order")}
STRUCTURAL_ROLE = {"T1": "T1", "FLAIR": "FLAIR", "T2": "T2"}


def short_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _tokens(text):
    return [t for t in re.split(r"[^A-Z0-9*]+", f" {text or ''} ".upper()) if t]


def description_tokens(description, protocol):
    """(sorted vocabulary tokens, count of dropped words) of a series description and protocol name."""
    tokens = _tokens(f"{description or ''} {protocol or ''}")
    return sorted({t for t in tokens if t in VOCABULARY}), sum(t not in VOCABULARY for t in tokens)


def description_phase(description, protocol):
    """'post', 'pre' or None from description words; computed in memory, text discarded (5.7, rule 3)."""
    raw = f" {description or ''} {protocol or ''} ".upper()
    tokens = _tokens(raw)
    if "POST" in tokens or "+C" in raw:
        return "post"
    if {"PRE", "NON", "WO", "PRECONTRAST", "NONCONTRAST"} & set(tokens):
        return "pre"
    if "GAD" in tokens or any("GADO" in t or "CONTRAST" in t for t in tokens):
        return "post"
    return None


def close(a, b, rel=0.01, absolute=1e-6):
    """Equal within a relative tolerance; two missing values are equal, one missing value is not."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= max(rel * max(abs(a), abs(b)), absolute)


def orientation(iop):
    """AX, COR, SAG or oblique from ImageOrientationPatient, or None."""
    if not iop or len(iop) != 6:
        return None
    r, c = iop[:3], iop[3:]
    n = (r[1] * c[2] - r[2] * c[1], r[2] * c[0] - r[0] * c[2], r[0] * c[1] - r[1] * c[0])
    norm = math.sqrt(sum(x * x for x in n))
    if not norm:
        return None
    axis = max(range(3), key=lambda i: abs(n[i]))
    return ("SAG", "COR", "AX")[axis] if abs(n[axis]) >= 0.95 * norm else "oblique"


def native_sampling(rec):
    """Native resolution from AcquisitionMatrix and field of view, never the reconstructed grid (5.3).

    'inplane' lists (row_mm, col_mm) per possible phase-direction assignment (one when the
    direction is known, two when it is missing, none without a matrix).
    """
    pixel, rows, cols = rec.get("pixel_mm"), rec.get("rows"), rec.get("cols")
    matrix = rec.get("acq_matrix") or [0, 0, 0, 0]
    n_freq = (matrix[0] or matrix[1]) if len(matrix) == 4 else 0
    n_phase = (matrix[2] or matrix[3]) if len(matrix) == 4 else 0
    inplane = []
    if pixel and len(pixel) == 2 and rows and cols and n_freq and n_phase:
        fov_row, fov_col = cols * pixel[1], rows * pixel[0]
        options = {"ROW": (fov_row / n_phase, fov_col / n_freq), "COL": (fov_row / n_freq, fov_col / n_phase)}
        direction = rec.get("phase_dir")
        inplane = [options[direction]] if direction in options else [options["ROW"], options["COL"]]
    thick = rec.get("thick_mm")
    step = rec.get("step_mm") or rec.get("sbs_mm")
    gap = round(step - thick, 4) if step is not None and thick is not None else None
    max_inplane = max(max(pair) for pair in inplane) if inplane else None
    native_max = max(max_inplane, thick) if max_inplane is not None and thick is not None else None
    interpolated = bool(inplane and pixel and len(pixel) == 2
                        and (pixel[1] < 0.75 * inplane[0][0] or pixel[0] < 0.75 * inplane[0][1]))
    return {"inplane": inplane, "max_inplane": max_inplane, "slice": thick, "gap": gap, "max": native_max,
            "interpolated": interpolated}


def native_text(sampling):
    if not sampling["inplane"] or sampling["slice"] is None:
        return ""
    row, col = sampling["inplane"][0]
    return f"{row:.2f}x{col:.2f}x{sampling['slice']:.2f}"


def structural_family(p):
    """The T1/FLAIR/T2 sequence family of a parameter set, or None (5.4 table). Times in ms.

    Vendor conventions in real exports (2026-11-19): Siemens can omit IR when it gives an inversion
    time (MPRAGE, SPACE FLAIR); Philips gives IR without SE for an inversion-recovery spin echo and
    no inversion time for a 3D T1 TFE; GE research-mode sequences give RM instead of the sequence
    type, so the timing decides.
    """
    mr, seq, var, opt = p.get("mr_acq"), set(p.get("scan_seq") or ()), set(p.get("seq_var") or ()), \
        set(p.get("scan_opt") or ())
    tr, te, ti = p.get("tr"), p.get("te"), p.get("ti")
    research = "RM" in seq
    inversion = "IR" in seq or bool(ti)
    prepared = inversion or "MP" in var or "IR_GEMS" in opt
    gradient_echo, spin_echo = "GR" in seq or research, "SE" in seq or research
    inversion_spin_echo = inversion and (spin_echo or "IR" in seq) and not seq & {"GR", "EP"}

    def known(*values):
        return all(v is not None for v in values)

    if mr == "3D" and gradient_echo and prepared and known(ti, te, tr) and 300 <= ti <= 1300 and te <= 10 \
            and tr < 3000:
        return "T1_3D_IRGRE"
    if mr == "3D" and "GR" in seq and "MP" in var and ti is None and known(te, tr) and te <= 10 and tr <= 50:
        return "T1_3D_IRGRE"  # Philips 3D T1 TFE; its TR is one shot's
    if mr == "3D" and "GR" in seq and not prepared and p.get("single_echo", True) and known(te, tr) \
            and te <= 10 and tr <= 50:
        return "T1_3D_GRE"
    if mr == "2D" and inversion_spin_echo and known(ti, te) and 600 <= ti <= 1300 and te <= 40:
        return "T1_2D_IR"
    if mr == "2D" and "SE" in seq and not inversion and known(tr, te) and tr <= 1000 and te <= 30:
        return "T1_2D_SE"
    if inversion_spin_echo and known(ti, tr, te) and 1400 <= ti <= 3000 and tr >= 4000 and te >= 70:
        return {"3D": "FLAIR_3D", "2D": "FLAIR_2D"}.get(mr)
    if spin_echo and not inversion and "EP" not in seq and known(tr, te):
        if mr == "3D" and tr >= 1500 and 60 <= te <= 500:
            return "T2_3D_TSE"
        if mr == "2D" and tr >= 2000 and 60 <= te <= 200:
            return "T2_2D_TSE"
    return None


def family_role(family):
    return STRUCTURAL_ROLE.get(family.split("_", 1)[0]) if family else None


def gradient_stats(bvals, bvecs, n_volumes):
    """Diffusion facts from .bval/.bvec values (5.2): b0 count, shells, b_max, directions per shell."""
    mismatch = (len(bvecs) != 3 or any(len(row) != len(bvals) for row in bvecs)
                or (n_volumes is not None and len(bvals) != n_volumes))
    shells = {}
    for i, b in enumerate(bvals):
        if b <= 50:
            continue
        found = shells.setdefault(int(round(b / 50.0) * 50), [])
        if mismatch:
            continue
        v = (bvecs[0][i], bvecs[1][i], bvecs[2][i])
        norm = math.sqrt(sum(x * x for x in v))
        if norm < 1e-6:
            continue  # a trace or derived volume: b > 0 with no direction
        u = tuple(x / norm for x in v)
        if not any(abs(sum(a * b2 for a, b2 in zip(u, w))) >= 0.995 for w in found):
            found.append(u)
    return {"n_volumes": len(bvals), "b0": sum(b <= 50 for b in bvals), "shells": sorted(shells),
            "b_max": max(shells) if shells else None,
            "n_dirs": max((len(d) for d in shells.values()), default=0), "mismatch": mismatch}


def dwi_family(gradients, min_tensor_directions):
    if gradients is None:
        return "DWI_UNKNOWN"
    if not gradients["shells"]:
        return "DWI_B0_ONLY"
    return "DWI_TENSOR" if gradients["n_dirs"] >= min_tensor_directions else "DWI_TRACE"


def generic_family(role, mr_acq):
    return f"{'SWI' if role == 'SWI_PHASE' else role}_{mr_acq if mr_acq in ('2D', '3D') else 'UNKNOWN'}"


def _sequence_tokens(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).upper() for v in value]
    return [t for t in re.split(r"[\\_ ]+", str(value).upper()) if t]


def _ms(seconds):
    return round(float(seconds) * 1000, 4) if isinstance(seconds, (int, float)) else None


@dataclass(eq=False)
class Volume:
    """One published volume (a .nii.gz with its sidecar) and what selection made of it."""
    relpath: str
    sha256: str
    folder: str
    token: str
    companions: list
    sidecar: dict
    dims: list
    gradients: dict | None
    series: dict | None
    json_sha256: str = ""
    vid: str = ""
    role: str | None = None
    family: str = ""
    role_rule: int = 6
    kind: str = "native"
    part: str = "mag"
    te: float | None = None
    image_type: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    acq: "Acquisition | None" = None
    group: "Group | None" = None
    t1_family: bool = False

    @property
    def mixed(self):
        return bool(self.series and self.series.get("mixed"))

    def s(self, key, default=None):
        """A field of the series record (None for a missing or mixed record)."""
        return default if not self.series or self.series.get("mixed") else self.series.get(key, default)


@dataclass(eq=False)
class Acquisition:
    members: list
    id: str
    role: str
    primary: Volume
    n_echoes: int
    has_phase: bool
    phase_only: bool
    issues: list
    duplicate_of: set = field(default_factory=set)
    phase: str = "unknown"
    evidence: str = "none"
    warnings: list = field(default_factory=list)

    @property
    def family(self):
        return self.primary.family


@dataclass(eq=False)
class Group:
    """The derived volumes of one series, linked to a parent acquisition or not (5.6)."""
    members: list
    id: str
    role: str
    kind: str
    series: dict | None
    link_state: str = "no_parent"
    link_rung: str = ""
    parent: Acquisition | None = None
    candidates: list = field(default_factory=list)
    phase: str = "unknown"
    evidence: str = "none"


def setup_volume(v, min_tensor_directions):
    """Fill a volume's id, part, echo, kind, role and family (5.2, 5.4)."""
    v.vid = short_hash(f"{v.relpath}:{v.sha256}")
    s, sc = v.series, v.sidecar
    if s is None:
        v.issues.append("metadata_unavailable")
    elif s.get("multiframe"):
        v.issues.append("metadata_unavailable")
    if v.mixed:
        v.issues.append("mixed_series")
    own = _sequence_tokens(sc.get("ImageType")) if sc.get("ImageType") else list(v.s("image_type", []))
    sidecar_parts = {PART_TOKENS[t] for t in own if t in PART_TOKENS}
    # Siemens XA keeps its distortion and normalization tokens in ImageTypeText, which the sidecar omits.
    v.image_type = own + [t for t in v.s("image_type", []) if t in VARIANT_TOKENS and t not in own]
    series_parts = {k for k, n in (v.s("parts") or {}).items() if n}
    if len(sidecar_parts) == 1:
        v.part = next(iter(sidecar_parts))
    elif len(series_parts) <= 1:
        v.part = next(iter(series_parts), "mag")
    else:
        v.part = "unknown"
        v.issues.append("metadata_unavailable")
    echoes = v.s("echoes") or []
    sidecar_te = _ms(sc.get("EchoTime"))
    if len(echoes) > 1:
        match = [e["te_ms"] for e in echoes if sidecar_te is not None and close(e["te_ms"], sidecar_te)]
        v.te = match[0] if match else None
        if not match:
            v.issues.append("metadata_unavailable")
    elif v.series and not v.mixed:
        v.te = v.s("te_ms")
    else:
        v.te = sidecar_te
    if v.mixed or v.series is None:
        params = {"mr_acq": sc.get("MRAcquisitionType"), "scan_seq": _sequence_tokens(sc.get("ScanningSequence")),
                  "seq_var": _sequence_tokens(sc.get("SequenceVariant")), "scan_opt": [],
                  "tr": _ms(sc.get("RepetitionTime")), "te": v.te, "ti": _ms(sc.get("InversionTime")),
                  "single_echo": sc.get("EchoNumber") in (None, 1)}
    else:
        params = {"mr_acq": v.s("mr_acq"), "scan_seq": v.s("scan_seq"), "seq_var": v.s("seq_var"),
                  "scan_opt": v.s("scan_opt"), "tr": v.s("tr_ms"), "te": v.te, "ti": v.s("ti_ms"),
                  "single_echo": len(echoes) <= 1}
    mr_acq = params["mr_acq"]
    structural = structural_family(params)
    label = v.folder
    if label == "LOCALIZER" or v.s("localizer") or "LOCALIZER" in v.image_type:
        v.role, v.role_rule = "LOCALIZER", 1
    elif label in SPECIALIZED:
        v.role, v.role_rule = label, 2
        if structural:
            v.warnings.append("label_parameter_conflict")
    elif v.gradients is not None or "DIFFUSION" in v.image_type:
        v.role, v.role_rule = "DWI", 3
    elif structural:
        v.role, v.role_rule = family_role(structural), 4
        v.t1_family = v.role == "T1"
        label_role = "T1" if label == "T1_POST" else label
        if label in STRUCTURAL_LABELS and label_role != v.role:
            v.warnings.append("label_parameter_conflict")
    elif label in STRUCTURAL_LABELS:
        v.role, v.role_rule = label, 5
    if v.role in (None, "LOCALIZER"):
        v.family = v.role or ""
    elif v.role == "DWI":
        v.family = dwi_family(v.gradients, min_tensor_directions) if v.role_rule == 3 else "UNCLASSIFIED"
    elif v.role in ("T1", "T1_POST", "T2", "FLAIR"):
        v.family = structural or "UNCLASSIFIED"
    else:
        v.family = generic_family(v.role, mr_acq)
    derived = (bool(v.image_type) and v.image_type[0] == "DERIVED") or bool(DERIVED_TOKENS & set(v.image_type))
    if v.role in DERIVED_ROLES:
        if not derived and v.image_type and v.image_type[0] == "ORIGINAL":
            v.warnings.append("derived_by_role")
        derived = True
    if derived:
        tokens = set(v.image_type)
        v.kind = ("reformat" if tokens & REFORMAT_TOKENS else
                  "projection" if tokens & PROJECTION_TOKENS or v.role == "SWI_MINIP" else "map")


def _identity_match(a, b):
    """True when two native volumes of different series share one acquisition's identity (5.5, rule 2)."""
    if a.s("for_hash") is None or a.s("for_hash") != b.s("for_hash"):
        return False
    ta, tb = a.s("acq_rel_s"), b.s("acq_rel_s")
    if ta is None or tb is None or abs(ta - tb) > 1:
        return False
    if a.s("orientation") is None or a.s("orientation") != b.s("orientation"):
        return False
    if (a.s("rows"), a.s("cols"), a.s("n_positions")) != (b.s("rows"), b.s("cols"), b.s("n_positions")):
        return False
    for key in ("pos_min", "pos_max"):
        if a.s(key) is None or b.s(key) is None or abs(a.s(key) - b.s(key)) > 0.1:
            return False
    return all(close(a.s(key), b.s(key)) for key in ("tr_ms", "ti_ms", "flip_deg"))


def build_acquisitions(volumes):
    """Group native volumes into acquisitions and record duplicates (5.5)."""
    natives = [v for v in volumes if v.kind == "native" and v.role and v.role not in KEPT_ROLES]
    parent = {id(v): id(v) for v in natives}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    duplicates = []
    for i, a in enumerate(natives):
        for b in natives[i + 1:]:
            if a.series is None or b.series is None or a.mixed or b.mixed:
                continue
            same_series = a.series["series_key"] == b.series["series_key"]
            if not same_series and not _identity_match(a, b):
                continue
            if a.part != b.part or not close(a.te, b.te):
                parent[find(id(a))] = find(id(b))
            elif same_series:
                for v in (a, b):
                    if "multiple_outputs_same_series" not in v.issues:
                        v.issues.append("multiple_outputs_same_series")
            else:
                duplicates.append((a, b))
    groups = {}
    for v in natives:
        groups.setdefault(find(id(v)), []).append(v)
    acquisitions = []
    for members in groups.values():
        members.sort(key=lambda v: v.relpath)
        mags = [v for v in members if v.part != "phase"] or members
        roles = Counter(v.role for v in mags)
        role = sorted(roles, key=lambda r: (-roles[r], r))[0]
        issues = sorted({issue for v in members for issue in v.issues})
        if len(roles) > 1:
            issues.append("role_conflict")
        # The first echo whose parameters name a family: for a dual-echo PD/T2 series, the T2 echo.
        primary = sorted(mags, key=lambda v: (v.family == "UNCLASSIFIED", v.te is None, v.te or 0, v.relpath))[0]
        acq = Acquisition(members=members, id=short_hash("\n".join(sorted(v.vid for v in members))), role=role,
                          primary=primary, n_echoes=len({round(v.te, 1) for v in mags if v.te is not None}) or 1,
                          has_phase=any(v.part == "phase" for v in members),
                          phase_only=all(v.part == "phase" for v in members), issues=issues)
        for v in members:
            v.acq = acq
        acquisitions.append(acq)
    for a, b in duplicates:
        a.acq.duplicate_of.add(b.acq.id)
        b.acq.duplicate_of.add(a.acq.id)
    return sorted(acquisitions, key=lambda acq: acq.id)


def build_groups(volumes):
    """Derived volumes of one series form a group (5.6)."""
    found = {}
    for v in volumes:
        if v.kind != "native" and v.role and v.role not in KEPT_ROLES:
            key = v.series["series_key"] if v.series else v.vid
            found.setdefault(key, []).append(v)
    groups = []
    for members in found.values():
        members.sort(key=lambda v: v.relpath)
        first = members[0]
        group = Group(members=members, id=short_hash("\n".join(sorted(v.vid for v in members))), role=first.role,
                      kind=first.kind, series=None if first.mixed else first.series)
        for v in members:
            v.group = group
        groups.append(group)
    return sorted(groups, key=lambda g: g.id)


def _rung_filter(rung, s, candidates):
    """Candidates kept by one ladder rung, or None when the derived series lacks the field (skip)."""
    def p(acq, key):
        return acq.primary.s(key)

    if rung == "reference":
        refs = set(s.get("ref_series_keys") or ())
        if not refs:
            return None
        return [a for a in candidates if any(m.series and m.series["series_key"] in refs for m in a.members)]
    if rung == "frame_of_reference":
        if not s.get("for_hash"):
            return None
        return [a for a in candidates if p(a, "for_hash") == s["for_hash"]]
    if rung == "geometry":
        if s.get("orientation") is None or s.get("pos_min") is None:
            return None
        return [a for a in candidates if p(a, "orientation") == s["orientation"]
                and (p(a, "rows"), p(a, "cols"), p(a, "n_positions")) == (s.get("rows"), s.get("cols"), s.get("n_positions"))
                and p(a, "pos_min") is not None and abs(p(a, "pos_min") - s["pos_min"]) <= 0.5
                and abs(p(a, "pos_max") - s["pos_max"]) <= 0.5]
    if rung == "z_range":
        if s.get("orientation") is None or s.get("pos_min") is None:
            return None
        return [a for a in candidates if p(a, "orientation") == s["orientation"] and p(a, "pos_min") is not None
                and s["pos_min"] >= p(a, "pos_min") - 1 and s["pos_max"] <= p(a, "pos_max") + 1]
    if rung == "parameters":
        fields = [k for k in ("tr_ms", "ti_ms", "flip_deg", "te_ms") if s.get(k) is not None]
        if not fields:
            return None

        def matches(a):
            for key in fields:
                if key == "te_ms":
                    tes = [m.te for m in a.members if m.te is not None] or [p(a, "te_ms")]
                    if any(t is not None for t in tes) and not any(t is not None and close(t, s[key]) for t in tes):
                        return False
                elif p(a, key) is not None and not close(p(a, key), s[key]):
                    return False
            return True
        return [a for a in candidates if matches(a)]
    if rung == "acquisition_time":
        if s.get("acq_rel_s") is None:
            return None
        return [a for a in candidates if p(a, "acq_rel_s") is not None and abs(p(a, "acq_rel_s") - s["acq_rel_s"]) <= 1]
    if rung == "series_order":
        if s.get("series_number") is None:
            return None
        return [a for a in candidates if p(a, "series_number") is not None and p(a, "series_number") < s["series_number"]]
    raise ValueError(rung)


def link_group(group, acquisitions):
    """Run the linking ladder for one derived group (5.6)."""
    roles = PARENT_ROLES.get(group.role, set())
    candidates = [a for a in acquisitions if a.role in roles and not a.primary.mixed]
    if not candidates or group.series is None:
        group.link_state, group.candidates = "no_parent" if not candidates else "unlinked", [a.id for a in candidates]
        return
    for rung in RUNGS[group.kind]:
        kept = _rung_filter(rung, group.series, candidates)
        if kept is None:
            continue
        if not kept:
            group.link_state, group.link_rung, group.candidates = "unlinked", rung, [a.id for a in candidates]
            return
        candidates = kept
        if len(candidates) == 1:
            group.link_state, group.link_rung, group.parent = "linked", rung, candidates[0]
            group.candidates = [candidates[0].id]
            return
    group.link_state, group.candidates = "ambiguous", sorted(a.id for a in candidates)


def series_phases(series_records):
    """Contrast rules 1-3 for every series (5.7): {series_key: (phase, evidence)}."""
    records = [s for s in series_records if not s.get("mixed")]
    exam_wide = bool(records) and all(s.get("contrast_agent") for s in records)
    phases = {}
    for s in records:
        if s.get("bolus_rel_s") is not None and s.get("acq_rel_s") is not None:
            phases[s["series_key"]] = ("post" if s["bolus_rel_s"] < s["acq_rel_s"] else "pre", "bolus_time")
        elif s.get("contrast_agent"):
            phases[s["series_key"]] = ("unknown", "agent_tag_exam_wide") if exam_wide else ("post", "agent_tag")
        elif s.get("desc_phase"):
            phases[s["series_key"]] = (s["desc_phase"], "description")
    return phases


def assign_phases(acquisitions, groups, series_records, contrast_unknown):
    """Contrast phase of every acquisition and derived group (5.7, rules 1-7)."""
    first = series_phases(series_records)
    for acq in acquisitions:
        found = {first[m.series["series_key"]] for m in acq.members
                 if m.series and not m.mixed and m.series["series_key"] in first}
        phases = {p for p, _ in found}
        if len(phases) > 1:
            acq.phase, acq.evidence = "unknown", "members_disagree"
            acq.warnings.append("contrast_members_disagree")
        elif found:
            acq.phase, acq.evidence = sorted(found)[0]
        else:
            reformat = sorted({g.series.get("desc_phase") for g in groups if g.parent is acq and g.kind == "reformat"
                               and g.series and g.series.get("desc_phase")})
            if len(reformat) == 1:
                acq.phase, acq.evidence = reformat[0], "reformat_description"
    # Rule 5: timing against the earliest series with post evidence from rules 1-4.
    posts = [(s.get("acq_rel_s"), s.get("series_number")) for s in series_records
             if not s.get("mixed") and first.get(s["series_key"], ("",))[0] == "post"]
    posts += [(a.primary.s("acq_rel_s"), a.primary.s("series_number")) for a in acquisitions
              if a.phase == "post" and a.evidence == "reformat_description"]
    posts = [p for p in posts if p[0] is not None and p[1] is not None]
    anchor = min(posts) if posts else None

    def by_timing(t, number):
        if anchor is None or t is None or number is None:
            return None
        if t > anchor[0] and number > anchor[1]:
            return "post"
        if t < anchor[0] and number < anchor[1]:
            return "pre"
        return None

    def fallback(item, t, number):
        timed = by_timing(t, number)
        if timed:
            item.phase, item.evidence = timed, "timing"
        elif not posts and not any(p == "post" for p, _ in first.values()) and contrast_unknown == "presume_pre":
            item.phase, item.evidence = "pre", "presumed"
        else:
            item.phase, item.evidence = "unknown", "none"

    for acq in acquisitions:
        if acq.evidence == "none" and acq.phase == "unknown":
            fallback(acq, acq.primary.s("acq_rel_s"), acq.primary.s("series_number"))
    for group in groups:
        if group.parent is not None:
            group.phase, group.evidence = group.parent.phase, group.parent.evidence
        elif group.series and group.series["series_key"] in first:
            group.phase, group.evidence = first[group.series["series_key"]]
        else:
            fallback(group, (group.series or {}).get("acq_rel_s"), (group.series or {}).get("series_number"))


def split_t1(acquisitions, groups):
    """T1-family acquisitions with a post phase become T1_POST; linked groups take their parent's role."""
    for acq in acquisitions:
        if acq.role == "T1" and acq.phase == "post" and all(m.t1_family for m in acq.members):
            acq.role = "T1_POST"
            for m in acq.members:
                m.role = "T1_POST"
    for group in groups:
        if group.role in ("T1", "T1_POST"):
            role = group.parent.role if group.parent is not None and group.parent.role in ("T1", "T1_POST") else (
                "T1_POST" if group.phase == "post" and all(m.t1_family for m in group.members) else group.role)
            group.role = role
            for m in group.members:
                m.role = role


@dataclass
class Session:
    volumes: list
    acquisitions: list
    groups: list


def analyze(series, volumes, min_tensor_directions=6, contrast_unknown="presume_pre"):
    """Identity of one session's volumes: roles, acquisitions, links and phases (section 5).

    series: {series_key: record}; volumes: list of Volume (series filled in by the caller).
    """
    for v in volumes:
        setup_volume(v, min_tensor_directions)
    acquisitions = build_acquisitions(volumes)
    groups = build_groups(volumes)
    for group in groups:
        link_group(group, acquisitions)
    assign_phases(acquisitions, groups, list(series.values()), contrast_unknown)
    split_t1(acquisitions, groups)
    return Session(volumes=sorted(volumes, key=lambda v: v.relpath), acquisitions=acquisitions, groups=groups)
