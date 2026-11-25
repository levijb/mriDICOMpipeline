"""Load and check the round 4 exam catalog (plan 2.1 item 4, schema in plan 3.1).

A catalog file is JSON:

    {"catalog": name,
     "sources": {source_id: text},            # every tag value and expected value cites one
     "protocols": {name: series fragment},     # optional, merged under a series that names it
     "exams": [exam, ...]}

exam: ``id``, ``uid_exam`` (1-9999), ``family``, ``roster_rows`` ([[subject_id, date], ...]),
``sessions`` and ``expected`` (requirement ID -> {"outcome", "oracle", "source"}).

session: ``label``, ``zip_name`` or ``upload_path``, ``study_date``, optional ``study_time``,
``uid_study``, ``series``.

series (after merging its protocol): ``uid_series``, ``tags`` (keyword -> [value, source_id]),
``private_tags`` (list of {tag, creator, vr, value, source}), ``slices``, ``geometry``
({iop, first_position, step_mm, pixel_spacing, rows, cols, optional frame, positions_mm}),
``echoes`` (TEs in ms, or [null]), ``parts``, ``gradients`` (null or {bvals, bvecs, source}),
``sentinels``, ``folder`` ([classifier label, source_id]) and ``facts`` (the header facts the
catalog states for R4-OR-03 and R4-HDR-01, plan 3.4 field names).

``expected_facts`` returns the stated facts plus the pass-through facts: values that are a
stated tag value unchanged (for example tr_ms is RepetitionTime, already in ms by PS3.3), or a
count from the catalog's own structure (n_instances = volumes x echoes x parts x slices).
A pass-through fact that the catalog also states explicitly must agree, else loading fails.
Nothing here imports a pipeline module or runs pipeline code.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os

from r4lib import dicom_builder

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_DIR = os.path.join(HERE, "expected", "catalog")
CATALOG_FILES = ("structural", "diffusion", "susceptibility", "kept_roles", "hard_cases",
                 "geometry", "visits", "naming", "cleanup")
ORACLE_LETTERS = set("STCP")
SERIES_KEYS = ("uid_series", "tags", "slices", "geometry", "echoes", "parts")
GEOMETRY_KEYS = ("iop", "first_position", "step_mm", "pixel_spacing", "rows", "cols")
STATED_FACTS = ("orientation", "n_positions", "pos_min", "pos_max", "step_mm", "extent_mm",
                "uniform_spacing", "acq_rel_s", "series_rel_s", "bolus_rel_s", "desc_tokens",
                "manufacturer")


class CatalogError(ValueError):
    pass


def _merge(base, over):
    out = copy.deepcopy(base)
    for key, value in over.items():
        if key in ("tags", "facts") and key in out:
            merged = dict(out[key])
            merged.update(value)
            out[key] = merged
        elif key == "geometry" and key in out:
            merged = dict(out[key])
            merged.update(value)
            out[key] = merged
        else:
            out[key] = copy.deepcopy(value)
    return out


def _check_oracle(where, item, sources):
    if not isinstance(item, dict) or "oracle" not in item or "source" not in item:
        raise CatalogError(f"{where}: expected outcome needs both 'oracle' and 'source'")
    letters = {c for c in str(item["oracle"]).replace(",", "").replace(" ", "")}
    if not letters or not letters <= ORACLE_LETTERS:
        raise CatalogError(f"{where}: oracle must be S, T, C or P (got {item['oracle']!r})")
    if not str(item["source"]).strip():
        raise CatalogError(f"{where}: empty source")


def resolve_protocol(name, protocols, where, depth=0):
    """A protocol with its ``base`` chain merged in (base first)."""
    if name not in protocols:
        raise CatalogError(f"{where}: unknown protocol {name!r}")
    if depth > 5:
        raise CatalogError(f"{where}: protocol base chain too deep at {name!r}")
    proto = protocols[name]
    if "base" in proto:
        parent = resolve_protocol(proto["base"], protocols, where, depth + 1)
        return _merge(parent, {k: v for k, v in proto.items() if k != "base"})
    return proto


def expand_series(raw, protocols, sources, where):
    series = raw
    if "protocol" in raw:
        proto = dict(resolve_protocol(raw["protocol"], protocols, where))
        # Protocol facts (for example the normalized manufacturer) are kept apart, so that only
        # a series that states its own facts is checked by R4-OR-03.
        proto["protocol_facts"] = proto.pop("facts", {})
        series = _merge(proto, {k: v for k, v in raw.items() if k != "protocol"})
    for key in SERIES_KEYS:
        if key not in series:
            raise CatalogError(f"{where}: series lacks {key!r}")
    for key in GEOMETRY_KEYS:
        if key not in series["geometry"]:
            raise CatalogError(f"{where}: geometry lacks {key!r}")
    plain = {}
    for keyword, entry in series["tags"].items():
        if not (isinstance(entry, list) and len(entry) == 2):
            raise CatalogError(f"{where}: tag {keyword} must be [value, source_id]")
        if entry[1] not in sources:
            raise CatalogError(f"{where}: tag {keyword} cites unknown source {entry[1]!r}")
        plain[keyword] = entry[0]
    out = dict(series)
    out["tags_with_sources"] = series["tags"]
    out["tags"] = plain
    for spec in series.get("private_tags", []):
        if spec.get("source") not in sources:
            raise CatalogError(f"{where}: private tag {spec.get('tag')} cites unknown source")
    for spec in series.get("volume_private_tags", []):
        if spec.get("source") not in sources:
            raise CatalogError(f"{where}: volume private tag {spec.get('tag')} cites unknown source")
        if len(spec.get("values", [])) != n_volumes(series):
            raise CatalogError(f"{where}: volume private tag {spec.get('tag')} needs one value per volume")
    if "image_type_text" in series:
        value, src = series["image_type_text"]
        if src not in sources:
            raise CatalogError(f"{where}: image_type_text cites unknown source")
        out["ImageTypeText"] = value
    if series.get("gradients") and series["gradients"].get("source") not in sources:
        raise CatalogError(f"{where}: gradients cite unknown source")
    if "folder" in series and series["folder"][1] not in sources:
        raise CatalogError(f"{where}: folder cites unknown source")
    return out


def _raw(name):
    with open(os.path.join(CATALOG_DIR, f"{name}.json"), encoding="utf-8") as f:
        return json.load(f)


def load_file(name):
    """Load one catalog file. ``protocols_from`` names other catalog files whose protocols
    (and sources) this file may use; its own protocols and sources win on a name clash."""
    data = _raw(name)
    if data.get("protocols_from"):
        protocols, sources = {}, {}
        for other in data["protocols_from"]:
            raw = _raw(other)
            protocols.update(raw.get("protocols", {}))
            sources.update(raw.get("sources", {}))
        protocols.update(data.get("protocols", {}))
        sources.update(data["sources"])
        data["protocols"], data["sources"] = protocols, sources
    return check(data, name)


def check(data, name="<memory>"):
    """Validate one catalog document and expand every series. Returns the document with
    each session's series list replaced by expanded series."""
    for key in ("catalog", "sources", "exams"):
        if key not in data:
            raise CatalogError(f"{name}: missing top-level {key!r}")
    sources = data["sources"]
    protocols = data.get("protocols", {})
    seen_exam_ids, seen_uids = set(), set()
    out = copy.deepcopy(data)
    for e_index, exam in enumerate(out["exams"]):
        where = f"{name}: exam {exam.get('id', e_index)}"
        for key in ("id", "uid_exam", "family", "roster_rows", "sessions", "expected"):
            if key not in exam:
                raise CatalogError(f"{where}: missing {key!r}")
        if exam["id"] in seen_exam_ids:
            raise CatalogError(f"{where}: duplicate id")
        seen_exam_ids.add(exam["id"])
        if not (1 <= int(exam["uid_exam"]) < 10000) or exam["uid_exam"] in seen_uids:
            raise CatalogError(f"{where}: uid_exam must be unique and in 1..9999")
        seen_uids.add(exam["uid_exam"])
        for req, item in exam["expected"].items():
            _check_oracle(f"{where}: expected {req}", item, sources)
            if "outcome" not in item:
                raise CatalogError(f"{where}: expected {req} lacks 'outcome'")
        uids = set()
        for session in exam["sessions"]:
            if "label" not in session or "study_date" not in session:
                raise CatalogError(f"{where}: session needs label and study_date")
            if "zip_name" not in session and "upload_path" not in session:
                raise CatalogError(f"{where}: session needs zip_name or upload_path")
            expanded = []
            for s_index, raw in enumerate(session.get("series", [])):
                swhere = f"{where}: session {session['label']} series {raw.get('uid_series', s_index)}"
                series = expand_series(raw, protocols, sources, swhere)
                if series["uid_series"] in uids:
                    raise CatalogError(f"{swhere}: duplicate uid_series within the exam")
                uids.add(series["uid_series"])
                if not (1 <= int(series["uid_series"]) < 10000):
                    raise CatalogError(f"{swhere}: uid_series must be in 1..9999")
                if "facts" in series:
                    expected_facts(exam, series, swhere)  # raises on inconsistency
                expanded.append(series)
            session["series"] = expanded
    return out


def load_all(names=CATALOG_FILES):
    return {name: load_file(name) for name in names
            if os.path.exists(os.path.join(CATALOG_DIR, f"{name}.json"))}


def iter_series(catalogs):
    """Yield (catalog name, exam, session, series) for every series of every exam."""
    for name, data in catalogs.items():
        for exam in data["exams"]:
            for session in exam["sessions"]:
                for series in session["series"]:
                    yield name, exam, session, series


# ---------------------------------------------------------------------------- facts

def n_volumes(series):
    grads = series.get("gradients")
    return len(grads["bvals"]) if grads else int(series.get("volumes", 1))


def passthrough_facts(exam, series):
    """Facts that are a stated tag value unchanged, or a count of the catalog's structure."""
    t = series["tags"]
    g = series["geometry"]
    echoes = series.get("echoes") or [None]
    parts = series.get("parts") or ["mag"]
    vols = n_volumes(series)
    slices = int(series["slices"])
    n_instances = vols * len(echoes) * len(parts) * slices
    if series.get("stacks"):
        n_instances = sum(vols * len(s.get("echoes", echoes)) * len(s.get("parts", parts))
                          * int(s.get("slices", slices)) for s in series["stacks"])
    facts = {
        "series_number": t.get("SeriesNumber"),
        "rows": int(g["rows"]), "cols": int(g["cols"]),
        "pixel_mm": [float(v) for v in g["pixel_spacing"]],
        "tr_ms": t.get("RepetitionTime"), "ti_ms": t.get("InversionTime"),
        "flip_deg": t.get("FlipAngle"), "etl": t.get("EchoTrainLength"),
        "model": t.get("ManufacturerModelName"), "field_t": t.get("MagneticFieldStrength"),
        "mr_acq": t.get("MRAcquisitionType"), "seq_name": t.get("SequenceName"),
        "thick_mm": t.get("SliceThickness"), "sbs_mm": t.get("SpacingBetweenSlices"),
        "acq_matrix": t.get("AcquisitionMatrix"),
        "phase_dir": t.get("InPlanePhaseEncodingDirection"),
        "pct_phase_fov": t.get("PercentPhaseFieldOfView"),
        "n_instances": n_instances,
        "contrast_agent": bool(t.get("ContrastBolusAgent")),
        "multiframe": False,
    }
    for fact, keyword in (("scan_seq", "ScanningSequence"), ("seq_var", "SequenceVariant"),
                          ("scan_opt", "ScanOptions"), ("software", "SoftwareVersions")):
        value = t.get(keyword)
        if value is None:
            facts[fact] = None
        else:
            facts[fact] = sorted(set(value if isinstance(value, list) else [value]))
    image_type = set()
    base = t.get("ImageType", ["ORIGINAL", "PRIMARY", "OTHER"])
    for part in parts:
        image_type.update(dicom_builder.PART_TOKEN[part] if tok == "{part}" else tok for tok in base)
        for tok in series.get("ImageTypeText", []):
            image_type.add(dicom_builder.PART_TOKEN[part] if tok == "{part}" else tok)
    facts["image_type"] = sorted(image_type)
    if len(echoes) > 1:
        facts["te_ms"] = None
        facts["echoes"] = [{"te_ms": te, "echo_number": i + 1, "n_instances": vols * len(parts) * slices}
                           for i, te in sorted(enumerate(echoes), key=lambda p: p[1])]
    else:
        facts["te_ms"] = echoes[0]
        facts["echoes"] = None
    facts["parts"] = {p: vols * len(echoes) * slices for p in parts}
    frame = int(g.get("frame", series["uid_series"]))
    facts["for_hash"] = hashlib.sha256(dicom_builder.frame_uid(exam["uid_exam"], frame).encode("ascii")).hexdigest()
    facts["mixed"] = False
    facts["n_stacks"] = 1
    return facts


def expected_facts(exam, series, where="series"):
    stated = {**series.get("protocol_facts", {}), **series.get("facts", {})}
    facts = passthrough_facts(exam, series)
    for key, value in stated.items():
        if key == "unclear":
            continue
        if key in facts and key not in ("mixed", "n_stacks") and facts[key] != value and not (
                isinstance(value, float) and isinstance(facts[key], (int, float)) and abs(facts[key] - value) < 1e-9):
            raise CatalogError(f"{where}: stated fact {key}={value!r} contradicts the tags ({facts[key]!r})")
        facts[key] = value
    if not facts.get("mixed"):
        missing = [k for k in STATED_FACTS if k not in stated]
        if missing:
            raise CatalogError(f"{where}: facts must state {missing}")
    facts["unclear"] = list(stated.get("unclear", []))
    return facts
