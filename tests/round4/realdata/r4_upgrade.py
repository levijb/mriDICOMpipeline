"""R4-OPS-02 (plan item 51): compares the publications and the selection cache after the upgrade runs with their
state before the fixes. Read-only toward the project and the output roots; refuses output folders inside the
project; prints counts only.

    python tests/round4/realdata/r4_upgrade.py before --project D:\\mri_project --private D:\\round4\\private \\
        --output-root D:\\round4\\Selected --output-root D:\\round4\\BIDS --evidence D:\\round4\\evidence\\realdata

    python tests/round4/realdata/r4_upgrade.py compare --run UB --project D:\\mri_project --private D:\\round4\\private \\
        --output-root D:\\round4\\Selected --evidence D:\\round4\\evidence\\realdata \\
        [--mismatches D:\\round4\\evidence\\realdata\\mismatches.csv] [--verify-hashes]

before: for each output root, every published visit folder (its receipt, its selection manifest's hash and
content, its inventory, and each file's size and modification time), the root's other files, the project's
selection cache (each file's hash and time, and its series records) and whether selection_overrides.csv exists.
Writes <private>/upgrade/baseline.json (subject IDs and folder names: local only; never overwritten without
--force) and <evidence>/upgrade_baseline_summary.json (counts).

compare: takes the same record now and compares it with the baseline.
*   Visits, classed by receipt and manifest (scan-selection.md 7.5 rule 5: a visit is replaced when its manifest
    hash changes or a listed file is missing):
    *   kept: same receipt ID, every file present with the same size and modification time;
    *   kept_but_files_changed: same receipt ID, but a file was added, removed or touched;
    *   replaced_restored: new receipt ID after a listed file was missing before;
    *   replaced_expected: new receipt ID, explained (the owner's reading of R4-OPS-02, 2026-11-25) by a changed
        decision (the manifest's roles or files differ), by an R4-F-023/024 fact that changed in the session's
        cache (pos_min, pos_max, step_mm, extent_mm, orientation), or by the version alone (only selector_version,
        or a BIDS manifest's exporter_revision, differs); where the decision did not change, the image files
        (NIfTI, bval, bvec) hold the same bytes before and after (their inventory SHA-256 values);
    *   replaced_images_changed: as above without a decision change, but an image file's bytes differ;
    *   replaced_unexplained: new receipt ID with none of those explanations;
    *   new, removed, no_receipt.
    Each visit's published roles are also checked against the run's own report (selection.csv in the output
    root): a kept visit whose current decision differs is kept_despite_decision_change. Replaced and new visits
    are checked against their inventory (size; SHA-256 too with --verify-hashes). Leftover .staging_, .incoming_
    or .previous_ folders are listed.
*   Cache: which cache files were rewritten, and per series which facts changed. Each changed fact, and each
    pre-fix mismatch listed in --mismatches (R4-VAL-01's mismatches.csv), is compared again with the header
    reader's record for that series (the frozen reader's own comparison; the records saved by r4_facts.py in
    <private>/reader/), so a fix that should change a fact shows whether the rewritten cache now matches. The
    owner's reading R4-F-025 applies: an absent value, null in the reader and "" in the cache, counts as equal.
*   Overrides: whether selection_overrides.csv exists, before and now, and the report's override warnings.
Writes <private>/upgrade/after_<run>.json and, coded, <evidence>/upgrade_<run>_visits.csv,
upgrade_<run>_cache.csv and upgrade_<run>_summary.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter

import r4real

MANIFESTS = ("selection.json", ".selection.json")
RECEIPT = ".pipeline_complete.json"
INVENTORY = ".inventory.json"
DECISION_KEYS = ("roles", "files")
LEFTOVER = (".staging_", ".incoming_", ".previous_")
NOT_RECHECKED = {"software"}  # r4_facts.py compares it with the DICOM's value order, which the saved records lack
# The owner's reading of R4-OPS-02 (2026-11-25): a replaced visit is expected when a changed decision, a fact changed
# by R4-F-023 or R4-F-024, or the version alone explains it; where the decision did not change, the image files must
# be byte-identical before and after. Anything else is a failure.
FACT_FIELDS = {"pos_min", "pos_max", "step_mm", "extent_mm", "orientation"}  # the facts R4-F-023 and R4-F-024 change
VERSION_FIELDS = {"selector_version", "exporter_revision"}  # manifest fields that only carry a version
IMAGE_SUFFIXES = (".nii.gz", ".nii", ".bval", ".bvec")
FAIL = {"kept_but_files_changed", "kept_despite_decision_change", "replaced_unexplained", "replaced_images_changed",
        "replaced_differs_from_report", "removed", "no_receipt", "inventory_mismatch"}


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def rel(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def layout_of(root):
    if os.path.isdir(os.path.join(root, "Subjects")):
        return "default"
    if os.path.exists(os.path.join(root, "dataset_description.json")):
        return "bids"
    raise SystemExit(f"not an output root of select: {os.path.basename(root)}")


def visit_folders(root, layout):
    def dirs(p):
        return sorted(d for d in os.listdir(p) if os.path.isdir(os.path.join(p, d)))
    if layout == "default":
        base = os.path.join(root, "Subjects")
        return [os.path.join(base, s, v) for s in dirs(base) for v in dirs(os.path.join(base, s)) if not v.startswith(".")]
    return [os.path.join(root, s, v) for s in dirs(root) if s.startswith("sub-")
            for v in dirs(os.path.join(root, s)) if v.startswith("ses-")]


def files_of(folder):
    out = {}
    for dirpath, _dirs, names in os.walk(folder):
        for name in names:
            st = os.stat(os.path.join(dirpath, name))
            out[rel(os.path.join(dirpath, name), folder)] = [st.st_size, st.st_mtime_ns]
    return out


def visit_record(folder, root):
    receipt = load_json(os.path.join(folder, RECEIPT)) or {}
    name = next((m for m in MANIFESTS if os.path.exists(os.path.join(folder, m))), None)
    manifest = (load_json(os.path.join(folder, name)) if name else None) or {}
    inventory = (load_json(os.path.join(folder, INVENTORY)) or {}).get("files", [])
    meta = {k: v for k, v in manifest.items() if k not in DECISION_KEYS}
    return {"folder": rel(folder, root),
            "subject_id": receipt.get("subject_id") or manifest.get("subject_id"),
            "session": receipt.get("session") or manifest.get("session"),
            "receipt_id": receipt.get("receipt_id"), "selection_sha256": receipt.get("selection_sha256"),
            "manifest": name, "manifest_sha256": sha_file(os.path.join(folder, name)) if name else None,
            "decision_digest": digest({k: manifest.get(k) for k in DECISION_KEYS}) if name else None,
            "meta": meta,
            "roles": {k: [r.get("outcome"), r.get("candidate_id") or ""] for k, r in (manifest.get("roles") or {}).items()},
            "inventory": {i["name"]: [i.get("size"), i.get("sha256")] for i in inventory},
            "files": files_of(folder)}


def other_files(root, visits):
    skip = {os.path.normcase(os.path.abspath(v)) for v in visits}
    out = {}
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if os.path.normcase(os.path.abspath(os.path.join(dirpath, d))) not in skip]
        for name in names:
            p = os.path.join(dirpath, name)
            st = os.stat(p)
            out[rel(p, root)] = [st.st_size, st.st_mtime_ns, sha_file(p) if st.st_size < 50_000_000 else None]
    return out


def cache_state(project):
    root = os.path.join(project, "Reports", ".selection_cache")
    out = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            p = os.path.join(dirpath, name)
            st = os.stat(p)
            doc = load_json(p) or {}
            out[rel(p, root)] = {"sha256": sha_file(p), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                                 "cache_version": doc.get("cache_version"), "series": doc.get("series") or {}}
    return out


def overrides_state(project):
    p = os.path.join(project, "selection_overrides.csv")
    if not os.path.exists(p):
        return None
    st = os.stat(p)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha_file(p)}


def snapshot(project, roots):
    state = {"taken_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "overrides": overrides_state(project),
             "cache": cache_state(project), "roots": {}}
    for root in roots:
        layout = layout_of(root)
        folders = visit_folders(root, layout)
        visits = {}
        for folder in folders:
            rec = visit_record(folder, root)
            visits[f"{rec['subject_id']}/{rec['session']}" if rec["subject_id"] else "?/" + rec["folder"]] = rec
        state["roots"][os.path.normcase(os.path.abspath(root))] = {"layout": layout, "visits": visits,
                                                                    "other_files": other_files(root, folders)}
    return state


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, sort_keys=True)


def before(a, codes):
    path = os.path.join(a.private, "upgrade", "baseline.json")
    if os.path.exists(path) and not a.force:
        raise SystemExit("baseline.json exists; it is the pre-fix record and is not overwritten without --force")
    state = snapshot(a.project, a.output_root)
    write_json(path, state)
    summary = {"taken_utc": state["taken_utc"], "overrides_file": state["overrides"] is not None,
               "cache_files": len(state["cache"]),
               "cache_versions": dict(Counter(str(c["cache_version"]) for c in state["cache"].values())),
               "roots": {}}
    for root, r in state["roots"].items():
        vs = r["visits"].values()
        summary["roots"][r["layout"]] = {
            "visits": len(r["visits"]), "files": sum(len(v["files"]) for v in vs),
            "bytes": sum(s for v in vs for s, _t in v["files"].values()),
            "visits_with_receipt": sum(1 for v in vs if v["receipt_id"]),
            "visit_codes_unknown": sum(1 for v in vs if codes.session(v["subject_id"], v["session"]) == "?"),
            "selector_versions": dict(Counter(str(v["meta"].get("selector_version")) for v in vs)),
            "listed_files_missing_or_resized": sum(1 for v in vs for n, (size, _h) in v["inventory"].items()
                                                   if v["files"].get(n, [None])[0] != size),
            "files_outside_inventory": sum(1 for v in vs for n in v["files"]
                                           if n not in v["inventory"] and n not in (RECEIPT, INVENTORY, v["manifest"])),
            "other_files": len(r["other_files"])}
    with open(os.path.join(a.evidence, "upgrade_baseline_summary.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, indent=1, sort_keys=True)
    print(json.dumps(summary, indent=1, sort_keys=True))


def report_roles(root):
    path = os.path.join(root, "selection.csv")
    out = {}
    if os.path.exists(path):
        for r in r4real.read_csv(path):
            out.setdefault((r["subject_id"], r["session"]), {})[r["role"]] = [r["outcome"], r["candidate_id"] or ""]
    return out


def roles_from_report(report, rec):
    sid, label = rec["subject_id"], rec["session"] or ""
    return report.get((sid, label)) or report.get((sid, label[:8]))


def image_hashes(rec):
    return sorted(h for name, (_size, h) in rec["inventory"].items() if name.lower().endswith(IMAGE_SUFFIXES))


def classify(b, n, report, root, verify, facts_changed=frozenset()):
    """(class, info) for one visit. facts_changed: the R4-F-023/024 fact fields whose cached values changed for any
    series of the visit's session."""
    info = {}
    if n is None:
        return "removed", {}
    if b is not None:
        info["roles_changed"] = sorted(k for k in set(b["roles"]) | set(n["roles"]) if b["roles"].get(k) != n["roles"].get(k))
        info["meta_changed"] = sorted(k for k in set(b["meta"]) | set(n["meta"]) if b["meta"].get(k) != n["meta"].get(k))
        same = [f for f in n["files"] if b["files"].get(f) == n["files"][f]]
        info["files_same"] = len(same)
        info["files_changed"] = len(set(n["files"]) | set(b["files"])) - len(same)
    if b is None:
        cls = "new"
    elif not n["receipt_id"]:
        return "no_receipt", info
    elif n["receipt_id"] == b["receipt_id"]:
        cls = "kept" if n["files"] == b["files"] else "kept_but_files_changed"
    elif any(b["files"].get(name, [None])[0] != size for name, (size, _h) in b["inventory"].items()):
        cls = "replaced_restored"
    else:
        explained = []
        decision = n["decision_digest"] != b["decision_digest"]
        if decision:
            explained.append("decision")
        if facts_changed:
            explained.append("facts R4-F-023/024 (" + ",".join(sorted(facts_changed)) + ")")
        if info["meta_changed"] and set(info["meta_changed"]) <= VERSION_FIELDS:
            explained.append("version (" + ",".join(info["meta_changed"]) + ")")
        info["explained_by"] = explained
        if not explained:
            cls = "replaced_unexplained"
        elif not decision and image_hashes(n) != image_hashes(b):
            cls = "replaced_images_changed"
        else:
            cls = "replaced_expected"
        if not decision:
            info["images_identical"] = image_hashes(n) == image_hashes(b)
    current = roles_from_report(report, n)
    if current is not None:
        common = set(current) & set(n["roles"])
        info["differs_from_report"] = sorted(k for k in common if current[k] != n["roles"][k])
        if info["differs_from_report"]:
            cls = "kept_despite_decision_change" if cls.startswith("kept") else "replaced_differs_from_report"
    if cls.startswith("replaced") or cls == "new":
        folder = os.path.join(root, *n["folder"].split("/"))
        bad = []
        for name, (size, sha) in n["inventory"].items():
            got = n["files"].get(name)
            if got is None or got[0] != size or (verify and sha_file(os.path.join(folder, *name.split("/"))) != sha):
                bad.append(name)
        info["inventory_problems"] = len(bad)
        if bad:
            cls = "inventory_mismatch"
    return cls, info


def compare_cache(a, base, now, codes):
    from r4_facts import load_reader

    reader = load_reader()
    prefix = {}
    if a.mismatches:
        for r in r4real.read_csv(a.mismatches):
            if r["field"] not in ("<series>", "volume_dims"):
                prefix.setdefault(r["session"], []).append((r["series"], r["field"]))
    rows, rewritten, records = [], 0, {}
    for name in sorted(set(base) | set(now)):
        b, n = base.get(name), now.get(name)
        sid, label = name.split("/", 1)
        label = label[:-5] if label.endswith(".json") else label
        code = codes.session(sid, label)
        if b is None or n is None:
            rows.append({"session": code, "series": "", "field": "<cache file>", "change": "new" if b is None else "removed",
                         "after_vs_reader": ""})
            continue
        if b["sha256"] != n["sha256"]:
            rewritten += 1
        fields = {}
        for key in set(b["series"]) | set(n["series"]):
            bs, ns = b["series"].get(key) or {}, n["series"].get(key) or {}
            for f in set(bs) | set(ns):
                if bs.get(f) != ns.get(f):
                    fields.setdefault(key, set()).add(f)
        for series8, f in prefix.get(code, []):
            for key in n["series"]:
                if key.startswith(series8):
                    fields.setdefault(key, set()).add(f)
        if not fields:
            continue
        if code not in records:
            records[code] = (load_json(os.path.join(a.private, "reader", f"{code}.json")) or {}).get("series", {})
        for key, fs in sorted(fields.items()):
            m, t = records[code].get(key), dict(n["series"].get(key) or {})
            if t.get("echoes") == []:
                t["echoes"] = None
            results = {}
            if m is not None and n["series"].get(key) is not None:
                for item in reader.compare_series(m, t):
                    result = item["result"]
                    # Owner reading R4-F-025 (accepted 2026-11-25): an absent value, null in the reader and "" in
                    # select's cache, counts as equal.
                    if result == "mismatch" and item["reader"] is None and item["select"] == "":
                        result = "equal (R4-F-025 reading)"
                    results[item["field"]] = result
            for f in sorted(fs):
                changed = (b["series"].get(key) or {}).get(f) != (n["series"].get(key) or {}).get(f)
                result = "not rechecked" if f in NOT_RECHECKED else results.get(f, "no reader record" if m is None else "not compared")
                rows.append({"session": code, "series": key[:8], "field": f,
                             "change": "changed" if changed else "unchanged (pre-fix mismatch)", "after_vs_reader": result})
    return rows, rewritten


def compare(a, codes):
    base = load_json(os.path.join(a.private, "upgrade", "baseline.json"))
    if base is None:
        raise SystemExit("no baseline.json in <private>/upgrade; run 'before' first")
    now = snapshot(a.project, a.output_root)
    write_json(os.path.join(a.private, "upgrade", f"after_{a.run}.json"), now)
    rows, summary = [], {"run": a.run, "baseline_utc": base["taken_utc"], "now_utc": now["taken_utc"], "roots": {}}
    cache_rows, rewritten = compare_cache(a, base["cache"], now["cache"], codes)
    facts_by_session = {}
    for r in cache_rows:
        if r["change"] == "changed" and r["field"] in FACT_FIELDS:
            facts_by_session.setdefault(r["session"], set()).add(r["field"])
    for root in a.output_root:
        key = os.path.normcase(os.path.abspath(root))
        if key not in base["roots"]:
            raise SystemExit(f"the baseline holds no record of {os.path.basename(root)}")
        b, n = base["roots"][key], now["roots"][key]
        report = report_roles(root)
        classes, meta, explanations = Counter(), Counter(), Counter()
        for vk in sorted(set(b["visits"]) | set(n["visits"])):
            bv, nv = b["visits"].get(vk), n["visits"].get(vk)
            rec = nv or bv
            code = codes.session(rec["subject_id"], rec["session"])
            cls, info = classify(bv, nv, report, root, a.verify_hashes, frozenset(facts_by_session.get(code, ())))
            classes[cls] += 1
            meta.update(info.get("meta_changed", []))
            explanations.update([";".join(info["explained_by"])] if info.get("explained_by") else [])
            rows.append({"layout": b["layout"], "visit": code, "class": cls,
                         "explained_by": ";".join(info.get("explained_by", [])),
                         "images_identical": info.get("images_identical", ""),
                         "fail": cls in FAIL, "roles_changed": ";".join(info.get("roles_changed", [])),
                         "meta_changed": ";".join(info.get("meta_changed", [])),
                         "differs_from_report": ";".join(info.get("differs_from_report", [])),
                         "files_same": info.get("files_same", ""), "files_changed": info.get("files_changed", ""),
                         "inventory_problems": info.get("inventory_problems", ""),
                         "receipt_before": ((bv or {}).get("receipt_id") or "")[:8],
                         "receipt_after": ((nv or {}).get("receipt_id") or "")[:8]})
        leftovers = [p for p in n["other_files"] if any(part.startswith(LEFTOVER) for part in p.split("/"))]
        changed_other = sorted(p for p in set(b["other_files"]) | set(n["other_files"])
                               if b["other_files"].get(p) != n["other_files"].get(p))
        summary["roots"][b["layout"]] = {
            "visits_before": len(b["visits"]), "visits_now": len(n["visits"]), "classes": dict(classes),
            "fail": sum(v for k, v in classes.items() if k in FAIL), "manifest_fields_changed": dict(meta),
            "replaced_expected_by": dict(explanations),
            "leftover_folders": len(leftovers), "other_files_changed": len(changed_other),
            "other_files_changed_names": [p for p in changed_other if not p.startswith(("sub-", "Subjects/"))]}
    fields = ["layout", "visit", "class", "explained_by", "images_identical", "fail", "roles_changed", "meta_changed",
              "differs_from_report", "files_same",
              "files_changed", "inventory_problems", "receipt_before", "receipt_after"]
    r4real.write_csv(os.path.join(a.evidence, f"upgrade_{a.run}_visits.csv"), rows, fields)
    r4real.write_csv(os.path.join(a.evidence, f"upgrade_{a.run}_cache.csv"), cache_rows,
                     ["session", "series", "field", "change", "after_vs_reader"])
    summary["cache"] = {"files_before": len(base["cache"]), "files_now": len(now["cache"]), "files_rewritten": rewritten,
                        "cache_versions_now": dict(Counter(str(c["cache_version"]) for c in now["cache"].values())),
                        "series_fields": dict(Counter(f"{r['change']}: {r['after_vs_reader']}" for r in cache_rows)),
                        "by_field": dict(Counter(f"{r['field']}: {r['after_vs_reader']}" for r in cache_rows))}
    report = {}
    for root in a.output_root:
        for r in r4real.read_csv(os.path.join(root, "selection.csv")) if os.path.exists(os.path.join(root, "selection.csv")) else []:
            for item in (r["warnings"] + ";" + r["reasons"]).split(";"):
                if item.startswith("override"):
                    report[item.split("(")[0]] = report.get(item.split("(")[0], 0) + 1
    summary["overrides"] = {"file_before": base["overrides"] is not None, "file_now": now["overrides"] is not None,
                            "report_override_items": report}
    summary["decisions_part_holds"] = all(r["fail"] == 0 for r in summary["roots"].values()) and \
        all(r["leftover_folders"] == 0 for r in summary["roots"].values())
    with open(os.path.join(a.evidence, f"upgrade_{a.run}_summary.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, indent=1, sort_keys=True)
    print(json.dumps(summary, indent=1, sort_keys=True))


def main(argv=None):
    p = argparse.ArgumentParser(description="R4-OPS-02: upgrade comparison")
    p.add_argument("what", choices=["before", "compare"])
    p.add_argument("--project", required=True)
    p.add_argument("--private", required=True)
    p.add_argument("--output-root", action="append", required=True)
    p.add_argument("--evidence", required=True)
    p.add_argument("--run", default="", help="upgrade run label for compare (UA, UB, UC)")
    p.add_argument("--mismatches", help="R4-VAL-01's pre-fix mismatches.csv (compare)")
    p.add_argument("--verify-hashes", action="store_true", help="hash replaced and new visits' files (compare)")
    p.add_argument("--force", action="store_true", help="overwrite an existing baseline (before)")
    a = p.parse_args(argv)
    r4real.check_outputs(a.project, a.private, a.evidence)
    for root in a.output_root:
        if r4real.inside(root, a.project):
            raise SystemExit("refused: an output root lies inside the project")
    codes = r4real.Codes(a.private)
    if a.what == "compare" and not a.run:
        raise SystemExit("compare needs --run")
    {"before": before, "compare": compare}[a.what](a, codes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
