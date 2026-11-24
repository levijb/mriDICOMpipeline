"""Compare header facts under plan 3.4, independently of the header reader's own comparator.

Used by R4-OR-03 (reader against the catalog) so that a defect in the reader's comparator
cannot hide a defect in the reader.
"""

from __future__ import annotations

INT_FIELDS = {"series_number", "rows", "cols", "n_positions", "n_instances", "n_stacks", "etl"}
STR_FIELDS = {"manufacturer", "model", "mr_acq", "seq_name", "phase_dir", "orientation"}
SET_FIELDS = {"image_type", "scan_seq", "seq_var", "scan_opt", "software", "desc_tokens"}
TOL = {"tr_ms": 0.01, "te_ms": 0.01, "ti_ms": 0.01, "flip_deg": 0.01, "field_t": 0.001,
       "thick_mm": 0.001, "sbs_mm": 0.001, "step_mm": 0.001, "pos_min": 0.01, "pos_max": 0.01,
       "extent_mm": 0.01, "acq_rel_s": 0.01, "series_rel_s": 0.01, "bolus_rel_s": 0.01,
       "pct_phase_fov": 0.01}
BOOL_FIELDS = {"contrast_agent", "uniform_spacing", "multiframe", "mixed"}
MIXED_KEEP = {"series_number", "manufacturer", "model", "field_t", "n_instances", "n_stacks", "mixed"}
ALL_FIELDS = (INT_FIELDS | STR_FIELDS | SET_FIELDS | set(TOL) | BOOL_FIELDS
              | {"pixel_mm", "acq_matrix", "for_hash", "parts", "echoes"})


def equal(field, expected, actual):
    if field in SET_FIELDS:
        return sorted(set(expected or [])) == sorted(set(actual or []))
    if expected is None or actual is None:
        return expected is None and actual is None
    if field in INT_FIELDS:
        return int(expected) == int(actual)
    if field in STR_FIELDS:
        return str(expected) == str(actual)
    if field in TOL:
        return abs(float(expected) - float(actual)) <= TOL[field] + 1e-9
    if field in BOOL_FIELDS:
        return bool(expected) is bool(actual)
    if field == "pixel_mm":
        return len(expected) == len(actual) and all(abs(float(a) - float(b)) <= 0.0001 + 1e-9
                                                    for a, b in zip(expected, actual))
    if field == "acq_matrix":
        return [int(v) for v in expected] == [int(v) for v in actual]
    if field == "for_hash":
        return str(actual).startswith(str(expected)) or str(expected).startswith(str(actual))
    if field == "parts":
        return {k: int(v) for k, v in expected.items()} == {k: int(v) for k, v in actual.items()}
    if field == "echoes":
        if len(expected) != len(actual):
            return False
        for a, b in zip(expected, actual):
            if abs(float(a["te_ms"]) - float(b["te_ms"])) > 0.01 + 1e-9:
                return False
            if a["echo_number"] != b["echo_number"] or int(a["n_instances"]) != int(b["n_instances"]):
                return False
        return True
    raise KeyError(field)


def compare(expected_facts, record):
    """Mismatches between the catalog's expected facts and one reader record: a list of
    (field, expected, actual). The reader's for_sha256 stands for for_hash."""
    actual = dict(record)
    if "for_sha256" in actual:
        actual["for_hash"] = actual["for_sha256"]
    fields = MIXED_KEEP if expected_facts.get("mixed") else ALL_FIELDS
    out = []
    for field in sorted(fields):
        if field not in expected_facts:
            continue
        if field not in actual:
            out.append((field, expected_facts[field], "<absent>"))
            continue
        if not equal(field, expected_facts[field], actual[field]):
            out.append((field, expected_facts[field], actual[field]))
    exp_unclear = set(expected_facts.get("unclear", []))
    got_unclear = set(record.get("unclear", {})) & (fields | {"positions", "native", "echoes", "parts"})
    if exp_unclear != got_unclear:
        out.append(("unclear", sorted(exp_unclear), sorted(got_unclear)))
    return out
