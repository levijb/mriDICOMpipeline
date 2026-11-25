"""Phase 3 triage helpers for findings the real-data checks raise. Read-only toward the project; coded evidence.

    python tests/round4/realdata/r4_triage.py WHAT --project D:\\mri_project --private D:\\round4\\private \\
        --reports D:\\round4\\private\\runA2 --evidence D:\\round4\\evidence\\realdata

WHAT is one of duplicates, copies (after r4_objective.py), ge_part (after duplicates), first_echo, reviews (after
duplicates, ge_part and first_echo), varying, brief027, pixels (after r4_pixels.py). Each function's docstring says what it reads and writes.

duplicates: for every role decision with reason duplicate_series, compares the voxel data of the top-group
acquisitions' primary volumes (NiBabel). Pairs whose arrays are equal are copies, as scan-selection.md 5.5
describes; pairs that differ are separate images that share the acquisition identity (for example a
scanner-processed image labelled ORIGINAL). Writes duplicates.csv and duplicates_summary.json with visit codes,
roles, vendors, series numbers, ImageType values and voxel statistics only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

import r4real

FIELDS = ["visit", "role", "member_roles", "manufacturer", "model", "series_numbers", "image_types", "shapes", "same_shape",
          "identical", "correlation", "verdict"]


def _corr(x, y):
    import numpy as np

    x, y = x.ravel(), y.ravel()
    if x.std() == 0 or y.std() == 0:
        return 1.0 if np.array_equal(x, y) else 0.0
    return float(np.corrcoef(x, y)[0, 1])


def compare(paths):
    """(shapes, same 3D grid, identical, lowest correlation with the first image). A 3D image is matched
    against the best-correlating volume of a 4D one when their 3D grids agree."""
    import nibabel as nib
    import numpy as np

    arrays = [np.asanyarray(nib.load(p).dataobj, dtype=np.float64) for p in paths]
    shapes = [list(a.shape) for a in arrays]
    if any(a.shape[:3] != arrays[0].shape[:3] for a in arrays):
        return shapes, False, False, None
    base = arrays[0]
    identical = all(a.shape == base.shape and np.array_equal(base, a) for a in arrays[1:])
    lowest = 1.0
    for a in arrays[1:]:
        if a.shape == base.shape:
            c = _corr(base, a)
        else:
            vols_a = [a[..., i] for i in range(a.shape[3])] if a.ndim == 4 else [a]
            vols_b = [base[..., i] for i in range(base.shape[3])] if base.ndim == 4 else [base]
            c = max(_corr(x, y) for x in vols_a for y in vols_b)
        lowest = min(lowest, c)
    return shapes, True, identical, round(lowest, 4)


def verdict_of(same_grid, identical, corr):
    if not same_grid:
        return "different grid"
    if identical:
        return "copy (identical voxels)"
    if corr is not None and corr >= 0.999:
        return "copy (same image, different scaling or 4D layout)"
    if corr is not None and corr < 0.5:
        return "different images"
    return "partly similar"


def duplicates(a):
    codes = r4real.Codes(a.private)
    decisions = r4real.read_csv(os.path.join(a.reports, "selection.csv"))
    vrows = r4real.read_csv(os.path.join(a.reports, "selection_volumes.csv"))
    by_session = {}
    for r in vrows:
        by_session.setdefault((r["subject_id"], r["session"]), []).append(r)
    out = []
    for d in decisions:
        if d["outcome"] != "review_required" or "duplicate_series" not in d["reasons"]:
            continue
        rows = by_session[(d["subject_id"], d["session"])]
        # The top group: the recommended acquisition and the acquisitions it duplicates (any role; 5.5).
        rec = [r for r in rows if r["acquisition_id"] == d["rule_recommendation"]]
        group = {d["rule_recommendation"]} | (set(rec[0]["duplicate_of"].split(";")) if rec else set())
        acqs = {}
        for r in rows:
            if r["acquisition_id"] in group and r["kind"] == "native":
                acqs.setdefault(r["acquisition_id"], []).append(r)
        # Primary volume per acquisition: magnitude, first echo.
        prim = []
        for acq, members in sorted(acqs.items()):
            mags = [m for m in members if m["part"] in ("mag", "")] or members
            prim.append(sorted(mags, key=lambda m: float(m["echo_te_ms"] or 0))[0])
        if len(prim) < 2:
            out.append({"visit": codes.visit(d["subject_id"], d["session"]), "role": d["role"], "verdict": "pair not found"})
            continue
        paths = [os.path.join(a.project, "Niftis", *r["source"].split("/")) for r in prim]
        shapes, same_shape, identical, corr = compare(paths)
        verdict = verdict_of(same_shape, identical, corr)
        out.append({"visit": codes.visit(d["subject_id"], d["session"]), "role": d["role"],
                    "member_roles": "|".join(r["role"] for r in prim), "manufacturer": prim[0]["manufacturer"], "model": prim[0]["model"],
                    "series_numbers": "|".join(r["series_number"] or "?" for r in prim),
                    "image_types": "|".join(r["image_type"] for r in prim), "shapes": json.dumps(shapes),
                    "same_shape": same_shape, "identical": identical, "correlation": corr, "verdict": verdict})
    r4real.write_csv(os.path.join(a.evidence, "duplicates.csv"), out, FIELDS)
    summary = {"decisions": len(out), "verdicts": dict(Counter(r["verdict"] for r in out)),
               "by_role_verdict": dict(Counter(f"{r['role']}:{r['verdict']}" for r in out)),
               "by_member_roles": dict(Counter(f"{r['role']}:{r.get('member_roles')}:{r['verdict']}" for r in out)),
               "by_vendor_verdict": dict(Counter(f"{r.get('manufacturer', '?')}:{r['verdict']}" for r in out))}
    with open(os.path.join(a.evidence, "duplicates_summary.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, indent=1, sort_keys=True)
    print(json.dumps(summary["verdicts"]))


def copies(a):
    """Partners behind R4-VAL-02's `copies` flags: for each flagged chosen series, the other series of the session
    with the same identity (oracles/objective_checks.duplicates_of), classed by the reader's ImageType as derived
    (value 1 DERIVED or a derived token) or native, and for native partners whether their ImageType carries an SWI
    or projection token. Writes copies.csv and copies_summary.json (coded)."""
    from oracles import objective_checks as oc

    flags = [r for r in r4real.read_csv(os.path.join(a.evidence, "objective.csv")) if r["category"] == "copies"]
    codes = r4real.Codes(a.private)
    decisions ={(codes.visit(d["subject_id"], d["session"]), d["role"]): d
                 for d in r4real.read_csv(os.path.join(a.reports, "selection.csv"))}
    vrows = r4real.read_csv(os.path.join(a.reports, "selection_volumes.csv"))
    out = []
    for f in flags:
        recs = json.load(open(os.path.join(a.private, "reader", f"{f['session']}.json"), encoding="utf-8"))["series"]
        d = decisions[(f["visit"], f["role"])]
        members = {r4real.sidecar(a.project, r["source"]).get("source_series_key") for r in vrows
                   if r["subject_id"] == d["subject_id"] and r["session"] == d["session"]
                   and r["acquisition_id"] == d["candidate_id"]}
        for rec in [r for k, r in recs.items() if k.startswith(f["series"])]:
            for pk in oc.duplicates_of(rec, list(recs.values())):
                pr = recs[pk]
                it = set(pr.get("image_type") or [])
                derived = "DERIVED" in set(pr.get("image_type_value1") or []) or bool(it & oc.DERIVED_TOKENS)
                kind = "derived" if derived else ("native, SWI token" if "SWI" in it else "native")
                kind += ", member of the chosen acquisition" if pk in members else ", outside it"
                voxels, select_role, select_fate = "", "", ""
                if not derived and pk not in members:
                    srows = [r for r in vrows if r["subject_id"] == d["subject_id"] and r["session"] == d["session"]]
                    side = {r["source"]: r4real.sidecar(a.project, r["source"]).get("source_series_key") for r in srows}
                    prow = [r for r in srows if side[r["source"]] == pk]
                    crow = ([r for r in srows if r["source"] == d["source"]]
                            or [r for r in srows if r["acquisition_id"] == d["candidate_id"]])
                    if prow:
                        select_role = "/".join(sorted({r["role"] or "(none)" for r in prow}))
                        select_fate = "/".join(sorted({r["fate"] for r in prow}))
                    if prow and crow:
                        paths = [os.path.join(a.project, "Niftis", *x["source"].split("/")) for x in (crow[0], prow[0])]
                        _s, same, identical, corr = compare(paths)
                        voxels = verdict_of(same, identical, corr)
                    elif not prow:
                        voxels = "partner has no published volume"
                out.append({"visit": f["visit"], "role": f["role"], "chosen_series": f["series_number"],
                            "partner_series": pr.get("series_number"), "partner_kind": kind, "voxels": voxels,
                            "select_role": select_role, "select_fate": select_fate,
                            "partner_image_type": "\\".join(sorted(it)), "partner_parts": json.dumps(pr.get("parts"))})
    fields = ["visit", "role", "chosen_series", "partner_series", "partner_kind", "voxels", "select_role", "select_fate",
              "partner_image_type", "partner_parts"]
    r4real.write_csv(os.path.join(a.evidence, "copies.csv"), out, fields)
    per_flag = {}
    for r in out:
        per_flag.setdefault((r["visit"], r["role"], r["chosen_series"]), set()).add(r["partner_kind"])
    summary = {"flags": len(flags), "partners": len(out),
               "flags_by_partner_kinds": dict(Counter(f"{k[1]}:{'+'.join(sorted(v))}" for k, v in per_flag.items())),
               "outside_native_voxels": dict(Counter(f"{r['role']}:{r['select_role']}:{r['voxels']}" for r in out if r["voxels"])),
               "partner_image_types": dict(Counter(f"{r['role']}:{r['partner_kind']}:{r['partner_image_type']}" for r in out).most_common(15))}
    with open(os.path.join(a.evidence, "copies_summary.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(summary, fh, indent=1, sort_keys=True)
    print(json.dumps(summary["flags_by_partner_kinds"]))


GE_PART = {0: "magnitude", 1: "phase", 2: "real", 3: "imaginary"}


def _text(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="replace")
    return str(value).strip().strip("\x00").strip()


def _ge_image_type(ds):
    """(private creator of block 10 in group 0043, integer value of (0043,102F)); None where absent."""
    creator = ds.get((0x0043, 0x0010))
    element = ds.get((0x0043, 0x102F))
    if element is None:
        return _text(creator.value) if creator is not None else None, None
    value = element.value
    if isinstance(value, bytes):
        value = int.from_bytes(value[:2], "little", signed=True) if len(value) >= 2 else None
    elif isinstance(value, (list, tuple)) or value.__class__.__name__ == "MultiValue":
        value = value[0] if len(value) else None
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = _text(value)
    return _text(creator.value) if creator is not None else None, value


def _session_files(private, visit):
    with open(os.path.join(private, "reader", f"{visit}.json"), encoding="utf-8") as f:
        reader = json.load(f)["series"]
    with open(os.path.join(private, "index", f"{visit}.json"), encoding="utf-8") as f:
        index = json.load(f)
    return reader, index


def _paths(index, key):
    entry = index.get(key) or []
    return entry["paths"] if isinstance(entry, dict) else entry


def ge_part(a):
    """R4-F-021 follow-up (owner, 2026-11-25): GE's private image-type element (0043,102F), read from every
    instance of every GE native series that select files under SWI or SWI_PHASE. Values: 0 magnitude, 1 phase,
    2 real, 3 imaginary. Headers only. Writes ge_part.csv and ge_part_summary.json (coded)."""
    import pydicom
    from pydicom.tag import Tag

    codes = r4real.Codes(a.private)
    vrows = r4real.read_csv(os.path.join(a.reports, "selection_volumes.csv"))
    partners = set()
    for r in r4real.read_csv(os.path.join(a.evidence, "duplicates.csv")):
        if r["verdict"] == "different images":
            for role, number in zip(r["member_roles"].split("|"), r["series_numbers"].split("|")):
                if role == "SWI_PHASE":
                    partners.add((r["visit"], number))
    targets = {}
    for r in vrows:
        if r["manufacturer"] == "GE" and r["kind"] == "native" and r["role"] in ("SWI", "SWI_PHASE"):
            targets.setdefault((codes.visit(r["subject_id"], r["session"]), r["series_number"]), set()).add(
                (r["role"], r["part"] or "mag", r["fate"]))
    tags = [Tag(0x0043, 0x0010), Tag(0x0043, 0x102F)]
    out = []
    for (visit, number), info in sorted(targets.items()):
        reader, index = _session_files(a.private, visit)
        keys = [k for k, s in reader.items() if str(s.get("series_number")) == number]
        values, creators = Counter(), Counter()
        for k in keys:
            for path in _paths(index, k):
                creator, value = _ge_image_type(pydicom.dcmread(path, stop_before_pixels=True, specific_tags=tags))
                creators[creator] += 1
                values[GE_PART.get(value, value) if value is not None else "absent"] += 1
        out.append({"visit": visit, "series_number": number, "select_role": "/".join(sorted({i[0] for i in info})),
                    "select_part": "/".join(sorted({i[1] for i in info})), "select_fate": "/".join(sorted({i[2] for i in info})),
                    "r4_f_021_partner": (visit, number) in partners, "instances": sum(values.values()),
                    "ge_0043_102F": json.dumps(dict(values), sort_keys=True),
                    "private_creator": json.dumps(dict(creators), sort_keys=True),
                    "image_type": "\\".join(sorted({t for k in keys for t in (reader[k].get("image_type") or [])}))})
    fields = ["visit", "series_number", "select_role", "select_part", "select_fate", "r4_f_021_partner", "instances",
              "ge_0043_102F", "private_creator", "image_type"]
    r4real.write_csv(os.path.join(a.evidence, "ge_part.csv"), out, fields)
    summary = {"series": len(out), "r4_f_021_partner_series": sum(1 for r in out if r["r4_f_021_partner"]),
               "by_group": dict(Counter(f"{r['select_role']}|select part {r['select_part']}|F-021 partner "
                                        f"{r['r4_f_021_partner']}|0043,102F {r['ge_0043_102F']}" for r in out)),
               "private_creators": dict(Counter(r["private_creator"] for r in out))}
    with open(os.path.join(a.evidence, "ge_part_summary.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(summary, fh, indent=1, sort_keys=True)
    print(json.dumps(summary, indent=1))


PARENT_ROLE = {"ADC": "DWI", "EADC": "DWI", "SWI_MINIP": "SWI"}
SPEC_REASONS = {
    "metadata_unavailable": "no eligible candidate; those failing on evidence alone have family UNCLASSIFIED (5.3, 6.2)",
    "label_parameter_conflict": "winner check: label and parameters name different roles (5.3, 6.4)",
    "repeat_needs_review": "same-protocol repeat under the default repeat_policy review (6.4)",
    "unresolved_tie": "equal ranking keys with no repeat or duplicate rule to settle them (6.4)",
    "contrast_unknown": "winner's contrast phase unknown in a pre or post role (5.7, 6.4)",
    "mixed_series": "no eligible candidate; those failing on evidence alone come from a mixed series (D10, 6.2)",
    "partial_coverage": "extent below the role's min_extent_mm (6.2)",
}


def _maker_of(vrows):
    makers = {}
    for r in vrows:
        if r["manufacturer"]:
            makers.setdefault((r["subject_id"], r["session"]), Counter())[r["manufacturer"]] += 1
    return {k: v.most_common(1)[0][0] for k, v in makers.items()}


def reviews(a):
    """R4-F-022 follow-up (owner, 2026-11-25): the review decisions left once the R4-F-021 decisions (GE phase
    series filed as duplicates; duplicates.csv verdict 'different images') are set aside, by role and reason, each
    reason classed as a defect, a specification gap or required by the specification, with its basis. Reads
    duplicates.csv and, when present, ge_part.csv and first_echo.csv. Writes reviews.csv and reviews_summary.json."""
    codes = r4real.Codes(a.private)
    decisions = r4real.read_csv(os.path.join(a.reports, "selection.csv"))
    vrows = r4real.read_csv(os.path.join(a.reports, "selection_volumes.csv"))
    maker = _maker_of(vrows)
    verdict = {(r["visit"], r["role"]): r["verdict"] for r in r4real.read_csv(os.path.join(a.evidence, "duplicates.csv"))}
    f021 = {k for k, v in verdict.items() if v == "different images"}
    ge_path = os.path.join(a.evidence, "ge_part.csv")
    ge = {(r["visit"], r["series_number"]): r["ge_0043_102F"] for r in r4real.read_csv(ge_path)} if os.path.exists(ge_path) else {}
    by_session = {}
    for r in vrows:
        by_session.setdefault((r["subject_id"], r["session"]), []).append(r)
    dec = {(d["subject_id"], d["session"], d["role"]): d for d in decisions}

    def classify(d, visit, reason):
        rows = by_session[(d["subject_id"], d["session"])]
        if reason == "duplicate_series":
            v = verdict.get((visit, d["role"]), "")
            if v.startswith("copy"):
                return "spec", "true copies (voxels identical or rescaled): review, either may be chosen (5.5)"
            return "spec", f"same acquisition identity, voxels {v} (5.5 as written is header-only)"
        if reason == "parent_review":
            parent = PARENT_ROLE.get(d["role"], "")
            if (visit, parent) in f021:
                return "defect", f"follows R4-F-021: the parent {parent} decision is an R4-F-021 review"
            return "spec", f"the parent {parent} decision is in review (6.5 rule 3)"
        if reason == "derived_unlinked":
            groups = [r for r in rows if r["role"] == d["role"] and r["kind"] != "native"]
            states = {r["link_state"] for r in groups}
            if states == {"no_parent"}:
                pd = dec.get((d["subject_id"], d["session"], PARENT_ROLE.get(d["role"], "")))
                if pd and pd["outcome"] == "missing":
                    return "spec gap", (f"R4-F-028: no {pd['role']} acquisition in the session, so the map has no parent "
                                        "candidate; 6.5 names unlinked and ambiguous groups only")
                if pd and pd["candidate_id"]:
                    chosen = [r for r in rows if r["acquisition_id"] == pd["candidate_id"]]
                    roles = {r["role"] for r in chosen}
                    if roles - {pd["role"]}:
                        tags = "; ".join(sorted({ge.get((visit, r["series_number"]), "not read") for r in chosen}))
                        return "defect", (f"R4-F-029: the {pd['role']} decision chose an acquisition whose role is "
                                          f"{'/'.join(sorted(roles))} (0043,102F: {tags}); the map's parent roles "
                                          f"are {pd['role']} only")
            return "spec", "derived group unlinked or ambiguous (6.5 rule 6)"
        if reason in SPEC_REASONS:
            return "spec", SPEC_REASONS[reason]
        return "unclassified", ""

    out = []
    for d in decisions:
        if d["outcome"] != "review_required":
            continue
        visit = codes.visit(d["subject_id"], d["session"])
        if (visit, d["role"]) in f021:
            continue
        for item in [x for x in d["reasons"].split(";") if x]:
            reason = item.split("(")[0]
            cls, basis = classify(d, visit, reason)
            out.append({"visit": visit, "role": d["role"], "manufacturer": maker.get((d["subject_id"], d["session"]), "?"),
                        "reason": reason, "class": cls, "basis": basis, "reasons_of_decision": d["reasons"]})
    fields = ["visit", "role", "manufacturer", "reason", "class", "basis", "reasons_of_decision"]
    r4real.write_csv(os.path.join(a.evidence, "reviews.csv"), out, fields)
    table = Counter((r["role"], r["reason"], r["class"], r["basis"]) for r in out)
    per_decision = {}
    for r in out:
        per_decision.setdefault((r["visit"], r["role"]), set()).add(r["class"])
    worst = Counter(next(c for c in ("defect", "spec gap", "unclassified", "spec") if c in v) for v in per_decision.values())
    base = sum(1 for d in decisions if d["outcome"] not in ("missing", "session_excluded", "source_not_ready")
               and int(d["n_candidates"] or 0) >= 1)
    summary = {"review_decisions_after_r4_f_021": len(per_decision), "reason_rows": len(out),
               "decisions_by_class": dict(worst),
               "table": [{"role": k[0], "reason": k[1], "class": k[2], "basis": k[3], "decisions": n}
                         for k, n in sorted(table.items())],
               "review_rate_base": base}
    fe_path = os.path.join(a.evidence, "first_echo.csv")
    moved = sum(1 for r in r4real.read_csv(fe_path) if r["text_outcome"].startswith("review_required")) if os.path.exists(fe_path) else 0
    follows = {k for k, v in per_decision.items() if v == {"defect"}
               and all("follows R4-F-021" in r["basis"] for r in out if (r["visit"], r["role"]) == k)}
    no_parent = {(r["visit"], r["role"]) for r in out if r["basis"].startswith(("R4-F-028", "R4-F-029"))}
    left = len(per_decision) - len(follows) + moved
    summary["what_if"] = {
        "note": ("what-if only. after_phase_4: the R4-F-021 decisions and the reviews that follow them resolved, "
                 "R4-F-016's text rule applied (R4-F-029's map then has no parent and stays, as R4-F-028). "
                 "and_no_parent_not_review: also if the owner's reading of R4-F-028 takes a map with no parent "
                 "out of review"),
        "after_phase_4": {"reviews": left, "rate": r4real.wilson(left, base)},
        "and_no_parent_not_review": {"reviews": left - len(no_parent), "rate": r4real.wilson(left - len(no_parent), base)}}
    with open(os.path.join(a.evidence, "reviews_summary.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(summary, fh, indent=1, sort_keys=True)
    print(json.dumps({k: summary[k] for k in ("review_decisions_after_r4_f_021", "decisions_by_class", "what_if")}))


def _tesla(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return round(value / 10000 if value > 100 else value, 3)


def _maker(text):
    upper = (_text(text) or "").upper()
    if upper.startswith("GE") or "GENERAL ELECTRIC" in upper:
        return "GE"
    for name in ("SIEMENS", "PHILIPS"):
        if upper.startswith(name):
            return name
    return upper.split()[0] if upper.split() else None


def _most_common(values):
    counts = Counter(v for v in values if v not in (None, ""))
    return sorted(counts, key=lambda v: (-counts[v], str(v)))[0] if counts else None


def varying(a):
    """Unclear rows 'value varies within the series' for field_t, manufacturer and model (owner reading,
    2026-11-25: accepted on condition that no decision changes whichever instance's value is used). field_t and
    manufacturer reach a decision only through the profile's field_strength and manufacturer requirements, which
    use the session's most common value over its series; model reaches none. For each such series, every value
    its instances hold is put in place of the value select recorded, and the session's most common value is
    recomputed. Reads instance headers. Writes varying.csv and varying_summary.json (scanner values only)."""
    import pydicom

    codes = r4real.Codes(a.private)
    label_of = {c: k.split("/", 1) for k, c in codes.sessions.items()}
    norm = {"field_t": _tesla, "manufacturer": _maker, "model": _text}
    out = []
    for name in sorted(os.listdir(os.path.join(a.private, "reader"))):
        visit = name[:-5]
        reader, index = _session_files(a.private, visit)
        hits = [(k, s) for k, s in reader.items() if isinstance(s.get("unclear"), dict)
                and any(s["unclear"].get(f) == "varies_within_series" for f in norm)]
        if not hits:
            continue
        sid, label = label_of[visit]
        cache = r4real.cache_doc(a.project, sid, label)["series"]
        for key, s in hits:
            values = {f: Counter() for f in norm}
            for path in _paths(index, key):
                ds = pydicom.dcmread(path, stop_before_pixels=True,
                                     specific_tags=["Manufacturer", "ManufacturerModelName", "MagneticFieldStrength"])
                values["manufacturer"][_maker(ds.get("Manufacturer"))] += 1
                values["model"][_text(ds.get("ManufacturerModelName"))] += 1
                values["field_t"][_tesla(ds.get("MagneticFieldStrength"))] += 1
            for field, fn in norm.items():
                if s["unclear"].get(field) != "varies_within_series":
                    continue
                recorded = fn(cache.get(key, {}).get(field))
                others = [fn(c.get(field)) for k, c in cache.items() if k != key]
                session_value = _most_common(others + [recorded])
                alternatives = {str(v): _most_common(others + [v]) for v in values[field]}
                changes = field != "model" and any(m != session_value for m in alternatives.values())
                out.append({"visit": visit, "series_number": s.get("series_number"), "field": field,
                            "mixed": s.get("mixed"), "instances": sum(values[field].values()),
                            "instance_values": json.dumps({str(k): n for k, n in values[field].items()}, sort_keys=True),
                            "select_value": recorded, "session_value": session_value,
                            "session_value_by_instance_value": json.dumps(alternatives, sort_keys=True),
                            "reaches_a_decision": "no rule uses it" if field == "model" else
                            "only through the profile's " + ("field_strength" if field == "field_t" else "manufacturer")
                            + " requirement (not set in run A2)",
                            "decision_could_change": changes})
    fields = ["visit", "series_number", "field", "mixed", "instances", "instance_values", "select_value", "session_value",
              "session_value_by_instance_value", "reaches_a_decision", "decision_could_change"]
    r4real.write_csv(os.path.join(a.evidence, "varying.csv"), out, fields)
    summary = {"rows": len(out), "by_field": dict(Counter(r["field"] for r in out)),
               "decision_could_change": sum(1 for r in out if r["decision_could_change"]),
               "mixed_series": sum(1 for r in out if r["mixed"])}
    with open(os.path.join(a.evidence, "varying_summary.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(summary, fh, indent=1, sort_keys=True)
    print(json.dumps(summary))


def first_echo(a):
    """R4-F-016 on real data (owner ruling 2026-11-25: the text wins). The text takes a multi-echo acquisition's
    family from its first echo (lowest echo time among its non-phase volumes); the code takes the first echo
    whose family is not UNCLASSIFIED. For each acquisition where the two give different families, its role
    decision and what the text's rule would give. Writes first_echo.csv and first_echo_summary.json (coded)."""
    codes = r4real.Codes(a.private)
    decisions = {(d["subject_id"], d["session"], d["role"]): d
                 for d in r4real.read_csv(os.path.join(a.reports, "selection.csv"))}
    acqs = {}
    for r in r4real.read_csv(os.path.join(a.reports, "selection_volumes.csv")):
        if r["kind"] == "native" and r["acquisition_id"]:
            acqs.setdefault((r["subject_id"], r["session"], r["acquisition_id"]), []).append(r)

    def te(r):
        return float(r["echo_te_ms"]) if r["echo_te_ms"] else None

    out = []
    for (sid, date, acq), members in sorted(acqs.items()):
        mags = [m for m in members if m["part"] != "phase"] or members
        code = sorted(mags, key=lambda m: (m["family"] == "UNCLASSIFIED", te(m) is None, te(m) or 0, m["source"]))[0]
        text = sorted(mags, key=lambda m: (te(m) is None, te(m) or 0, m["source"]))[0]
        if code["family"] == text["family"]:
            continue
        role = code["role"]
        d = decisions.get((sid, date, "SWI" if role == "SWI_PHASE" else role))
        chosen = bool(d) and d["candidate_id"] == acq
        unusable = text["family"] == "UNCLASSIFIED" or (text["family"].endswith("_UNKNOWN") and text["family"] != "DWI_UNKNOWN")
        if not chosen:
            effect = "not chosen: the choice does not change"
        elif unusable and int(d["n_eligible"] or 0) <= 1:
            effect = "review_required (metadata_unavailable): it was the only eligible candidate"
        elif unusable:
            effect = "another eligible candidate would be chosen"
        else:
            effect = "family and tier change: decision to recompute"
        out.append({"visit": codes.visit(sid, date), "role": role, "manufacturer": code["manufacturer"],
                    "series_number": code["series_number"], "code_family": code["family"], "code_echo_ms": code["echo_te_ms"],
                    "text_family": text["family"], "text_echo_ms": text["echo_te_ms"],
                    "outcome": d["outcome"] if d else "", "chosen": chosen, "text_outcome": effect})
    fields = ["visit", "role", "manufacturer", "series_number", "code_family", "code_echo_ms", "text_family",
              "text_echo_ms", "outcome", "chosen", "text_outcome"]
    r4real.write_csv(os.path.join(a.evidence, "first_echo.csv"), out, fields)
    summary = {"acquisitions_where_rules_differ": len(out), "by_role_effect": dict(Counter(f"{r['role']}: {r['text_outcome']}" for r in out))}
    with open(os.path.join(a.evidence, "first_echo_summary.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(summary, fh, indent=1, sort_keys=True)
    print(json.dumps(summary))


def _png(path, image):
    """Write a 2D uint8 array as an 8-bit greyscale PNG (no imaging library in the environment)."""
    import struct
    import zlib

    height, width = image.shape
    raw = b"".join(b"\x00" + image[i].tobytes() for i in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _mid_slice_png(nifti, png, volume=0):
    """Axial mid-slice of one volume, in RAS orientation (anterior at the top, patient's left on the left), scaled
    between the 1st and 99th percentiles of its voxels (zero and fill values left out) and enlarged to at least 384 pixels."""
    import nibabel as nib
    import numpy as np

    img = nib.as_closest_canonical(nib.load(nifti))
    data = np.asanyarray(img.dataobj, dtype=np.float64)
    if data.ndim == 4:
        data = data[..., volume]
    s = np.rot90(data[:, :, data.shape[2] // 2])
    fill = s.min()  # some scanner maps fill the background with the type's minimum (-32768) instead of 0
    mask = (s != 0) & ((s != fill) | ((s == fill).mean() < 0.05))
    inside_head = s[mask] if mask.mean() > 0.05 else s.ravel()
    lo, hi = np.percentile(inside_head, [1, 99])
    scaled = np.clip((s - lo) / (hi - lo) if hi > lo else s * 0, 0, 1)
    factor = max(1, int(np.ceil(384 / max(scaled.shape))))
    scaled = np.kron(scaled, np.ones((factor, factor)))
    _png(png, (scaled * 255).round().astype(np.uint8))
    return img.shape, [round(float(z), 3) for z in img.header.get_zooms()[:3]]


F027 = [
    ("V077", "QSM", ["502", "500", "503"], "502",
     "The frozen checks flag it: its ScanningSequence is RM (GE research mode) without GR, and series 500, 502 and "
     "503 share one acquisition identity.",
     "Is series 502 a quantitative susceptibility (QSM) map: flat grey background, iron-rich deep grey matter and "
     "veins standing out bright or dark, no magnitude anatomy?"),
    ("V077", "SWI", ["503", "500", "502"], "503",
     "The frozen checks flag it for the same two reasons as case 1.",
     "Is series 503 a susceptibility-weighted image: magnitude-like anatomy with veins shown dark, rather than a "
     "plain magnitude image (series 500) or a QSM map?"),
    ("V001", "DWI", ["7", "700", "701"], "7",
     "The frozen copies check flags it: series 7 and 700 share an acquisition identity; the best-matching volume of "
     "series 7 correlates with series 700 at 0.67.",
     "Is series 7 (34 volumes) the diffusion scan, with series 700 and 701 (one volume each) being images the "
     "scanner computed from it (such as a trace-weighted or ADC image) rather than separate diffusion scans?"),
]


def brief027(a):
    """R4-F-027 (owner, 2026-11-25): writes <private>/R4-F-027.md with each case's NIfTI paths and its yes/no
    question, and mid-slice PNGs in <private>/R4-F-027/. Every file holds paths, so all stay in the private folder."""
    codes = r4real.Codes(a.private)
    vrows = r4real.read_csv(os.path.join(a.reports, "selection_volumes.csv"))
    folder = os.path.join(a.private, "R4-F-027")
    os.makedirs(folder, exist_ok=True)
    lines = ["# R4-F-027: three choices to check by eye", "",
             "Private (paths). Code under test `4d8a362`; `select`'s first-run reports (run A2). For each case: what `select`",
             "chose, the files, axial mid-slice PNGs in the `R4-F-027` folder beside this file, and one question. **Yes** means",
             "the choice is right. Please answer yes, no or cannot tell, per case.", "",
             "PNGs: RAS orientation (anterior at the top, the patient's left on the left), grey scale from the 1st to the 99th",
             "percentile of the slice's voxels, zero and fill values left out. A 4D series shows volume 0 and its last volume.", ""]
    for n, (visit, role, numbers, chosen, why, question) in enumerate(F027, 1):
        lines += [f"## Case {n}: {visit} {role} (series {chosen} chosen)", "", why, "",
                  "| Series | `select`'s role | Fate | ImageType | Volumes | NIfTI | PNG |", "|---|---|---|---|---|---|---|"]
        for number in numbers:
            rows = [r for r in vrows if codes.visit(r["subject_id"], r["session"]) == visit and r["series_number"] == number
                    and r["kind"] == "native"]
            for r in rows:
                path = os.path.join(a.project, "Niftis", *r["source"].split("/"))
                pngs = []
                vols = int(r["n_volumes"] or 1)
                for vol in ([0, vols - 1] if vols > 1 else [0]):
                    png = f"case{n}_{visit}_series{number}_vol{vol}.png"
                    _mid_slice_png(path, os.path.join(folder, png), vol)
                    pngs.append(f"`R4-F-027/{png}`")
                lines.append(f"| {number} | {r['role'] or '(none)'} | {r['fate']} | `{r['image_type']}` | {vols} | `{path}` | "
                             f"{', '.join(pngs)} |")
        lines += ["", f"**Question {n}:** {question}", "", "Answer: ", ""]
    with open(os.path.join(a.private, "R4-F-027.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    print(f"wrote R4-F-027.md and {len(os.listdir(folder))} PNGs in the private folder")


CHOSEN_FATES = {"selected", "selected_derived"}


def pixels(a):
    """R4-VAL-05 triage (after r4_pixels.py): joins each sampled series part in pixels.csv to select's volumes of that
    series (through the sidecars' source_series_key) and the reader's ImageType, so the statuses can be counted for
    the volumes select chose and by role and make. Writes pixels_triage.csv and pixels_triage_summary.json (coded)."""
    codes = r4real.Codes(a.private)
    vrows = r4real.read_csv(os.path.join(a.reports, "selection_volumes.csv"))
    maker = _maker_of(vrows)
    vols = {}
    for r in vrows:
        visit = codes.visit(r["subject_id"], r["session"])
        key = r4real.sidecar(a.project, r["source"]).get("source_series_key") or ""
        v = vols.setdefault((visit, key), {"roles": set(), "fates": set(), "kinds": set(),
                                           "maker": maker.get((r["subject_id"], r["session"]), "?")})
        v["roles"].add(r["role"] or "(none)")
        v["fates"].add(r["fate"])
        v["kinds"].add(r["kind"])
    readers = {}
    out = []
    for p in r4real.read_csv(os.path.join(a.evidence, "pixels.csv")):
        visit = p["session"]
        if visit not in readers:
            readers[visit], _ = _session_files(a.private, visit)
        keys = [k for k in readers[visit] if k.startswith(p["series"])]
        info = next((vols[(visit, k)] for k in keys if (visit, k) in vols), {"roles": set(), "fates": set(), "kinds": set(), "maker": "?"})
        rec = readers[visit].get(keys[0], {}) if keys else {}
        out.append({"visit": visit, "series_number": p["series_number"], "part": p["part"], "status": p["status"],
                    "reason": p["reason"], "niftis": p["niftis"], "kept": p["kept"], "mismatches": p["mismatches"],
                    "roles": "/".join(sorted(info["roles"])), "fates": "/".join(sorted(info["fates"])),
                    "kinds": "/".join(sorted(info["kinds"])), "chosen": bool(info["fates"] & CHOSEN_FATES),
                    "manufacturer": info["maker"], "image_type": "\\".join(rec.get("image_type") or []),
                    "n_instances": rec.get("n_instances", "")})
    fields = ["visit", "series_number", "part", "status", "reason", "niftis", "kept", "mismatches", "roles", "fates", "kinds",
              "chosen", "manufacturer", "image_type", "n_instances"]
    r4real.write_csv(os.path.join(a.evidence, "pixels_triage.csv"), out, fields)

    def label(r):
        return f"{r['status']}: {r['reason']}" if r["reason"] else r["status"]
    chosen = [r for r in out if r["chosen"]]
    summary = {"series_parts": len(out), "status": dict(Counter(r["status"] for r in out)),
               "chosen_series_parts": len(chosen), "chosen_status": dict(Counter(label(r) for r in chosen)),
               "status_by_manufacturer": dict(Counter(f"{r['manufacturer']}: {label(r)}" for r in out)),
               "not_compared_by_kind": dict(Counter(f"{r['reason']} | {r['kinds'] or '(no volume)'}" for r in out
                                                    if r["status"] == "not compared")),
               "echo_time_ambiguity_by_niftis": dict(Counter(f"{r['niftis']} files, {r['manufacturer']}, {r['roles']}" for r in out
                                                             if r["reason"].startswith("instance echo time")).most_common(25)),
               "under_sampled_by_role": dict(Counter(f"{r['roles']} ({r['kinds']})" for r in out if r["status"] == "under-sampled")),
               "chosen_not_matched": [{k: r[k] for k in ("visit", "series_number", "part", "status", "reason", "roles", "niftis")}
                                      for r in chosen if r["status"] != "matched"]}
    with open(os.path.join(a.evidence, "pixels_triage_summary.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(summary, fh, indent=1, sort_keys=True)
    print(json.dumps({k: summary[k] for k in ("series_parts", "status", "chosen_series_parts", "chosen_status")}, indent=1))


def main(argv=None):
    p = argparse.ArgumentParser(description="Phase 3 triage helpers")
    p.add_argument("what", choices=["duplicates", "copies", "ge_part", "reviews", "varying", "first_echo", "brief027",
                                    "pixels"])
    p.add_argument("--project", required=True)
    p.add_argument("--private", required=True)
    p.add_argument("--reports", required=True)
    p.add_argument("--evidence", required=True)
    a = p.parse_args(argv)
    r4real.check_outputs(a.project, a.private, a.evidence)
    {"duplicates": duplicates, "copies": copies, "ge_part": ge_part, "reviews": reviews, "varying": varying,
     "first_echo": first_echo, "brief027": brief027, "pixels": pixels}[a.what](a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
