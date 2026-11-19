"""Scan selection, part 2: profiles, gates, ranking and the fate of every volume (design: section 6).

Pure functions over a selection_identity.Session. Nothing here reads files.
"""

from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
import re

from .selection_identity import KEPT_ROLES, close, native_sampling, native_text

RULES_REVISION = 2  # 2: rotated 3D slice counts, vendor sequence conventions (2026-11-19)
RANKED = ("T1", "T1_POST", "T2", "FLAIR", "DWI", "SWI", "QSM", "GRE")
FOLLOWING = {"ADC": "DWI", "EADC": "DWI", "SWI_MINIP": "SWI"}
REPORT_ROLES = ("LOCALIZER", "T1", "T1_POST", "T2", "FLAIR", "DWI", "ADC", "EADC", "SWI", "SWI_MINIP", "QSM", "GRE",
                "MRA", "MRV", "PERFUSION")
PROFILE_ROLES = set(REPORT_ROLES)
FAMILIES = {"T1": {"T1_3D_IRGRE", "T1_3D_GRE", "T1_2D_IR", "T1_2D_SE"},
            "T1_POST": {"T1_3D_IRGRE", "T1_3D_GRE", "T1_2D_IR", "T1_2D_SE"},
            "FLAIR": {"FLAIR_3D", "FLAIR_2D"}, "T2": {"T2_3D_TSE", "T2_2D_TSE"}, "DWI": {"DWI_TENSOR", "DWI_TRACE"},
            "SWI": {"SWI_3D", "SWI_2D"}, "QSM": {"QSM_3D", "QSM_2D"}, "GRE": {"GRE_3D", "GRE_2D"}}
TWO_D_FAMILIES = {"T1_2D_IR", "T1_2D_SE", "FLAIR_2D", "T2_2D_TSE", "SWI_2D", "QSM_2D", "GRE_2D"}
THREE_D_ROLES = {"T1", "T1_POST", "T2", "FLAIR", "SWI", "QSM", "GRE"}
RANKING = {"T1": ("tier", "orientation", "native", "gap", "variant"),
           "T1_POST": ("tier", "orientation", "native", "gap", "variant"),
           "T2": ("tier", "contrast", "orientation", "native", "gap", "variant"),
           "FLAIR": ("tier", "contrast", "orientation", "native", "gap", "variant"),
           "DWI": ("tier", "directions", "b_target", "native", "gap", "variant"),
           "SWI": ("has_phase", "tier", "echoes", "contrast", "native", "gap", "variant"),
           "QSM": ("has_phase", "tier", "echoes", "contrast", "native", "gap"),
           "GRE": ("tier", "contrast", "native", "gap", "variant")}
# Reasons that mean "evidence missing": review instead of no_eligible_candidate (6.2).
EVIDENCE_REASONS = {"metadata_unavailable", "mixed_series", "role_conflict", "multiple_outputs_same_series",
                    "phase_only", "no_gradient_table", "gradient_mismatch", "native_unknown"}
# Families whose own evidence reason replaces the tier gates (6.2).
UNTIERED = {"UNCLASSIFIED", "DWI_UNKNOWN", "DWI_B0_ONLY"}
DISTORTION_TOKENS = ("DIS3D", "DIS2D", "ND")

AXES = ["AX", "SAG", "COR"]
_STRUCTURAL = {"contrast": "pre", "min_extent_mm": 110, "native_cap_mm": 1.1}
DEFAULT_PROFILE = {
    "name": "multimodal", "version": 1, "allow_accepted_partial": True, "contrast_unknown": "presume_pre",
    "repeat_policy": "review", "repeat_recommendation": "later", "modalities": None, "extras": False,
    "complete_only": False, "field_strength": None, "manufacturer": None,
    "roles": {
        "LOCALIZER": {"reduce": False}, "MRA": {"reduce": False}, "MRV": {"reduce": False},
        "PERFUSION": {"reduce": False}, "ADC": {}, "EADC": {}, "SWI_MINIP": {},
        "T1": dict(_STRUCTURAL, tiers=[
            {"name": "3d_ir_gre", "families": ["T1_3D_IRGRE"], "max_in_plane_mm": 1.3, "max_slice_mm": 1.3},
            {"name": "3d_gre", "families": ["T1_3D_GRE"], "max_in_plane_mm": 1.3, "max_slice_mm": 1.3, "fallback": True},
            {"name": "2d", "families": ["T1_2D_SE", "T1_2D_IR"], "max_in_plane_mm": 1.3, "max_slice_mm": 6.0,
             "orientation": AXES, "fallback": True},
            {"name": "any", "families": ["T1_3D_IRGRE", "T1_3D_GRE", "T1_2D_SE", "T1_2D_IR"], "orientation": AXES,
             "fallback": True}]),
        "T1_POST": dict(_STRUCTURAL, contrast="post", tiers=[
            {"name": "3d", "families": ["T1_3D_IRGRE", "T1_3D_GRE"], "max_in_plane_mm": 1.3, "max_slice_mm": 1.3},
            {"name": "2d", "families": ["T1_2D_SE", "T1_2D_IR"], "max_in_plane_mm": 1.3, "max_slice_mm": 6.0,
             "orientation": AXES, "fallback": True},
            {"name": "any", "families": ["T1_3D_IRGRE", "T1_3D_GRE", "T1_2D_SE", "T1_2D_IR"], "orientation": AXES,
             "fallback": True}]),
        "FLAIR": dict(_STRUCTURAL, contrast="prefer_pre", tiers=[
            {"name": "3d", "families": ["FLAIR_3D"], "max_in_plane_mm": 1.2, "max_slice_mm": 1.5},
            {"name": "2d_consensus", "families": ["FLAIR_2D"], "max_in_plane_mm": 1.2, "max_slice_mm": 3.0,
             "max_gap_mm": 0.1, "orientation": AXES, "fallback": True},
            {"name": "2d_legacy", "families": ["FLAIR_2D"], "max_in_plane_mm": 1.3, "max_slice_mm": 6.0,
             "max_gap_mm": 1.5, "orientation": AXES, "fallback": True},
            {"name": "any", "families": ["FLAIR_3D", "FLAIR_2D"], "orientation": AXES, "fallback": True}]),
        "T2": dict(_STRUCTURAL, contrast="prefer_pre", tiers=[
            {"name": "3d", "families": ["T2_3D_TSE"], "max_in_plane_mm": 1.2, "max_slice_mm": 1.5},
            {"name": "2d_consensus", "families": ["T2_2D_TSE"], "max_in_plane_mm": 1.2, "max_slice_mm": 3.0,
             "max_gap_mm": 0.1, "orientation": AXES},
            {"name": "2d_clinical", "families": ["T2_2D_TSE"], "max_in_plane_mm": 1.2, "max_slice_mm": 5.0,
             "max_gap_mm": 1.0, "orientation": AXES, "fallback": True},
            {"name": "any", "families": ["T2_3D_TSE", "T2_2D_TSE"], "orientation": AXES, "fallback": True}]),
        "DWI": {"contrast": "any", "native_cap_mm": 2.0, "min_tensor_directions": 6, "direction_cap": 30,
                "b_target": [800, 1200], "tiers": [
                    {"name": "tensor", "families": ["DWI_TENSOR"]}, {"name": "trace", "families": ["DWI_TRACE"]}]},
        "SWI": {"contrast": "prefer_pre", "native_cap_mm": 1.0, "echo_cap": 3, "tiers": [
            {"name": "3d", "families": ["SWI_3D"]}, {"name": "2d", "families": ["SWI_2D"], "fallback": True}]},
        "QSM": {"contrast": "prefer_pre", "native_cap_mm": 1.0, "echo_cap": 3, "tiers": [
            {"name": "3d", "families": ["QSM_3D"]}, {"name": "2d", "families": ["QSM_2D"], "fallback": True}]},
        "GRE": {"contrast": "prefer_pre", "native_cap_mm": 1.0, "tiers": [
            {"name": "thin", "families": ["GRE_3D", "GRE_2D"], "max_slice_mm": 3.0, "max_gap_mm": 1.0},
            {"name": "clinical", "families": ["GRE_3D", "GRE_2D"], "max_slice_mm": 5.0, "max_gap_mm": 1.0,
             "fallback": True},
            {"name": "any", "families": ["GRE_3D", "GRE_2D"], "fallback": True}]},
    }}
TOP_KEYS = {"name", "version", "allow_accepted_partial", "contrast_unknown", "repeat_policy",
            "repeat_recommendation", "modalities", "extras", "complete_only", "field_strength", "manufacturer", "roles"}
ROLE_KEYS = {"reduce", "contrast", "min_extent_mm", "native_cap_mm", "variant_preference", "prefer_normalized",
             "tiers", "min_tensor_directions", "direction_cap", "b_target", "echo_cap", "require"}
# Requirements a candidate must meet (6.2): each command-line option sets one for the roles it applies to.
REQUIRE_SCOPE = {"mr_acq": THREE_D_ROLES, "isotropic": set(RANKED), "max_voxel_mm": set(RANKED),
                 "no_fallback": set(RANKED), "min_directions": {"DWI"}}
ISOTROPIC_RATIO = 1.2  # largest native voxel side / smallest
FIELD_TOLERANCE_T = 0.25  # a header value of 2.89 T counts as 3 T
TIER_KEYS = {"name", "families", "max_in_plane_mm", "max_slice_mm", "max_gap_mm", "orientation", "fallback", "enabled"}


def _number(value, positive=True):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and (value > 0 if positive else value >= 0)


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def load_profile(data=None, options=None):
    """The built-in profile, a profile file merged in (key by key, role by role), then command-line options.

    options: {"modalities", "extras", "complete_only", "field_strength", "manufacturer": run-wide values;
    "require": {key: value}, applied to every role in REQUIRE_SCOPE[key]}. None or False leaves a value as is.
    """
    profile = copy.deepcopy(DEFAULT_PROFILE)
    if data is not None:
        if not isinstance(data, dict):
            raise ValueError("Profile must be a JSON object")
        unknown = set(data) - TOP_KEYS
        if unknown:
            raise ValueError(f"Unknown profile key(s): {', '.join(sorted(unknown))}")
        for key, value in data.items():
            if key != "roles":
                profile[key] = value
                continue
            if not isinstance(value, dict):
                raise ValueError("Profile 'roles' must be an object")
            for role, settings in value.items():
                if role not in PROFILE_ROLES:
                    raise ValueError(f"Unknown profile role {role!r}")
                if not isinstance(settings, dict):
                    raise ValueError(f"Profile role {role} must be an object")
                bad = set(settings) - ROLE_KEYS
                if bad:
                    raise ValueError(f"Unknown key(s) for profile role {role}: {', '.join(sorted(bad))}")
                profile["roles"][role].update(settings)
    options = options or {}
    for key in ("modalities", "extras", "complete_only", "field_strength", "manufacturer"):
        if options.get(key) not in (None, False):
            profile[key] = options[key]
    for key, value in (options.get("require") or {}).items():
        if value in (None, False):
            continue
        for role in REQUIRE_SCOPE[key]:
            profile["roles"][role]["require"] = dict(profile["roles"][role].get("require") or {}, **{key: value})
    if isinstance(profile.get("modalities"), list):
        profile["modalities"] = sorted({str(m).upper() for m in profile["modalities"]})
    if isinstance(profile.get("manufacturer"), list):
        profile["manufacturer"] = sorted({str(m).upper() for m in profile["manufacturer"]})
    if isinstance(profile.get("field_strength"), list):
        profile["field_strength"] = sorted(set(profile["field_strength"]))
    validate_profile(profile)
    return profile


def validate_profile(p):
    def fail(message):
        raise ValueError("Invalid profile: " + message)

    if not isinstance(p.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", p["name"]):
        fail("name must match [A-Za-z0-9_-]{1,64}")
    if not _integer(p.get("version")):
        fail("version must be a positive integer")
    for key in ("allow_accepted_partial", "extras", "complete_only"):
        if not isinstance(p.get(key), bool):
            fail(f"{key} must be true or false")
    modalities = p.get("modalities")
    if modalities is not None and (not isinstance(modalities, list) or not modalities
                                   or not set(modalities) <= set(REPORT_ROLES)):
        fail("modalities must list folder names from: " + ", ".join(REPORT_ROLES))
    if p["complete_only"] and modalities is None:
        fail("complete_only needs a list of modalities")
    field = p.get("field_strength")
    if field is not None and (not isinstance(field, list) or not field or not all(_number(x) for x in field)):
        fail("field_strength must list field strengths in tesla, for example [3]")
    makers = p.get("manufacturer")
    if makers is not None and (not isinstance(makers, list) or not makers or not all(
            isinstance(x, str) and x.strip() for x in makers)):
        fail("manufacturer must list manufacturer names, for example [\"GE\"]")
    if p.get("contrast_unknown") not in ("presume_pre", "review"):
        fail("contrast_unknown must be presume_pre or review")
    if p.get("repeat_policy") not in ("review", "later", "earlier"):
        fail("repeat_policy must be review, later or earlier")
    if p.get("repeat_recommendation") not in ("later", "earlier"):
        fail("repeat_recommendation must be later or earlier")
    for role, rp in p["roles"].items():
        reduce = rp.get("reduce", True)
        if not isinstance(reduce, bool):
            fail(f"{role}.reduce must be true or false")
        if role in KEPT_ROLES and reduce:
            fail(f"{role} has no selection rules yet; it must keep every volume (reduce: false)")
        if role in FOLLOWING:
            if set(rp) - {"reduce"} or not reduce:
                fail(f"{role} follows {FOLLOWING[role]} and takes no settings")
            continue
        if role not in RANKED or not reduce:
            if rp.get("require"):
                fail(f"{role} takes no requirements")
            continue
        require = rp.get("require") or {}
        if not isinstance(require, dict) or set(require) - set(REQUIRE_SCOPE):
            fail(f"{role}.require takes only: {', '.join(sorted(REQUIRE_SCOPE))}")
        if require.get("mr_acq") not in (None, "3D"):
            fail(f"{role}.require.mr_acq must be \"3D\" or null")
        for key in ("isotropic", "no_fallback"):
            if not isinstance(require.get(key, False), bool):
                fail(f"{role}.require.{key} must be true or false")
        if require.get("max_voxel_mm") is not None and not _number(require["max_voxel_mm"]):
            fail(f"{role}.require.max_voxel_mm must be a positive number or null")
        if require.get("min_directions") is not None and (role != "DWI" or not _integer(require["min_directions"])):
            fail("require.min_directions applies to DWI and must be a positive integer")
        if rp.get("contrast") not in ("pre", "post", "prefer_pre", "any"):
            fail(f"{role}.contrast must be pre, post, prefer_pre or any")
        if rp.get("min_extent_mm") is not None and not _number(rp["min_extent_mm"], positive=False):
            fail(f"{role}.min_extent_mm must be a number or null")
        if not _number(rp.get("native_cap_mm")):
            fail(f"{role}.native_cap_mm must be a positive number")
        if not isinstance(rp.get("variant_preference", []), list) or not all(
                isinstance(x, str) for x in rp.get("variant_preference", [])):
            fail(f"{role}.variant_preference must be a list of strings")
        if rp.get("prefer_normalized") not in (None, True, False):
            fail(f"{role}.prefer_normalized must be true, false or null")
        for key in ("min_tensor_directions", "direction_cap", "b_target"):
            if key in rp and role != "DWI":
                fail(f"{key} applies to DWI only")
        if role == "DWI":
            if not _integer(rp.get("min_tensor_directions")) or not _integer(rp.get("direction_cap")):
                fail("DWI.min_tensor_directions and direction_cap must be positive integers")
            b = rp.get("b_target")
            if not (isinstance(b, list) and len(b) == 2 and all(_number(x) for x in b) and b[0] < b[1]):
                fail("DWI.b_target must be [low, high]")
        if "echo_cap" in rp and (role not in ("SWI", "QSM") or not _integer(rp["echo_cap"])):
            fail("echo_cap applies to SWI and QSM and must be a positive integer")
        tiers = rp.get("tiers")
        if not isinstance(tiers, list) or not tiers:
            fail(f"{role}.tiers must be a nonempty list")
        for tier in tiers:
            if not isinstance(tier, dict) or set(tier) - TIER_KEYS:
                fail(f"{role} tier has unknown keys")
            if not isinstance(tier.get("name"), str) or not tier["name"]:
                fail(f"{role} tier needs a name")
            families = tier.get("families")
            if not isinstance(families, list) or not families or not set(families) <= FAMILIES[role]:
                fail(f"{role} tier {tier['name']!r}: families must be a nonempty subset of "
                     f"{', '.join(sorted(FAMILIES[role]))}")
            for key in ("max_in_plane_mm", "max_slice_mm", "max_gap_mm"):
                if tier.get(key) is not None and not _number(tier[key], positive=key != "max_gap_mm"):
                    fail(f"{role} tier {tier['name']!r}: {key} must be a number or null")
            if not isinstance(tier.get("orientation", []), list) or not set(tier.get("orientation", [])) <= {
                    "AX", "SAG", "COR", "oblique"}:
                fail(f"{role} tier {tier['name']!r}: orientation must list AX, SAG, COR or oblique")
            for key in ("fallback", "enabled"):
                if not isinstance(tier.get(key, False), bool):
                    fail(f"{role} tier {tier['name']!r}: {key} must be true or false")


def profile_sha256(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def selection_role(role):
    return "SWI" if role == "SWI_PHASE" else role


def reduces(profile, role):
    return profile["roles"].get(role, {}).get("reduce", True) and role not in KEPT_ROLES


# ---- gates and tiers (6.2) ---------------------------------------------------------------------------------

def _tier_status(tier, sampling):
    limit = tier.get("max_in_plane_mm")
    if limit is not None:
        if not sampling["inplane"]:
            return "native_unknown"
        passes = [max(pair) <= limit + 1e-6 for pair in sampling["inplane"]]
        if not any(passes):
            return "too_coarse"
        if not all(passes):
            return "native_unknown"
    limit = tier.get("max_slice_mm")
    if limit is not None:
        if sampling["slice"] is None:
            return "native_unknown"
        if sampling["slice"] > limit + 1e-6:
            return "too_coarse"
    limit = tier.get("max_gap_mm")
    if limit is not None:
        if sampling["gap"] is None:
            return "native_unknown"
        if sampling["gap"] > limit + 1e-6:
            return "slice_gap"
    return "ok"


def assign_tier(family, tiers, sampling):
    """(index, tier) of the first enabled tier the candidate meets, else (None, reasons)."""
    statuses = []
    for i, tier in enumerate(tiers):
        if not tier.get("enabled", True) or family not in tier["families"]:
            continue
        status = _tier_status(tier, sampling)
        if status == "ok":
            return (i, tier), []
        statuses.append(status)
    if not statuses:
        return None, ["family_not_enabled"]
    return None, [r for r in ("too_coarse", "slice_gap", "native_unknown") if r in statuses]


def _dims(v):
    d = list(v.dims or [])
    n = d[0] if d else 0
    return [d[i] if i <= n and i < len(d) else 1 for i in range(1, 4)]


def slices_match(v):
    """True when the NIfTI grid holds the series' rows, columns and slice positions.

    dcm2niix rotates 3D acquisitions to the nearest orthogonal orientation (its -x default), so the
    slices of a sagittal or coronal 3D scan can lie along any NIfTI axis.
    """
    rows, cols = v.s("rows"), v.s("cols")
    if rows and cols:
        return sorted(_dims(v)) == sorted([rows, cols, v.s("n_positions")])
    return v.s("n_positions") in _dims(v)


def gate(acq, role, rp, profile, excluded):
    """(reasons, tier) for one candidate acquisition in a ranked role (6.2)."""
    p = acq.primary
    reasons = []
    if acq.id in excluded:
        reasons.append("excluded_by_reviewer")
    if "mixed_series" in acq.issues:
        return reasons + ["mixed_series"], None
    family = acq.family
    if "metadata_unavailable" in acq.issues or family == "UNCLASSIFIED" or (
            family.endswith("_UNKNOWN") and family != "DWI_UNKNOWN"):
        reasons.append("metadata_unavailable")
    reasons += [r for r in ("role_conflict", "multiple_outputs_same_series") if r in acq.issues]
    if acq.phase_only:
        reasons.append("phase_only")
    if family == "DWI_UNKNOWN":
        reasons.append("no_gradient_table")
    if p.gradients and p.gradients.get("mismatch"):
        reasons.append("gradient_mismatch")
    if family == "DWI_B0_ONLY":
        reasons.append("b0_only")
    if p.dims and min(_dims(p)) < 10:
        reasons.append("not_a_volume")
    elif p.s("n_positions") and not p.s("mosaic") and p.dims and not slices_match(p):
        reasons.append("slice_count_mismatch")
    if any(any(c["kind"] == "eq" for c in m.companions) for m in acq.members) or p.s("uniform_spacing") is False:
        reasons.append("uneven_spacing")
    contrast = rp.get("contrast")
    if contrast == "pre" and acq.phase == "post":
        reasons.append("postcontrast")
    if contrast == "post" and acq.phase == "pre":
        reasons.append("precontrast")
    extent = p.s("extent_mm")
    if rp.get("min_extent_mm") is not None and extent is not None and extent < rp["min_extent_mm"]:
        reasons.append("partial_coverage")
    require = rp.get("require") or {}
    sampling = native_sampling(p.series or {})
    if require.get("mr_acq") == "3D" and role in THREE_D_ROLES and family in TWO_D_FAMILIES:
        reasons.append("not_3d")
    if require.get("isotropic"):
        reasons.append(_requirement(sampling, lambda sides: max(sides) <= ISOTROPIC_RATIO * min(sides) + 1e-6,
                                    "not_isotropic"))
    if require.get("max_voxel_mm") is not None:
        reasons.append(_requirement(sampling, lambda sides: max(sides) <= require["max_voxel_mm"] + 1e-6,
                                    "voxel_too_large"))
    if require.get("min_directions") and p.gradients and p.gradients["n_dirs"] < require["min_directions"]:
        reasons.append("too_few_directions")
    tier = None
    if family not in UNTIERED and not family.endswith("_UNKNOWN") and not acq.phase_only:
        tier, tier_reasons = assign_tier(family, rp["tiers"], sampling)
        reasons += tier_reasons
        if require.get("no_fallback") and tier is not None and tier[1].get("fallback"):
            reasons.append("fallback_only")
    return list(dict.fromkeys(r for r in reasons if r)), tier


def _requirement(sampling, test, reason):
    """'' when the native voxel passes, the reason when it fails, native_unknown when it cannot be checked."""
    if not sampling["inplane"] or not sampling["slice"]:
        return "native_unknown"
    results = {test((row, col, sampling["slice"])) for row, col in sampling["inplane"]}
    if len(results) > 1:
        return "native_unknown"
    return "" if results.pop() else reason


def normalized_field(value):
    """Field strength in tesla; old headers give gauss (15000 for 1.5 T)."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return round(value / 10000 if value > 100 else value, 3)


def session_requirement(series_records, profile):
    """(outcome, reason) when the session's scanner fails the field-strength or manufacturer requirement, else None.

    One session is one scanner, so these requirements decide the whole session (owner request, 2026-11-18).
    A value the headers do not give goes to review, never to a guess.
    """
    if profile.get("field_strength"):
        values = Counter(f for f in (normalized_field(s.get("field_t")) for s in series_records) if f)
        if not values:
            return "review_required", "field_strength_unknown"
        field = sorted(values, key=lambda f: (-values[f], f))[0]
        if not any(abs(field - wanted) <= FIELD_TOLERANCE_T for wanted in profile["field_strength"]):
            return "session_excluded", "field_strength"
    if profile.get("manufacturer"):
        names = Counter(s.get("manufacturer") for s in series_records if s.get("manufacturer"))
        if not names:
            return "review_required", "manufacturer_unknown"
        if sorted(names, key=lambda n: (-names[n], n))[0] not in profile["manufacturer"]:
            return "session_excluded", "manufacturer"
    return None


def listed(role, profile):
    """True when a role's folder is part of the output (the modalities option; None lists every folder)."""
    return profile.get("modalities") is None or selection_role(role) in profile["modalities"]


def incomplete_roles(decisions, profile):
    """Listed modalities without a selection, for complete_only (owner request, 2026-11-18)."""
    return [r for r in profile.get("modalities") or [] if decisions[r]["outcome"] not in
            ("selected", "selected_fallback", "kept_all")]


# ---- ranking (6.3) -----------------------------------------------------------------------------------------

def _variant(p, rp):
    tokens = set(p.image_type)
    preference = rp.get("variant_preference", list(DISTORTION_TOKENS))
    rank = next((i for i, t in enumerate(preference) if t in tokens), len(preference))
    normalized = rp.get("prefer_normalized")
    norm = 0 if normalized is None else (0 if ("NORM" in tokens) == normalized else 1)
    return (rank, norm)


def component(name, acq, tier, rp):
    """One ranking component; None when the value is unknown (6.3)."""
    p = acq.primary
    sampling = native_sampling(p.series or {}) if not p.mixed else native_sampling({})
    if name == "tier":
        return tier[0]
    if name == "has_phase":
        return 0 if acq.has_phase else 1
    if name == "directions":
        return -min(p.gradients["n_dirs"], rp["direction_cap"]) if p.gradients else None
    if name == "b_target":
        low, high = rp["b_target"]
        return 0 if p.gradients and any(low <= b <= high for b in p.gradients["shells"]) else 1
    if name == "echoes":
        return -min(acq.n_echoes, rp["echo_cap"])
    if name == "contrast":
        return 1 if rp.get("contrast") == "prefer_pre" and acq.phase == "post" else 0
    if name == "orientation":
        order = tier[1].get("orientation") or []
        if not order:
            return 0
        o = p.s("orientation")
        return None if o is None else order.index(o) if o in order else len(order)
    if name == "native":
        return None if sampling["max"] is None else round(max(sampling["max"], rp["native_cap_mm"]), 2)
    if name == "gap":
        if tier[1].get("max_gap_mm") is None:
            return 0
        return None if sampling["gap"] is None else round(round(max(sampling["gap"], 0) / 0.05) * 0.05, 2)
    if name == "variant":
        return _variant(p, rp)
    raise ValueError(name)


def _repeat_pair(a, b):
    pa, pb = a.primary, b.primary
    if (a.family, pa.s("orientation"), pa.s("mr_acq")) != (b.family, pb.s("orientation"), pb.s("mr_acq")):
        return False
    if not (close(pa.s("tr_ms"), pb.s("tr_ms")) and close(pa.te, pb.te) and close(pa.s("ti_ms"), pb.s("ti_ms"))
            and close(pa.s("flip_deg"), pb.s("flip_deg"))):
        return False
    na, nb = native_sampling(pa.series or {})["max"], native_sampling(pb.series or {})["max"]
    if not close(na, nb, rel=0.05):
        return False
    tokens = set(DISTORTION_TOKENS) | {"NORM"}
    return set(pa.image_type) & tokens == set(pb.image_type) & tokens


def _order_reliable(a, b):
    ta, tb = a.primary.s("acq_rel_s"), b.primary.s("acq_rel_s")
    na, nb = a.primary.s("series_number"), b.primary.s("series_number")
    if None in (ta, tb, na, nb) or abs(ta - tb) < 1 or na == nb:
        return False
    return (ta < tb) == (na < nb)


def _decision(role):
    return {"role": role, "outcome": "missing", "decided_by": "", "winner": None, "candidate_id": "",
            "rule_recommendation": "", "runner_up": "", "n_candidates": 0, "n_eligible": 0, "reasons": [],
            "warnings": [], "tier": None}


def _apply_overrides(d, ids, ov, chosen_lookup):
    """Overrides first (6.4 step 1): returns True when an override decided the outcome."""
    if not ov:
        return False
    stale = [i for i in ([ov["choose"]] if ov.get("choose") else []) + sorted(ov.get("exclude", ())) if i not in ids]
    if stale:
        d.update(outcome="review_required", reasons=["override_stale"], winner=None, decided_by="")
        return True
    if ov.get("none"):
        d.update(outcome="no_eligible_candidate", decided_by="override", winner=None, reasons=[])
        return True
    if ov.get("choose"):
        d.update(outcome="selected", decided_by="override", winner=chosen_lookup[ov["choose"]], reasons=[])
        d["warnings"].append("override_applied")
        return True
    if ov.get("exclude"):
        d["warnings"].append("override_applied")
    return False


def decide_ranked(role, candidates, rp, profile, ov, info, extra=()):
    """Decision for one ranked role (6.4). info collects per-candidate results.

    extra: derived groups an override may also choose (QSM maps).
    """
    d = _decision(role)
    d["n_candidates"] = len(candidates)
    excluded = set((ov or {}).get("exclude", ()))
    for acq in candidates:
        reasons, tier = gate(acq, role, rp, profile, excluded)
        info[acq.id] = {"reasons": reasons, "tier": tier, "eligible": not reasons, "rank": ""}
    eligible = [a for a in candidates if info[a.id]["eligible"]]
    d["n_eligible"] = len(eligible)
    winner, recommendation = None, None
    if not candidates:
        d["outcome"] = "missing"
    elif not eligible:
        evidence_only = any(info[a.id]["reasons"] and set(info[a.id]["reasons"]) <= EVIDENCE_REASONS
                            for a in candidates)
        counts = {}
        for a in candidates:
            counts[info[a.id]["reasons"][0]] = counts.get(info[a.id]["reasons"][0], 0) + 1
        d["outcome"] = "review_required" if evidence_only else "no_eligible_candidate"
        d["reasons"] = [f"{r}({n})" for r, n in sorted(counts.items())]
    else:
        names = RANKING[role]
        values = {a.id: [component(n, a, info[a.id]["tier"], rp) for n in names] for a in eligible}
        used = [i for i in range(len(names)) if all(values[a.id][i] is not None for a in eligible)]
        skipped = [names[i] for i in range(len(names)) if i not in used]
        key = {a.id: tuple(values[a.id][i] for i in used) for a in eligible}
        order = sorted(eligible, key=lambda a: (key[a.id], a.id))
        distinct = sorted(set(key.values()))
        for a in eligible:
            info[a.id]["rank"] = distinct.index(key[a.id]) + 1
        best = key[order[0].id]
        top = [a for a in order if key[a.id] == best]
        runner = next((a for a in order if key[a.id] != best), None)
        if runner is not None:
            d["runner_up"] = runner.id
        if len(top) == 1:
            winner = top[0]
            if runner is None:
                d["decided_by"] = "only_eligible"
            else:
                d["decided_by"] = next(names[used[i]] for i in range(len(used)) if best[i] != key[runner.id][i])
        elif all(b.id in a.duplicate_of for a in top for b in top if a is not b):
            recommendation = min(top, key=lambda a: (a.primary.s("series_number") or 0, a.id))
            d.update(outcome="review_required", reasons=["duplicate_series"])
        elif all(_repeat_pair(a, b) for i, a in enumerate(top) for b in top[i + 1:]):
            if not all(_order_reliable(a, b) for i, a in enumerate(top) for b in top[i + 1:]):
                d.update(outcome="review_required", reasons=["order_unreliable"])
            else:
                timed = sorted(top, key=lambda a: a.primary.s("acq_rel_s"))
                policy = profile["repeat_policy"]
                pick = timed[-1] if (policy if policy != "review" else profile["repeat_recommendation"]) == "later" \
                    else timed[0]
                if policy == "review":
                    recommendation = pick
                    d.update(outcome="review_required", reasons=["repeat_needs_review"])
                else:
                    winner = pick
                    d["decided_by"] = "repeat_order"
                    d["warnings"].append("repeat_order_tiebreak")
        else:
            d.update(outcome="review_required",
                     reasons=["undecidable_missing_field" if skipped else "unresolved_tie"])
        if winner is not None:
            checks = []
            if rp.get("contrast") in ("pre", "post") and winner.phase == "unknown":
                checks.append("contrast_unknown")
            if any("label_parameter_conflict" in m.warnings for m in winner.members):
                checks.append("label_parameter_conflict")
            if not winner.has_phase and any(a.phase_only for a in candidates):
                checks.append("orphan_phase")
            if checks:
                recommendation, winner = winner, None
                d.update(outcome="review_required", reasons=checks, decided_by="")
            else:
                d["tier"] = info[winner.id]["tier"]
                d["outcome"] = "selected_fallback" if d["tier"][1].get("fallback") else "selected"
    if recommendation is not None:
        d["rule_recommendation"] = recommendation.id
    elif winner is not None:
        d["rule_recommendation"] = winner.id
    d["winner"] = winner
    lookup = {a.id: a for a in candidates} | {g.id: g for g in extra}
    if _apply_overrides(d, set(lookup), ov, lookup) and d["winner"] is not None and d["winner"].id in info:
        d["tier"] = info[d["winner"].id]["tier"]
    if d["winner"] is not None:
        d["candidate_id"] = d["winner"].id
    return d


def _pe(v):
    value = str(v.sidecar.get("PhaseEncodingDirection") or "")
    return (value[0], value.endswith("-")) if value[:1] in ("i", "j", "k") else None


def dwi_partners(winner, candidates, info):
    """Reverse phase-encode b0 partners of the selected DWI source (6.4, step 7)."""
    found = set()
    if winner is None or _pe(winner.primary) is None:
        return found
    axis, negative = _pe(winner.primary)
    w = winner.primary
    for acq in candidates:
        p = acq.primary
        if acq is winner or info[acq.id]["reasons"] != ["b0_only"] or _pe(p) is None:
            continue
        if (p.s("orientation"), p.s("rows"), p.s("cols")) != (w.s("orientation"), w.s("rows"), w.s("cols")):
            continue
        if _pe(p) == (axis, not negative):
            found.add(acq.id)
    return found


def follow(d, groups, parents, ov, parent_review):
    """Outcome of a role whose groups follow selected parent acquisitions (6.5)."""
    d["n_candidates"] = len(groups)
    excluded = set((ov or {}).get("exclude", ()))
    live = [g for g in groups if g.id not in excluded]
    linked = [g for g in live if g.link_state == "linked" and g.parent in parents]
    d["n_eligible"] = len(linked)
    if not groups:
        d["outcome"] = "missing"
    elif parent_review:
        d.update(outcome="review_required", reasons=["parent_review"])
    elif len(linked) == 1:
        d.update(outcome="selected", decided_by="follows_source", winner=linked[0])
    elif len(linked) > 1:
        d.update(outcome="review_required", reasons=["multiple_derived_for_source"])
    elif any(g.link_state != "linked" for g in live):
        d.update(outcome="review_required", reasons=["derived_unlinked"])
    else:
        d.update(outcome="no_eligible_candidate", reasons=["source_not_selected"])
    if d["winner"] is not None:
        d["rule_recommendation"] = d["winner"].id
    lookup = {g.id: g for g in groups}
    _apply_overrides(d, set(lookup), ov, lookup)
    if d["winner"] is not None:
        d["candidate_id"] = d["winner"].id
    return d


def decide_session(session, profile, overrides=None, accepted_partial=False):
    """Every role's decision and every volume's fate for one analyzed session (6.4-6.6).

    overrides: {role: {"choose": id or None, "none": bool, "exclude": set of ids}}.
    Returns (decisions by role, volume rows by Volume, info by candidate id).
    """
    overrides = overrides or {}
    info, decisions, partners = {}, {}, set()
    by_role = {}
    for acq in session.acquisitions:
        by_role.setdefault(selection_role(acq.role), []).append(acq)
    groups_by_role = {}
    for g in session.groups:
        groups_by_role.setdefault(selection_role(g.role), []).append(g)
    for role in RANKED:
        rp = profile["roles"][role]
        if not reduces(profile, role):
            d = _decision(role)
            d["outcome"] = "kept_all" if by_role.get(role) or groups_by_role.get(role) else "missing"
            decisions[role] = d
            continue
        decisions[role] = decide_ranked(role, by_role.get(role, []), rp, profile, overrides.get(role), info,
                                        groups_by_role.get(role, []) if role == "QSM" else ())
    dwi = decisions["DWI"]
    if dwi["outcome"] in ("selected", "selected_fallback"):
        partners = dwi_partners(dwi["winner"], by_role.get("DWI", []), info)
    for role, parent_role in FOLLOWING.items():
        parent = decisions[parent_role]
        d = _decision(role)
        if parent["outcome"] == "kept_all":
            d["outcome"] = "kept_all" if groups_by_role.get(role) else "missing"
        else:
            winners = {parent["winner"]} if parent["outcome"] in ("selected", "selected_fallback") else set()
            follow(d, groups_by_role.get(role, []), winners, overrides.get(role),
                   parent["outcome"] == "review_required")
        decisions[role] = d
    qsm = decisions["QSM"]
    qsm_groups = groups_by_role.get("QSM", [])
    if reduces(profile, "QSM") and qsm["outcome"] in ("missing", "no_eligible_candidate") \
            and qsm["decided_by"] != "override" and qsm_groups:
        parents = {decisions[r]["winner"] for r in ("SWI", "GRE")
                   if decisions[r]["outcome"] in ("selected", "selected_fallback")}
        native = qsm["n_candidates"]
        fresh = follow(_decision("QSM"), qsm_groups, parents, None, False)
        if fresh["outcome"] != "no_eligible_candidate" or not native:
            fresh["n_candidates"] += native
            decisions["QSM"] = fresh
    for role in REPORT_ROLES:
        if role not in decisions:
            d = _decision(role)
            present = any(v.role == role for v in session.volumes)
            d["outcome"] = "kept_all" if present else "missing"
            decisions[role] = d
    # Derived groups that join a selected acquisition in a ranked role, or win a following role (6.5, 6.6).
    selected_groups = set()
    for role, d in decisions.items():
        winner = d["winner"]
        if winner is None:
            continue
        if role in FOLLOWING or (role == "QSM" and d["decided_by"] == "follows_source") or not hasattr(winner, "primary"):
            selected_groups.add(winner.id)
    for g in session.groups:
        r = selection_role(g.role)
        d = decisions.get(r)
        if r in RANKED and d and d["winner"] is not None and g.parent is d["winner"] and g.kind != "reformat":
            selected_groups.add(g.id)
    excluded_groups = {i for role in FOLLOWING for i in (overrides.get(role) or {}).get("exclude", ())}
    excluded_groups |= set((overrides.get("QSM") or {}).get("exclude", ()))
    for role, d in decisions.items():
        if accepted_partial:
            d["warnings"].append("accepted_partial_session")
        if d["winner"] is not None and not hasattr(d["winner"], "primary"):
            for m in d["winner"].members:
                d["warnings"] += m.warnings
        if d["winner"] is not None and hasattr(d["winner"], "primary"):
            w = d["winner"]
            if w.evidence == "presumed":
                d["warnings"].append("contrast_presumed_pre")
            if w.evidence == "timing":
                d["warnings"].append("contrast_by_timing")
            d["warnings"] += w.warnings
            for m in w.members:
                d["warnings"] += m.warnings
            sampling = native_sampling(w.primary.series or {})
            if sampling["interpolated"]:
                d["warnings"].append("interpolated_grid")
            if sampling["gap"] is not None and sampling["gap"] < -0.01:
                d["warnings"].append("overlapping_slices")
        if any(selection_role(g.role) == role and g.kind != "reformat" and g.link_state != "linked"
               for g in session.groups):
            d["warnings"].append("derived_unlinked")
        d["warnings"] = sorted(set(d["warnings"]))
    rows = {}
    for v in session.volumes:
        rows[v] = _fate(v, decisions, info, partners, selected_groups, excluded_groups)
    return decisions, rows, info


def _fate(v, decisions, info, partners, selected_groups, excluded_groups):
    if v.role is None:
        return {"fate": "unclassified", "folder": "OTHER/UNCLASSIFIED", "candidate_id": ""}
    if v.acq is not None:
        r = selection_role(v.acq.role)
        d = decisions[r]
        candidate = v.acq.id
        if d["outcome"] == "kept_all":
            fate = "kept"
        elif d["winner"] is v.acq:
            fate = "selected"
        elif v.acq.id in partners:
            fate = "companion"
        elif d["outcome"] == "review_required":
            fate = "pending_review"
        elif info.get(v.acq.id, {}).get("eligible"):
            fate = "alternative"
        else:
            fate = "excluded"
    elif v.group is not None:
        g = v.group
        r = selection_role(g.role)
        d = decisions[r]
        candidate = g.id
        if d["outcome"] == "kept_all":
            fate = "kept"
        elif g.kind == "reformat":
            fate = "reformat"
        elif g.id in selected_groups:
            fate = "selected_derived"
        elif d["outcome"] == "review_required":
            fate = "pending_review"
        elif g.id in excluded_groups:
            fate = "excluded"
        elif g.link_state != "linked":
            fate = "unlinked"
        else:
            fate = "alternative"
    else:
        return {"fate": "kept", "folder": v.role, "candidate_id": ""}
    if fate in ("selected", "selected_derived", "kept"):
        folder = v.role
    elif fate == "companion":
        folder = "DWI"
    else:
        folder = "OTHER/" + v.role
    return {"fate": fate, "folder": folder, "candidate_id": candidate}


def role_facts(d):
    """Winner facts for selection.csv (7.4)."""
    w = d["winner"]
    if w is None:
        return {}
    if hasattr(w, "primary"):
        p, members, phase, evidence = w.primary, w.members, w.phase, w.evidence
        echoes, has_phase, rung = w.n_echoes, w.has_phase, ""
    else:
        p, members, phase, evidence = w.members[0], w.members, w.phase, w.evidence
        echoes, has_phase, rung = "", "", w.link_rung
    sampling = native_sampling(p.series or {}) if not p.mixed else native_sampling({})
    numbers = sorted({m.sidecar.get("SeriesNumber") for m in members if m.sidecar.get("SeriesNumber") is not None})
    g = p.gradients or {}
    return {"source": p.relpath, "series_numbers": ";".join(map(str, numbers)), "family": p.family,
            "tier": d["tier"][1]["name"] if d["tier"] else "", "contrast_phase": phase, "contrast_evidence": evidence,
            "orientation": p.s("orientation") or "", "mr_acq": p.s("mr_acq") or "", "native_mm": native_text(sampling),
            "n_echoes": echoes, "has_phase": has_phase, "n_dirs": g.get("n_dirs", ""), "b_max": g.get("b_max") or "",
            "link_rung": rung, "manufacturer": p.s("manufacturer") or "", "model": p.s("model") or "",
            "field_t": p.s("field_t") or "", "software": p.s("software") or "",
            "native_max_mm": sampling["max"]}
