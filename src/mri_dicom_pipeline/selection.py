"""The select command: one acquisition per modality folder (design: scan selection, section 7).

Reads prepared and converted sessions, never changes them, and writes its own
reports (selection.csv, selection_volumes.csv) to the output root. With
--publish it also builds the output tree: each modality folder holds one
acquisition, and every other volume goes to OTHER/<modality>/ or
OTHER/UNCLASSIFIED/. Files are copies of the published NIfTI outputs.

The sessions of one subject on one date (20260725, 20260725_2) are one visit:
their scans are ranked together and published to Selected/Subjects/<id>/<date>/
(owner request, 2026-11-19).

With --bids the same decisions are named and arranged as a BIDS dataset instead,
sub-<id>/ses-<date>/ under PROJECT_ROOT/BIDS (bids.py; design: BIDS output).
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import re
import signal
import time
import uuid

from . import __version__, bids, config, convert, fsutil, identity, receipts, recovery, reports
from . import selection_identity as sid_
from . import selection_metadata as meta
from . import selection_rules as rules
from .state import State, refuse_moved_project

OVERRIDE_ROLES = set(rules.RANKED) | set(rules.FOLLOWING)
SELECTOR_VERSION = f"{__version__}+rules{rules.RULES_REVISION}"
SELECTION_FIELDS = [
    "subject_id", "session", "sessions", "role", "outcome", "decided_by", "candidate_id", "source", "n_files",
    "rule_recommendation", "runner_up", "n_candidates", "n_eligible", "reasons", "warnings", "series_numbers",
    "family", "tier", "contrast_phase", "contrast_evidence", "orientation", "mr_acq", "native_mm", "n_echoes",
    "has_phase", "n_dirs", "b_max", "link_rung", "manufacturer", "model", "field_t", "software", "protocol_change",
    "profile", "profile_version", "profile_sha256", "selector_version"]
VOLUME_FIELDS = [
    "subject_id", "session", "volume_id", "source", "folder", "role", "role_rule", "family", "kind", "part",
    "echo_te_ms", "acquisition_id", "duplicate_of", "derived_group_id", "link_state", "link_rung", "link_parent",
    "link_candidates", "candidate_id", "tier", "eligible", "exclusion_reasons", "rank", "fate", "selected_path",
    "export_status", "export_reason", "published",
    "series_number", "image_type", "mr_acq", "scan_seq", "seq_var", "seq_name", "tr_ms", "te_ms", "ti_ms",
    "flip_deg", "orientation", "grid_mm", "native_mm", "gap_mm", "n_slices", "n_volumes", "extent_mm",
    "uniform_spacing", "eq_companion", "b0", "shells", "b_max", "n_dirs", "pe_dir", "contrast_phase",
    "contrast_evidence", "desc_tokens", "manufacturer", "model", "field_t", "software", "variant",
    "has_acq_matrix", "has_phase_dir", "has_image_type_text", "warnings"]
CHANGE_FIELDS = ("manufacturer", "model", "field_t", "family", "mr_acq")
# Header times are seconds after each session's first scan. In a visit, each later session's times and
# series numbers are moved past the earlier ones', so timing rules (contrast phase, repeat order) see the
# _2 session as the later one, as preparation names it, without storing times of day.
VISIT_GAP_S = 86400
VISIT_NUMBER_GAP = 1_000_000


def session_order(label):
    """Position of a session within its date: 20260725 is 1, 20260725_2 is 2."""
    _, _, suffix = label.partition("_")
    return int(suffix) if suffix.isdigit() else 1


def visit_series(parts):
    """One visit's series records, from each session's records in session order.

    Each record of a multi-session visit names its session's position as visit_part.
    """
    if len(parts) == 1:
        return parts[0]
    merged = {}
    for k, series in enumerate(parts):
        for key, record in series.items():
            record = dict(record, visit_part=k)
            if k:
                for field in ("acq_rel_s", "series_rel_s", "bolus_rel_s"):
                    if record.get(field) is not None:
                        record[field] += k * VISIT_GAP_S
                if record.get("series_number") is not None:
                    record["series_number"] += k * VISIT_NUMBER_GAP
            merged[key] = record
    return merged


class SourceChanged(ValueError):
    """A published NIfTI output no longer matches its conversion inventory."""


def fmt(value):
    """Fixed formatting for CSV cells: no exponent noise, booleans as true/false."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return ("%.3f" % value).rstrip("0").rstrip(".")
    if isinstance(value, (list, tuple, set)):
        return ";".join(fmt(x) for x in value)
    return str(value)


def load_subjects(path, column):
    """Subject IDs from a CSV with a header row (7.1, --subjects)."""
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if column not in (reader.fieldnames or []):
            raise ValueError(f"Subject list {path} has no {column!r} column")
        found = set()
        for line, row in enumerate(reader, 2):
            subject = (row.get(column) or "").strip()
            if not subject:
                continue
            if not identity.valid_id(subject):
                raise ValueError(f"Subject list row {line}: invalid {column}")
            found.add(subject)
    if not found:
        raise ValueError(f"Subject list {path} contains no subject IDs")
    return found


def load_overrides(path):
    """{(subject, session): {role: {"choose", "none", "exclude"}}} from the reviewers' CSV (6.8)."""
    path = Path(path)
    if not path.exists():
        return {}
    result = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        needed = {"subject_id", "session", "role", "action", "candidate_id"}
        if not needed <= set(reader.fieldnames or []):
            raise ValueError(f"Overrides file {path} needs columns: {', '.join(sorted(needed))}")
        for line, row in enumerate(reader, 2):
            values = {k: (row.get(k) or "").strip() for k in needed}
            if not any(values.values()):
                continue

            def fail(message):
                raise ValueError(f"Overrides file {path}, row {line}: {message}")

            try:
                key = identity.parse_key(f"{values['subject_id']}/{values['session']}")
            except ValueError:
                fail("invalid subject_id or session")
            role, action, candidate = values["role"], values["action"], values["candidate_id"]
            if role not in OVERRIDE_ROLES:
                fail(f"role must be one of {', '.join(sorted(OVERRIDE_ROLES))}")
            if action not in ("choose", "exclude", "none"):
                fail("action must be choose, exclude or none")
            if action == "none" and candidate:
                fail("candidate_id must be empty for none")
            if action != "none" and not re.fullmatch(r"[0-9a-f]{12}", candidate):
                fail("candidate_id must be a 12-character ID from the selection reports")
            entry = result.setdefault(key, {}).setdefault(role, {"choose": None, "none": False, "exclude": set()})
            if action in ("choose", "none") and (entry["choose"] or entry["none"]):
                fail("a second choose/none row for this session and role")
            if action == "choose":
                entry["choose"] = candidate
            elif action == "none":
                entry["none"] = True
            else:
                entry["exclude"].add(candidate)
    return result


def _list(text, convert_item=str, name="value"):
    if text is None:
        return None
    items = [x.strip() for x in str(text).split(",") if x.strip()]
    if not items:
        raise ValueError(f"--{name} needs at least one value")
    try:
        return [convert_item(x) for x in items]
    except ValueError:
        raise ValueError(f"--{name}: {text!r} is not a comma-separated list of numbers") from None


def options_from(cfg):
    """Command-line selection options as profile options (rules.load_profile)."""
    return {"modalities": _list(cfg.modalities, name="modalities"), "extras": cfg.extras,
            "complete_only": cfg.complete_only, "field_strength": _list(cfg.field_strength, float, "field-strength"),
            "manufacturer": _list(cfg.manufacturer, name="manufacturer"),
            "require": {"mr_acq": "3D" if cfg.three_d_only else None, "isotropic": cfg.isotropic,
                        "max_voxel_mm": cfg.max_voxel_mm, "min_directions": cfg.min_directions,
                        "no_fallback": cfg.no_fallback}}


def readiness(entry, cfg, blocked, intent_keys, profile):
    """('', conversion record) when the session can be evaluated, else (reason, None) (7.2)."""
    key = f"{entry['subject_id']}/{entry['label']}"
    conversion = convert.complete_record(entry, cfg.niftis / "Subjects" / entry["subject_id"] / entry["label"])
    if conversion is None:
        return "not_converted", None
    if not receipts.fast_valid(conversion, "convert", cfg.audit):
        return "output_modified", None
    if conversion.get("source_sha256") != entry.get("content_sha256"):
        return "needs_conversion", None
    if not receipts.fast_valid(entry.get("extraction") or {}, "extract", cfg.audit):
        return "extraction_invalid", None
    if key in blocked or key in intent_keys:
        return "recovery_open", None
    if conversion.get("accepted_errors") and not profile["allow_accepted_partial"]:
        return "accepted_partial", None
    return "", conversion


def bids_root(cfg, profile, logger):
    """The contrast agent of a --bids run after its checks (BIDS design 5.2), or None without --bids.

    A BIDS run refuses extras and roles it doesn't export, and writes only to an empty root or one
    with its marker; the first run records the agent and later runs reuse it. The default layout
    refuses a BIDS root.
    """
    if not cfg.bids:
        if (cfg.output / bids.MARKER).exists():
            raise ValueError(f"{cfg.output} holds a BIDS export; add --bids or choose another --output-root")
        return None
    if profile["extras"]:
        raise ValueError("--bids exports selections only; remove extras from the command line or the profile")
    refused = sorted(set(profile["modalities"] or ()) & bids.NOT_EXPORTED)
    if refused:
        raise ValueError(f"--bids does not export {', '.join(refused)}; leave it out of --modalities")
    marker = bids.read_marker(cfg.output)
    bids.check_root(cfg.output, marker)
    recorded = (marker or {}).get("contrast_agent")
    agent = cfg.bids_contrast_agent or recorded or "unknown"
    if recorded and recorded != agent:
        logger.warning("Contrast agent changed from %s to %s: post-contrast T1w files are renamed", recorded, agent)
    if not cfg.dry_run:
        bids.write_marker(cfg.output, agent)
    return agent


def _grid(cfg, v):
    try:
        return meta.nifti_grid(cfg.niftis / v.relpath)
    except (OSError, ValueError, EOFError):
        return None


def _open_intent_keys(cfg):
    keys = set()
    for path in sorted(recovery.intents_dir(cfg.reports).glob("*.json")):
        try:
            keys.add(fsutil.read_json(path).get("key"))
        except (OSError, ValueError, AttributeError):
            pass
    return keys


def _volume_row(subject, label, v, fate, info, folder_name):
    s = v.series if v.series and not v.mixed else {}
    sampling = sid_.native_sampling(s)
    acq, group = v.acq, v.group
    candidate = fate["candidate_id"]
    result = info.get(candidate, {})
    tier = result.get("tier")
    g = v.gradients or {}
    dims = v.dims or []
    grid = ""
    if v.series and not v.mixed and s.get("pixel_mm"):
        grid = f"{s['pixel_mm'][1]:.3f}x{s['pixel_mm'][0]:.3f}"
    return {
        "subject_id": subject, "session": label, "volume_id": v.vid, "source": v.relpath, "folder": v.folder,
        "role": v.role or "", "role_rule": v.role_rule, "family": v.family, "kind": v.kind, "part": v.part,
        "echo_te_ms": v.te, "acquisition_id": acq.id if acq else "",
        "duplicate_of": sorted(acq.duplicate_of) if acq else "", "derived_group_id": group.id if group else "",
        "link_state": group.link_state if group else "", "link_rung": group.link_rung if group else "",
        "link_parent": group.parent.id if group and group.parent else "",
        "link_candidates": group.candidates if group else "", "candidate_id": candidate,
        "tier": tier[1]["name"] if tier else "", "eligible": result.get("eligible", ""),
        "exclusion_reasons": result.get("reasons", ""), "rank": result.get("rank", ""), "fate": fate["fate"],
        "selected_path": folder_name, "series_number": v.sidecar.get("SeriesNumber"),
        "image_type": v.image_type, "mr_acq": s.get("mr_acq"), "scan_seq": s.get("scan_seq"),
        "seq_var": s.get("seq_var"), "seq_name": s.get("seq_name"), "tr_ms": s.get("tr_ms"), "te_ms": v.te,
        "ti_ms": s.get("ti_ms"), "flip_deg": s.get("flip_deg"), "orientation": s.get("orientation"),
        "grid_mm": grid, "native_mm": sid_.native_text(sampling), "gap_mm": sampling["gap"],
        # The series' slice count: dcm2niix can rotate a 3D volume, so its slices need not be the third axis.
        "n_slices": s.get("n_positions") or (dims[3] if len(dims) > 3 and dims[0] >= 3 else ""),
        "n_volumes": dims[4] if len(dims) > 4 and dims[0] >= 4 else 1, "extent_mm": s.get("extent_mm"),
        "uniform_spacing": s.get("uniform_spacing"),
        "eq_companion": any(c["kind"] == "eq" for c in v.companions), "b0": g.get("b0"),
        "shells": g.get("shells"), "b_max": g.get("b_max"), "n_dirs": g.get("n_dirs"),
        "pe_dir": v.sidecar.get("PhaseEncodingDirection"),
        "contrast_phase": (acq or group).phase if (acq or group) else "",
        "contrast_evidence": (acq or group).evidence if (acq or group) else "",
        "desc_tokens": s.get("desc_tokens"), "manufacturer": s.get("manufacturer"), "model": s.get("model"),
        "field_t": s.get("field_t"), "software": s.get("software"),
        "variant": [t for t in v.image_type if t in rules.DISTORTION_TOKENS or t == "NORM"],
        "has_acq_matrix": bool(s.get("acq_matrix")), "has_phase_dir": bool(s.get("phase_dir")),
        "has_image_type_text": bool(s.get("has_image_type_text")), "warnings": sorted(set(v.warnings))}


def _new_name(subject, label, folder, v, suffix):
    role = folder.rsplit("/", 1)[-1]
    return f"{folder}/{subject}_{label}_{role}_{v.token}{suffix}"


def _copy_verified(source, destination, expected):
    """Copy a file, hashing it on the way; raise SourceChanged when it differs from the inventory."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    with source.open("rb") as src, destination.open("wb") as out:
        for block in iter(lambda: src.read(4 * 1024 * 1024), b""):
            digest.update(block)
            size += len(block)
            out.write(block)
    if digest.hexdigest() != expected:
        raise SourceChanged(f"{source.name} changed since conversion")
    return size, expected


def _write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data), hashlib.sha256(data).hexdigest()


def _dump(data):
    return (json.dumps(data, sort_keys=True, indent=1, ensure_ascii=True, allow_nan=False) + "\n").encode()


COPY_FATES = {"selected", "selected_derived", "companion", "kept"}


def copied(v, fate, profile):
    """True when a volume goes into the output folder: a listed modality, and its selection or, with
    extras, also its alternatives in OTHER/ (owner decision 2026-11-18: no extras by default)."""
    if v.role is None:
        return profile["extras"] and profile["modalities"] is None
    if not rules.listed(v.role, profile):
        return False
    return fate["fate"] in COPY_FATES or profile["extras"]


def _roles(decisions, profile):
    roles = {}
    for role, d in sorted(decisions.items()):
        if not rules.listed(role, profile):
            continue
        roles[role] = {"outcome": d["outcome"], "decided_by": d["decided_by"], "candidate_id": d["candidate_id"],
                       "reasons": d["reasons"], "warnings": d["warnings"], "override": d["decided_by"] == "override"}
    return roles


def _manifest(subject, label, sessions, session, decisions, fates, conversions, profile, digest_profile):
    roles = _roles(decisions, profile)
    files = []
    for v in session.volumes:
        fate = fates[v]
        if not copied(v, fate, profile):
            continue
        files.append({"path": _new_name(subject, label, fate["folder"], v, ".nii.gz"), "source": v.relpath,
                      "source_sha256": v.sha256, "role": v.role or "", "fate": fate["fate"]})
        for c in v.companions:
            files.append({"path": _new_name(subject, label, fate["folder"], v, _suffix(v, c)),
                          "source": str(Path(v.relpath).parent.as_posix() + "/" + Path(c["name"]).name),
                          "source_sha256": c["sha256"], "role": v.role or "", "fate": fate["fate"]})
    return {"profile": profile["name"], "profile_version": profile["version"], "profile_sha256": digest_profile,
            "selector_version": SELECTOR_VERSION, "subject_id": subject, "session": label,
            "sessions": [{"session": s, "conversion_receipt_id": c.get("receipt_id")}
                         for s, c in zip(sessions, conversions)],
            "roles": roles, "files": sorted(files, key=lambda f: f["path"])}


def _suffix(v, companion):
    stem = Path(v.relpath).name[:-7]
    return Path(companion["name"]).name[len(stem):]


def _bids_manifest(subject, label, sessions, plan, decisions, conversions, profile, digest_profile, agent):
    """What a BIDS visit folder holds; its digest is the visit's fingerprint (BIDS design 5.3)."""
    files, omitted = [], []
    for e in plan:
        v = e["volume"]
        if e["status"] == "omitted":
            omitted.append({"source": v.relpath, "reason": e["reason"]})
            continue
        stem, folder = e["path"][:-7], Path(v.relpath).parent.as_posix()
        files.append({"path": e["path"], "source": v.relpath, "source_sha256": v.sha256, "role": v.role,
                      "fate": e["fate"], "candidate_id": e["candidate_id"], "status": e["status"]})
        files.append({"path": stem + ".json", "source": v.relpath[:-7] + ".json", "source_sha256": v.json_sha256,
                      "added": e["extra"]})
        for c in v.companions:
            source = f"{folder}/{Path(c['name']).name}"
            if e["suffix"] == "dwi" and c["kind"] in ("bval", "bvec"):
                files.append({"path": f"{stem}.{c['kind']}", "source": source, "source_sha256": c["sha256"]})
            else:
                omitted.append({"source": source, "reason": "companion_not_exported"})
    return {"layout": "bids", "exporter_revision": bids.EXPORTER_REVISION, "bids_version": bids.BIDS_VERSION,
            "contrast_agent": agent, "profile": profile["name"], "profile_version": profile["version"],
            "profile_sha256": digest_profile, "selector_version": SELECTOR_VERSION, "subject_id": subject,
            "session": label, "sessions": [{"session": s, "conversion_receipt_id": c.get("receipt_id")}
                                           for s, c in zip(sessions, conversions)],
            "roles": _roles(decisions, profile), "files": sorted(files, key=lambda f: f["path"]),
            "omitted": sorted(omitted, key=lambda f: f["source"])}


def _default_outputs(cfg, subject, label, chosen, fates, profile):
    outputs = []
    for v in chosen:
        fate = fates[v]
        source = cfg.niftis / v.relpath
        nifti_folder = cfg.niftis / Path(*Path(v.relpath).parts[:3])  # Subjects/<id>/<its own session>
        outputs.append(("copy", _new_name(subject, label, fate["folder"], v, ".nii.gz"), source, v.sha256))
        for c in v.companions:
            outputs.append(("copy", _new_name(subject, label, fate["folder"], v, _suffix(v, c)),
                            nifti_folder / c["name"], c["sha256"]))
        added = {"selection_role": v.role or "", "selection_fate": fate["fate"],
                 "selection_candidate_id": fate["candidate_id"], "selection_profile": profile["name"],
                 "source_nifti": v.relpath}
        outputs.append(("sidecar", _new_name(subject, label, fate["folder"], v, ".json"),
                        source.with_name(source.name[:-7] + ".json"), v.json_sha256,
                        lambda raw, added=added: dict(json.loads(raw), **added)))
    return outputs


def _bids_outputs(cfg, subject, label, plan):
    outputs = []
    for e in plan:
        if e["status"] == "omitted":
            continue
        v = e["volume"]
        source = cfg.niftis / v.relpath
        nifti_folder = cfg.niftis / Path(*Path(v.relpath).parts[:3])
        stem = e["path"][:-7]
        outputs.append(("copy", e["path"], source, v.sha256))
        outputs.append(("sidecar", stem + ".json", source.with_name(source.name[:-7] + ".json"), v.json_sha256,
                        lambda raw, extra=e["extra"]: bids.sidecar(raw, extra)))
        if e["suffix"] == "dwi":
            for c in v.companions:
                if c["kind"] in ("bval", "bvec"):
                    outputs.append(("copy", f"{stem}.{c['kind']}", nifti_folder / c["name"], c["sha256"]))
    scans = f"sub-{subject}_ses-{label}_scans"
    outputs.append(("bytes", scans + ".tsv", bids.scans_tsv(plan)))
    outputs.append(("bytes", scans + ".json", _dump(bids.SCANS_JSON)))
    return outputs


def publish_session(cfg, subject, label, sessions, session, decisions, fates, conversions, profile, digest_profile,
                    logger, plan=None, agent=None):
    """Build or update one visit in the output root (7.5); sessions and conversions are its sessions'.

    With a BIDS plan (bids.plan_visit) the visit becomes sub-<id>/ses-<date>/ instead.
    Returns 'published', 'unchanged', 'skipped: ...' (nothing to copy) or 'refused: ...'.
    """
    if plan is None:
        destination = cfg.output / "Subjects" / subject / label
        chosen = [v for v in session.volumes if copied(v, fates[v], profile)]
        if not chosen:
            return "skipped: nothing selected in the listed modalities"
        manifest = _dump(_manifest(subject, label, sessions, session, decisions, fates, conversions, profile,
                                   digest_profile))
        outputs = _default_outputs(cfg, subject, label, chosen, fates, profile)
        outputs.append(("bytes", "selection.json", manifest))
        layout = {}
    else:
        destination = cfg.output / f"sub-{subject}" / f"ses-{label}"
        if all(e["status"] == "omitted" for e in plan):
            return "skipped: nothing exportable in the listed modalities"
        manifest = _dump(_bids_manifest(subject, label, sessions, plan, decisions, conversions, profile,
                                        digest_profile, agent))
        outputs = _bids_outputs(cfg, subject, label, plan)
        outputs.append(("bytes", bids.MANIFEST, manifest))
        # Crash recovery restores this exact folder (cleanup_output).
        layout = {"layout": "bids", "destination": destination.relative_to(cfg.output).as_posix()}
    digest = hashlib.sha256(manifest).hexdigest()
    if destination.exists():
        try:
            receipt = fsutil.read_json(destination / receipts.RECEIPT)
            listed = fsutil.read_json(destination / receipts.INVENTORY)["files"]
        except (OSError, ValueError, KeyError):
            return "refused: folder not owned by this pipeline (no readable receipt)"
        intact = all((destination / item["name"]).is_file()
                     and (destination / item["name"]).stat().st_size == item["size"] for item in listed)
        if receipt.get("selection_sha256") == digest and intact:
            return "unchanged"
        extra = convert.files_outside_inventory(destination)
        if extra:
            return "refused: files not in its inventory: " + ", ".join(extra[:20])
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.parent / (".staging_" + uuid.uuid4().hex[:12])
    stage.mkdir()
    try:
        files = []
        for kind, name, *item in outputs:
            if kind == "copy":
                files.append((name, *_copy_verified(item[0], stage / name, item[1])))
            elif kind == "sidecar":
                raw = item[0].read_bytes()
                if hashlib.sha256(raw).hexdigest() != item[1]:
                    raise SourceChanged(f"{item[0].name} changed since conversion")
                files.append((name, *_write_bytes(stage / name, _dump(item[2](raw)))))
            else:
                files.append((name, *_write_bytes(stage / name, item[0])))
        inventory = [{"name": n, "size": size, "sha256": sha} for n, size, sha in sorted(files)]
        fsutil.atomic_json(stage / receipts.INVENTORY, {"files": inventory})
        fsutil.atomic_json(stage / receipts.RECEIPT, {
            "version": config.SCHEMA_VERSION, "complete": True, "receipt_id": uuid.uuid4().hex, "run_id": cfg.run_id,
            "kind": "select", "subject_id": subject, "session": label, "sessions": sessions,
            "selection_sha256": digest, "source_receipt_ids": [c.get("receipt_id") for c in conversions],
            "provenance": {"package_version": __version__, "selector_version": SELECTOR_VERSION,
                           "profile": profile["name"], "profile_version": profile["version"],
                           "profile_sha256": digest_profile}, **layout})
        incoming, previous = fsutil.swap_names(destination)
        if not fsutil.publish_tree(stage, destination, cfg.output, incoming, previous):
            logger.warning("Published %s/%s; the previous folder %s could not be deleted yet, the next "
                           "--publish run retries", subject, label, previous)
        return "published"
    except SourceChanged as e:
        return f"refused: source_changed ({e})"
    except OSError as e:
        return f"refused: {type(e).__name__}: {e}"
    finally:
        if stage.exists():
            fsutil.remove_tree(stage, cfg.output)


def cleanup_output(cfg, logger):
    """Finish or undo publications a crash interrupted (7.5, rule 6), in either layout."""
    subjects = cfg.output / "Subjects"
    folders = sorted(p for p in subjects.iterdir() if p.is_dir()) if subjects.is_dir() else []
    folders += sorted(p for p in cfg.output.glob("sub-*") if p.is_dir()) if cfg.output.is_dir() else []
    for folder in folders:
        for p in sorted(folder.iterdir()):
            try:
                if p.name.startswith((".staging_", ".incoming_")):
                    fsutil.remove_tree(p, cfg.output)
                elif p.name.startswith(".previous_"):
                    try:
                        receipt = fsutil.read_json(p / receipts.RECEIPT)
                        # A BIDS receipt records its folder; a Selected/ visit folder is named by its date.
                        target = cfg.output / receipt["destination"] if receipt.get("destination") \
                            else folder / receipt["session"]
                    except (OSError, ValueError, KeyError, TypeError, AttributeError):
                        logger.warning("Left %s in place: it has no readable selection receipt", p)
                        continue
                    if target.exists():
                        fsutil.remove_tree(p, cfg.output)
                    else:
                        fsutil.retry_locked(lambda: p.rename(target))
                        logger.info("RECONCILED restored %s from an interrupted publication", target)
            except OSError as e:
                logger.warning("Could not clean up %s: %s", p, e)


def _role_rows(subject, label, sessions, decisions, fates, prov, profile):
    rows = []
    counts = {}
    for v, fate in fates.items():
        if fate["fate"] in COPY_FATES:
            counts[fate["folder"]] = counts.get(fate["folder"], 0) + 1
    for role in rules.REPORT_ROLES:
        if not rules.listed(role, profile):
            continue
        d = decisions[role]
        facts = rules.role_facts(d)
        row = {"subject_id": subject, "session": label, "sessions": sessions, "role": role, "outcome": d["outcome"],
               "decided_by": d["decided_by"], "candidate_id": d["candidate_id"], "n_files": counts.get(role, 0),
               "rule_recommendation": d["rule_recommendation"], "runner_up": d["runner_up"],
               "n_candidates": d["n_candidates"], "n_eligible": d["n_eligible"], "reasons": d["reasons"],
               "warnings": d["warnings"], **{k: v for k, v in facts.items() if k != "native_max_mm"}, **prov}
        row["_change"] = {k: facts.get(k) for k in CHANGE_FIELDS} | {"native_max_mm": facts.get("native_max_mm")} \
            if facts else None
        rows.append(row)
    return rows


def _session_rows(subject, label, sessions, outcome, reason, prov, profile):
    return [{"subject_id": subject, "session": label, "sessions": sessions, "role": role, "outcome": outcome,
             "reasons": [reason], "_change": None, **prov} for role in rules.REPORT_ROLES if rules.listed(role, profile)]


def _protocol_changes(rows):
    """protocol_change against the same role's winner in the subject's previous evaluated session (7.4)."""
    last = {}
    for row in sorted(rows, key=lambda r: (r["subject_id"], r["session"], r["role"])):
        current = row.get("_change")
        key = (row["subject_id"], row["role"])
        if current:
            previous = last.get(key)
            if previous:
                changed = [k for k in CHANGE_FIELDS if previous.get(k) != current.get(k)]
                a, b = previous.get("native_max_mm"), current.get("native_max_mm")
                if a is not None and b is not None and abs(a - b) > 0.2:
                    changed.append("native_max_mm")
                row["protocol_change"] = changed
            last[key] = current


def run(cfg, ids, pairs, roster_warnings, stopper):
    logger = reports.setup_log(cfg.reports, "select", cfg.dry_run)
    started = time.monotonic()
    summary = {"run_id": cfg.run_id, "stage": "select", "outcome": "aborted", "reconciled": 0, "notes": ""}
    try:
        return _run(cfg, roster_warnings, stopper, logger, summary)
    finally:
        if not cfg.dry_run:
            summary["seconds"] = round(time.monotonic() - started, 2)
            reports.append_history(cfg, summary, logger)


def _run(cfg, roster_warnings, stopper, logger, summary):
    logger.info("RUN %s START stage=select dry_run=%s publish=%s output=%s", cfg.run_id, cfg.dry_run, cfg.publish,
                cfg.output)
    for warning in roster_warnings:
        logger.warning(warning)
    data = None
    if cfg.profile:
        try:
            data = json.loads(Path(cfg.profile).read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Profile {cfg.profile} is not valid JSON: {e}") from None
    profile = rules.load_profile(data, options_from(cfg))
    digest_profile = rules.profile_sha256(profile)
    prov = {"profile": profile["name"], "profile_version": profile["version"], "profile_sha256": digest_profile,
            "selector_version": SELECTOR_VERSION}
    subjects = load_subjects(cfg.subjects, cfg.subject_column) if cfg.subjects else None
    overrides = load_overrides(cfg.overrides)
    state = State(cfg.reports, cfg.run_id)
    if state.recovered_from_backup:
        logger.error("State index %s could not be read; using %s and its journals (read only)", state.path,
                     state.backup)
    refuse_moved_project(cfg, state)  # before bids_root, which writes the BIDS marker
    agent = bids_root(cfg, profile, logger)
    if cfg.publish and not cfg.dry_run:
        cleanup_output(cfg, logger)
    blocked, intent_keys = recovery.blocked_keys(state), _open_intent_keys(cfg)
    stop = stopper.event
    role_rows, volume_rows = [], []
    counts = {"evaluated": 0, "cached": 0, "not_ready": 0, "excluded": 0, "incomplete": 0, "published": 0,
              "unchanged": 0, "skipped": 0, "refused": 0}
    blocking_not_ready, review = 0, 0
    seen = set()
    visits = {}
    for key, entry in sorted(state.data["sessions"].items()):
        subject, label = entry.get("subject_id"), entry.get("label")
        if subject and label:
            visits.setdefault((subject, entry.get("date") or label.split("_", 1)[0]), []).append((key, entry))
    for (subject, label), members in sorted(visits.items()):
        if stop.is_set():
            break
        if subjects is not None and subject not in subjects:
            continue
        members.sort(key=lambda m: session_order(m[1]["label"]))
        if cfg.sessions is not None and not any(k in cfg.sessions for k, _ in members):
            continue
        seen.add(subject)
        sessions = [entry["label"] for _, entry in members]
        key = f"{subject}/{label}" + (f" (sessions {', '.join(sessions)})" if len(sessions) > 1 else "")
        # Every session of the visit must be ready; the visit waits for the last one (7.2).
        problems, parts = [], []
        try:
            for member_key, entry in members:
                reason, conversion = readiness(entry, cfg, blocked, intent_keys, profile)
                if not reason:
                    try:
                        parts.append((conversion, *meta.session_metadata(entry, conversion, cfg, stop)))
                    except InterruptedError:
                        raise
                    except Exception as e:  # one unreadable session never stops the run (7.6)
                        reason = f"metadata_read_failed:{type(e).__name__}"
                        logger.error("Could not read %s for selection: %s: %s", member_key, type(e).__name__, e)
                if reason:
                    problems.append((entry["label"], reason))
        except InterruptedError:
            break
        if problems:
            counts["not_ready"] += 1
            blocking_not_ready += any(r != "not_converted" for _, r in problems)
            reason = problems[0][1] if len(sessions) == 1 else "; ".join(f"{s}: {r}" for s, r in problems)
            logger.warning("Not evaluated %s: %s", key, reason)
            role_rows += _session_rows(subject, label, sessions, "source_not_ready", reason, prov, profile)
            continue
        conversions = [conversion for conversion, _, _, _ in parts]
        series = visit_series([s for _, s, _, _ in parts])
        records = [r for _, _, volume_records, _ in parts for r in volume_records]
        counts["evaluated"] += 1
        counts["cached"] += all(hit for _, _, _, hit in parts)
        scanner = rules.session_requirement(list(series.values()), profile)
        if scanner:
            outcome, reason = scanner
            counts["excluded"] += outcome == "session_excluded"
            review += sum(1 for r in rules.REPORT_ROLES if rules.listed(r, profile)) if outcome == "review_required" else 0
            logger.warning("%s %s: %s", "Excluded" if outcome == "session_excluded" else "Review", key, reason)
            role_rows += _session_rows(subject, label, sessions, outcome, reason, prov, profile)
            continue
        volumes = [sid_.Volume(relpath=r["relpath"], sha256=r["sha256"], folder=r["folder"], token=r["token"],
                               companions=r["companions"], sidecar=r["sidecar"], dims=r["dims"],
                               gradients=r["gradients"], series=series.get(r["sidecar"].get("source_series_key")),
                               json_sha256=r["json_sha256"])
                   for r in records]
        session = sid_.analyze(series, volumes, profile["roles"]["DWI"].get("min_tensor_directions", 6),
                               profile["contrast_unknown"])
        decisions, fates, info = rules.decide_session(
            session, profile, overrides.get((subject, label)),
            accepted_partial=any(conversion.get("accepted_errors") for conversion in conversions))
        review += sum(d["outcome"] == "review_required" for r, d in decisions.items() if rules.listed(r, profile))
        chosen = [v for v in session.volumes if copied(v, fates[v], profile)]
        plan, plan_error = None, ""
        if cfg.bids:
            try:
                plan = bids.plan_visit(subject, label, chosen, fates, decisions, agent, lambda v: _grid(cfg, v))
            except bids.PlanError as e:
                plan_error = str(e)
        missing = {r: decisions[r]["outcome"] for r in rules.incomplete_roles(decisions, profile)} \
            if profile["complete_only"] else {}
        if profile["complete_only"] and plan is not None:
            missing.update((r, "not_exported") for r in bids.unexported_roles(plan, decisions, profile)
                           if r not in missing)
        if missing:
            for d in decisions.values():
                d["warnings"] = sorted(set(d["warnings"]) | {"session_incomplete"})
        role_rows += _role_rows(subject, label, sessions, decisions, fates, prov, profile)
        entries = {id(e["volume"]): e for e in plan or ()}
        chosen_ids = {id(v) for v in chosen}
        planned = []  # rows whose file is in the plan: published only once the visit is in the root
        for v in session.volumes:
            fate = fates[v]
            if v.role is None and profile["modalities"] is not None or v.role and not rules.listed(v.role, profile):
                continue
            if cfg.bids:
                e = entries.get(id(v))
                row = _volume_row(subject, label, v, fate, info, e["path"] if e else "")
                if id(v) in chosen_ids:
                    row.update(export_status=e["status"] if e else "omitted",
                               export_reason=e["reason"] if e else "plan_refused")
                    if row["export_status"] != "omitted":
                        row["published"] = False
                        planned.append(row)
            else:
                path = _new_name(subject, label, fate["folder"], v, ".nii.gz") if id(v) in chosen_ids else ""
                row = _volume_row(subject, label, v, fate, info, path)
            volume_rows.append(row)
        outcome_text = ", ".join(f"{role}={decisions[role]['outcome']}" for role in rules.REPORT_ROLES
                                 if decisions[role]["outcome"] != "missing" and rules.listed(role, profile))
        logger.info("SELECTED %s: %s", key, outcome_text or "no volumes")
        if plan_error:
            counts["refused"] += 1
            logger.error("NOT EXPORTED %s: %s", key, plan_error)
        elif missing:
            counts["incomplete"] += 1
            logger.info("NOT PUBLISHED %s: incomplete (%s)", key, ", ".join(f"{r}={o}" for r, o in missing.items()))
        elif cfg.publish and not cfg.dry_run:
            result = publish_session(cfg, subject, label, sessions, session, decisions, fates, conversions, profile,
                                     digest_profile, logger, plan, agent)
            counts[result.split(":", 1)[0]] += 1
            (logger.error if result.startswith("refused") else logger.info)("PUBLISH %s: %s", key, result)
            for row in planned if result in ("published", "unchanged") else ():
                row["published"] = True
    for key in sorted(overrides):
        if key not in visits:
            logger.warning("Overrides name a session that is not in state: %s/%s (sessions on one date share the "
                           "date as their session)", *key)
    if subjects is not None:
        for subject in sorted(subjects - seen):
            logger.warning("Listed subject %s has no prepared session", subject)
    if cfg.bids and cfg.publish and not cfg.dry_run:
        try:
            bids.write_dataset_files(cfg.output, cfg.project_root.name, agent, logger)
        except OSError as e:
            logger.error("Could not update the BIDS dataset files in %s: %s", cfg.output, e)
            counts["refused"] += 1
    _protocol_changes(role_rows)
    for row in role_rows:
        row.pop("_change", None)
    if not cfg.dry_run:
        cfg.output.mkdir(parents=True, exist_ok=True)
        for name, rows, fields in (("selection.csv", role_rows, SELECTION_FIELDS),
                                   ("selection_volumes.csv", volume_rows, VOLUME_FIELDS)):
            ordered = sorted(rows, key=lambda r: tuple(str(r.get(k, "")) for k in
                                                       ("subject_id", "session", "role", "source")))
            try:
                reports.write_csv(cfg.output / name, [{k: fmt(r.get(k)) for k in fields} for r in ordered], fields)
            except OSError as e:
                logger.error("Could not write %s (is it open in another program?): %s", cfg.output / name, e)
                counts["refused"] += 1
    terminated, interrupted = stopper.signum == signal.SIGTERM, stopper.signum is not None
    issues = review or blocking_not_ready or counts["refused"]
    outcome = "terminated" if terminated else "interrupted" if interrupted else "issues" if issues else "ok"
    summary.update(outcome=outcome, completed=counts["evaluated"], cached=counts["cached"],
                   held=counts["not_ready"], unresolved=review,
                   notes=" ".join(f"{k}={counts[k]}" for k in ("excluded", "incomplete", "published", "unchanged",
                                                               "skipped", "refused")))
    logger.info("RUN %s END outcome=%s evaluated=%s cached=%s not_evaluated=%s scanner_excluded=%s incomplete=%s "
                "review_required=%s published=%s unchanged=%s skipped=%s refused=%s", cfg.run_id, outcome,
                counts["evaluated"], counts["cached"], counts["not_ready"], counts["excluded"], counts["incomplete"],
                review, counts["published"], counts["unchanged"], counts["skipped"], counts["refused"])
    if not cfg.dry_run:
        logger.info("Reports: %s and %s", cfg.output / "selection.csv", cfg.output / "selection_volumes.csv")
    return 143 if terminated else 130 if interrupted else 2 if issues else 0
