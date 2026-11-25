"""End-to-end runs of catalog exams and the comparison of `select`'s reports with the catalog's
expected outcomes (phase 2 helper for TG-IDN, TG-LNK, TG-CON, TG-RNK, TG-REV, TG-VIS, TG-HDR).

Commands run in child processes (r4lib.project). `select`'s reports are read as its documented
interface (README "Scan selection"; scan-selection.md 7.3 and 7.4): `selection.csv`,
`selection_volumes.csv`, the header cache and the published folders.

A report row is tied to its catalog series through the published sidecar's `source_series_key`,
which the oracle computes on its own: SHA-256 of the series' SeriesInstanceUID, truncated to 20
hex characters (catalog UIDs, plan 3.2). That way the comparison never depends on the
SeriesNumber a converter writes.

Selectors in the catalog's expected outcomes:
    "3"         every volume of series 3        "3:*"     the same
    "2:e1"      the volume of echo 1 of series 2 (the catalog's echo order)
    "900:stack2" the volume(s) of the second stack of mixed series 900
    "10+11"     the acquisition holding series 10 and 11
    "5:adc"     the dcm2niix _ADC companion of series 5's volume
Nothing here computes an expected value; every expected value comes from the catalog.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sys

from r4lib import catalog, dicom_builder
from r4lib.project import R4Project

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_CONVERTER = os.path.join(HERE, "fake_dcm2niix.py")
COPY_FATES = {"selected", "selected_derived", "companion", "kept"}


def series_key(exam, uid_series):
    return hashlib.sha256(dicom_builder.series_uid(exam["uid_exam"], uid_series).encode("ascii")).hexdigest()[:20]


def all_series(exam):
    return [s for session in exam["sessions"] for s in session["series"]]


def sidecar_hints(exam):
    """{SeriesInstanceUID: sidecar_hints} for the tier 2 converter (R4_FAKE_SIDECARS)."""
    out = {}
    for s in all_series(exam):
        if s.get("sidecar_hints"):
            out[dicom_builder.series_uid(exam["uid_exam"], s["uid_series"])] = s["sidecar_hints"]
    return out


# ---------------------------------------------------------------------------- converted projects

class Converted:
    def __init__(self, project, exam, converter, steps):
        self.project, self.exam, self.converter, self.steps = project, exam, converter, steps

    @property
    def root(self):
        return self.project.root

    def select(self, out, *args, env=None):
        return SelectRun(self, out, self.project.run("select", "--output-root", str(out), *args, env=env))

    def standard(self):
        """(report-only run, --extras run, --publish run, published visit folder), run once per exam.

        The --extras run supplies each volume's folder (selected_path is set only for copied
        volumes); the --publish run supplies the published files."""
        if not hasattr(self, "_standard"):
            base = os.path.join(os.path.dirname(self.root), "runs")
            run = self.select(os.path.join(base, "Sel"))
            extras = self.select(os.path.join(base, "SelX"), "--extras")
            pub = self.select(os.path.join(base, "Pub"), "--publish")
            subject = self.exam["roster_rows"][0][0]
            visit = run.rows[0]["session"] if run.rows else ""
            self._standard = (run, extras, pub, os.path.join(base, "Pub", "Subjects", subject, visit))
        return self._standard

    def bids(self):
        """(run, BIDS root) of `select --bids --publish` into a fresh root, run once per exam."""
        if not hasattr(self, "_bids"):
            root = os.path.join(os.path.dirname(self.root), "runs", "BIDS")
            options = self.exam.get("run_options")
            args = [a for a in options if a not in ("--bids", "--publish")] if options else []
            self._bids = (self.select(root, "--bids", "--publish", *args), root)
        return self._bids

    def check(self, req):
        """Problems for one generic requirement key of this exam (check_key on the standard runs)."""
        run, extras, _pub, visit = self.standard()
        problems = check_key(req, self.exam["expected"][req]["outcome"], run, extras, visit)
        record("catalog_checks", {"exam": self.exam["id"], "requirement": req, "converter": self.converter,
                                  "problems": problems})
        return problems


def converter_args(base, exam, converter):
    """(convert arguments, environment) for 'real' (MRI_PIPELINE_DCM2NIIX) or 'fake'."""
    env = dict(os.environ)
    if converter == "real":
        binary = os.environ.get("MRI_PIPELINE_DCM2NIIX")
        if not binary:
            raise RuntimeError("MRI_PIPELINE_DCM2NIIX is not set")
        return ["--dcm2niix-bin", binary], env
    hints = os.path.join(str(base), f"{exam['id']}_sidecars.json")
    with open(hints, "w", encoding="utf-8") as f:
        json.dump(sidecar_hints(exam), f)
    env["R4_FAKE_SIDECARS"] = hints
    return ["--dcm2niix-bin", FAKE_CONVERTER], env


def build(base, cat_name, exam_id, converter="real", sessions=None, prepare_args=(), convert_args=()):
    """Place an exam's ZIPs, then prepare and convert. ``sessions``: labels to convert (default all)."""
    data = catalog.load_file(cat_name)
    exam = next(e for e in data["exams"] if e["id"] == exam_id)
    os.makedirs(str(base), exist_ok=True)
    project = R4Project(os.path.join(str(base), "project"))
    project.place_exam(exam)
    steps = []
    prep = project.run("prepare", *prepare_args)
    steps.append(("prepare", prep.code))
    if prep.code != 0:
        raise AssertionError(f"{exam_id}: prepare exited {prep.code}: {prep.stderr[-2000:]}")
    args, env = converter_args(base, exam, converter)
    extra = []
    for label in sessions or ():
        extra += ["--session", f"{exam['roster_rows'][0][0]}/{label}"]
    conv = project.run("convert", *args, *extra, *convert_args, env=env)
    steps.append(("convert", conv.code))
    if conv.code != 0:
        raise AssertionError(f"{exam_id}: convert exited {conv.code}: {conv.stderr[-2000:]}")
    return Converted(project, exam, converter, steps)


class ExamStore:
    """Session-wide cache: each (catalog, exam, converter) is prepared and converted once."""

    def __init__(self, base):
        self.base, self.done = str(base), {}

    def get(self, cat_name, exam_id, converter="real"):
        key = (cat_name, exam_id, converter)
        if key not in self.done:
            path = os.path.join(self.base, f"{exam_id}-{converter}")
            if os.path.exists(path):
                shutil.rmtree(path)
            self.done[key] = build(path, cat_name, exam_id, converter)
        return self.done[key]


# ---------------------------------------------------------------------------- select runs

def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def split(cell):
    return [x for x in (cell or "").split(";") if x != ""]


class SelectRun:
    def __init__(self, conv, out, result):
        self.conv, self.out, self.result = conv, str(out), result
        self.code = result.code
        self.rows = read_csv(os.path.join(self.out, "selection.csv"))
        self.vrows = read_csv(os.path.join(self.out, "selection_volumes.csv"))
        self._series = {}
        exam = conv.exam
        keys = {series_key(exam, s["uid_series"]): s for s in all_series(exam)}
        for r in self.vrows:
            sidecar = self.sidecar(r["source"])
            r["_sidecar"] = sidecar
            s = keys.get(sidecar.get("source_series_key"))
            r["_uid"] = s["uid_series"] if s else None

    def sidecar(self, source):
        path = os.path.join(self.conv.root, "Niftis", *source.split("/"))
        path = path[:-7] + ".json" if path.endswith(".nii.gz") else path
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except OSError:
            return {}

    def role_row(self, role, session=None):
        found = [r for r in self.rows if r["role"] == role and (session is None or r["session"] == session)]
        return found[0] if len(found) == 1 else (found or None)

    def members(self, candidate_id):
        if not candidate_id:
            return []
        return [r for r in self.vrows if r["acquisition_id"] == candidate_id or r["derived_group_id"] == candidate_id]

    def uids_of(self, candidate_id):
        return sorted({r["_uid"] for r in self.members(candidate_id)}, key=str)


# ---------------------------------------------------------------------------- selectors

def selector_uids(selector):
    base = str(selector).split(":", 1)[0]
    return sorted(int(x) for x in base.split("+"))


def rows_for(run, selector):
    """Report rows a selector names; [] when none match."""
    exam = run.conv.exam
    sel = str(selector)
    base, _, qual = sel.partition(":")
    uids = [int(x) for x in base.split("+")]
    rows = [r for r in run.vrows if r["_uid"] in uids]
    if qual in ("", "*"):
        return rows
    series = next(s for s in all_series(exam) if s["uid_series"] == uids[0])
    if qual.startswith("e") and qual[1:].isdigit():
        te = float(series["echoes"][int(qual[1:]) - 1])
        return [r for r in rows if _te_ms(r) is not None and abs(_te_ms(r) - te) < 0.05]
    if qual.startswith("stack"):
        specs = dicom_builder.stack_specs(series)
        spec = specs[int(qual[5:]) - 1]
        tes = [(s.get("echoes") or [None])[0] for s in specs]
        if len(set(tes)) == len(tes) and None not in tes:
            return [r for r in rows if _te_ms(r) is not None and abs(_te_ms(r) - float(spec["echoes"][0])) < 0.05]
        n = int(spec["slices"])
        return [r for r in rows if str(n) in _dims(r)]
    raise ValueError(f"selector qualifier not handled here: {selector}")


def _te_ms(row):
    te = row["_sidecar"].get("EchoTime")
    return None if te is None else float(te) * 1000.0


def _dims(row):
    return [str(row.get("n_slices", ""))]


def folder_of(row):
    path = row.get("selected_path") or ""
    return path.rsplit("/", 1)[0] if "/" in path else ""


# ---------------------------------------------------------------------------- checks

def _close(a, b, tol):
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def reasons_include(cell, wanted):
    have = split(cell)
    missing = [w for w in wanted if not any(h == w or h.startswith(w + "(") or w in h for h in have)]
    return missing


def check_decision(run, role, exp, session=None):
    """Problems for one role's expected decision (R-RNK-3 and the decision parts of other keys)."""
    out = []
    row = run.role_row(role, session)
    if row is None or isinstance(row, list):
        return [f"{role}: expected one selection.csv row, found {0 if row is None else len(row)}"]
    for key, value in exp.items():
        if key == "outcome":
            if row["outcome"] != value:
                out.append(f"{role}: outcome {row['outcome']!r}, expected {value!r} (reasons {row['reasons']!r})")
        elif key in ("decided_by", "tier"):
            if row[key] != value:
                out.append(f"{role}: {key} {row[key]!r}, expected {value!r}")
        elif key == "winner":
            got = run.uids_of(row["candidate_id"])
            want = sorted({u for sel in value for u in selector_uids(sel)})
            if got != want:
                out.append(f"{role}: winner series {got}, expected {want}")
        elif key in ("runner_up", "rule_recommendation"):
            got = run.uids_of(row[key])
            want = sorted({u for sel in value for u in selector_uids(sel)})
            if got != want:
                out.append(f"{role}: {key} series {got}, expected {want}")
        elif key == "reasons_include":
            missing = reasons_include(row["reasons"], value)
            if missing:
                out.append(f"{role}: reasons {row['reasons']!r} lack {missing}")
        elif key == "warnings_include":
            missing = [w for w in value if w not in split(row["warnings"])]
            if missing:
                out.append(f"{role}: warnings {row['warnings']!r} lack {missing}")
        elif key in ("n_candidates", "n_eligible"):
            if str(row[key]) != str(value):
                out.append(f"{role}: {key} {row[key]!r}, expected {value!r}")
        else:
            out.append(f"{role}: expectation key {key!r} not handled by the checker")
    return out


def check_decisions(run, decisions, session=None):
    out = []
    for role, exp in decisions.items():
        out += check_decision(run, role, exp, session)
    return out


def _each(run, mapping, fn):
    out = []
    for sel, want in mapping.items():
        rows = rows_for(run, sel)
        if not rows:
            out.append(f"{sel}: no report row")
            continue
        for r in rows:
            problem = fn(sel, r, want)
            if problem:
                out.append(problem)
    return out


def check_roles(run, roles):
    return _each(run, roles, lambda sel, r, want: None if (r["role"], int(r["role_rule"] or 0)) == (want[0] or "", want[1])
                 else f"{sel}: role {r['role']!r} rule {r['role_rule']!r}, expected {want}")


def check_warnings(run, warnings):
    return _each(run, warnings, lambda sel, r, want: None if set(want) <= set(split(r["warnings"]))
                 else f"{sel}: volume warnings {r['warnings']!r} lack {want}")


def check_field(run, mapping, column, normalize=str):
    return _each(run, mapping, lambda sel, r, want: None if normalize(r[column]) == normalize(want)
                 else f"{sel}: {column} {r[column]!r}, expected {want!r}")


def check_native(run, mapping, tol=0.0051):
    def fn(sel, r, want):
        if want == "unknown":
            return None if r["native_mm"] == "" else f"{sel}: native_mm {r['native_mm']!r}, expected unknown"
        parts = r["native_mm"].split("x") if r["native_mm"] else []
        if len(parts) != 3 or not all(_close(p, w, tol) for p, w in zip(parts, want)):
            return f"{sel}: native_mm {r['native_mm']!r}, expected {want}"
        return None
    return _each(run, mapping, fn)


def check_fates(run, fates, extras_run=None):
    """{selector: [fate or [fates] or None, folder]}; the folder comes from a run with --extras
    (selected_path is set only for copied volumes)."""
    by_id = {r["volume_id"]: r for r in (extras_run or run).vrows}

    def fn(sel, r, want):
        fate, folder = want
        allowed = fate if isinstance(fate, list) else [fate]
        if fate is not None and r["fate"] not in allowed:
            return f"{sel}: fate {r['fate']!r}, expected {fate!r}"
        got = folder_of(by_id.get(r["volume_id"], r))
        if folder is not None and got != folder:
            return f"{sel}: folder {got!r}, expected {folder!r}"
        return None
    return _each(run, fates, fn)


def check_links(run, links):
    def fn(sel, r, want):
        if isinstance(want, str):
            want = {"parent": [want]}
        probs = []
        if "state" in want and r["link_state"] != want["state"]:
            probs.append(f"state {r['link_state']!r} != {want['state']!r}")
        if "rung" in want and r["link_rung"] != want["rung"]:
            probs.append(f"rung {r['link_rung']!r} != {want['rung']!r}")
        if "parent" in want:
            got = run.uids_of(r["link_parent"])
            exp = sorted({u for s in want["parent"] for u in selector_uids(s)})
            if got != exp:
                probs.append(f"parent series {got} != {exp}")
        return f"{sel}: " + "; ".join(probs) if probs else None
    return _each(run, links, fn)


def check_phases(run, phases):
    return _each(run, phases, lambda sel, r, want: None if [r["contrast_phase"], r["contrast_evidence"]] == list(want)
                 else f"{sel}: phase {r['contrast_phase']!r}/{r['contrast_evidence']!r}, expected {want}")


def acquisition_of(run, selector):
    ids = {r["acquisition_id"] for r in rows_for(run, selector)}
    return ids


def check_acquisitions(run, exp):
    out = []
    for group in exp.get("acquisitions", []):
        ids = set()
        for sel in group:
            ids |= acquisition_of(run, sel)
        if len(ids) != 1 or "" in ids:
            out.append(f"acquisition {group}: volumes carry acquisition ids {sorted(ids)}")
            continue
        acq = next(iter(ids))
        members = {r["_uid"] for r in run.vrows if r["acquisition_id"] == acq}
        want = {u for sel in group for u in selector_uids(sel)}
        if members != want:
            out.append(f"acquisition {group}: holds series {sorted(members, key=str)}")
    for pair in exp.get("duplicates", []):
        a, b = (acquisition_of(run, s) for s in pair)
        rows_a = [r for r in run.vrows if r["acquisition_id"] in a]
        if not a or not b or not all(set(split(r["duplicate_of"])) >= b for r in rows_a):
            out.append(f"duplicates {pair}: duplicate_of does not link them")
    if exp.get("duplicates") == []:
        flagged = [r["_uid"] for r in run.vrows if r["duplicate_of"]]
        if flagged:
            out.append(f"no duplicates expected; duplicate_of set on series {flagged}")
    for sel, n in exp.get("n_echoes", {}).items():
        ids = acquisition_of(run, sel)
        rows = [r for r in run.vrows if r["acquisition_id"] in ids and r["part"] != "phase"]
        tes = {round(float(r["echo_te_ms"]), 1) for r in rows if r["echo_te_ms"]}
        if len(tes) != n:
            out.append(f"{sel}: {len(tes)} echoes, expected {n}")
    for sel, want in exp.get("has_phase", {}).items():
        ids = set()
        for part in sel.split("+"):
            ids |= acquisition_of(run, part)
        rows = [r for r in run.vrows if r["acquisition_id"] in ids]
        got = any(r["part"] == "phase" for r in rows)
        if got != want:
            out.append(f"{sel}: has_phase {got}, expected {want}")
    return out


def check_gradients(run, exp):
    out = []
    for sel, want in exp.items():
        for r in rows_for(run, sel) or [None]:
            if r is None:
                out.append(f"{sel}: no report row")
                continue
            got = {"b0": r["b0"], "shells": r["shells"], "b_max": r["b_max"], "n_dirs": r["n_dirs"]}
            for key in ("b0", "b_max", "n_dirs"):
                if key in want and str(got[key]) != str(want[key]):
                    out.append(f"{sel}: {key} {got[key]!r}, expected {want[key]!r}")
            if "shells" in want and split(r["shells"]) != [str(s) for s in want["shells"]]:
                out.append(f"{sel}: shells {r['shells']!r}, expected {want['shells']}")
            if "mismatch" in want:
                cached = cache_volume(run, r)
                got_m = (cached or {}).get("gradients", {}) or {}
                if got_m.get("mismatch") is not want["mismatch"]:
                    out.append(f"{sel}: cache gradient mismatch {got_m.get('mismatch')!r}, expected {want['mismatch']}")
    return out


# ---------------------------------------------------------------------------- the header cache

def cache_files(conv):
    root = os.path.join(conv.root, "Reports", ".selection_cache")
    out = []
    for dirpath, _d, files in os.walk(root):
        out += [os.path.join(dirpath, f) for f in files if f.endswith(".json")]
    return sorted(out)


def load_cache(conv):
    data = {}
    for path in cache_files(conv):
        with open(path, encoding="utf-8") as f:
            data[path] = json.load(f)
    return data


def cache_series(conv, exam, uid_series):
    key = series_key(exam, uid_series)
    for doc in load_cache(conv).values():
        for k, rec in doc.get("series", {}).items():
            if k == key or rec.get("series_key") == key:
                return rec
    return None


def cache_volumes(doc):
    """[(relpath, volume record)] of one cache file. scan-selection.md 7.3 documents a map keyed by
    relpath; the code under test writes a list of records holding 'relpath' (finding R4-F-003), so
    both forms are read."""
    vols = doc.get("volumes", {})
    if isinstance(vols, dict):
        return list(vols.items())
    return [(v.get("relpath", ""), v) for v in vols]


def cache_volume(run, row):
    name = row["source"].rsplit("/", 1)[-1]
    for doc in load_cache(run.conv).values():
        for rel, vol in cache_volumes(doc):
            if rel.replace("\\", "/").rsplit("/", 1)[-1] == name:
                return vol
    return None


# ---------------------------------------------------------------------------- one exam, every key

GENERIC_KEYS = {"R-IDN-1", "R-IDN-2", "R-IDN-3", "R-IDN-4", "R-IDN-5", "R-IDN-6", "R-IDN-7", "R-LNK-1",
                "R-LNK-2", "R-LNK-3", "R-CON-1", "R-CON-2", "R-RNK-3", "R-RNK-5", "R-RNK-6", "R-RNK-7",
                "R-REV-1", "R-REV-3"}


def check_key(req, outcome, run, extras_run, publish_root=None):
    """Problems for one generic requirement key of an exam's ``expected`` block."""
    o = outcome
    out = []
    if req == "R-IDN-1":
        out += check_roles(run, o.get("roles", {}))
        out += check_warnings(run, o.get("warnings", {}))
    elif req == "R-IDN-2":
        out += check_field(run, o.get("families", {}), "family")
    elif req == "R-IDN-3":
        out += check_field(run, o.get("kinds", {}), "kind")
        out += check_warnings(run, o.get("warnings", {}))
    elif req == "R-IDN-4":
        out += check_acquisitions(run, o)
    elif req == "R-IDN-5":
        out += check_native(run, o.get("native_mm", {}))
    elif req == "R-IDN-6":
        for sel in o.get("no_selected_volume_of", []):
            bad = [r["fate"] for r in rows_for(run, sel) if r["fate"] in ("selected", "selected_derived", "companion")]
            if bad or not rows_for(run, sel):
                out.append(f"{sel}: fates {bad or 'no rows'}; no volume may be chosen")
        for sel, want in o.get("reasons", {}).items():
            out += _each(run, {sel: want}, lambda s, r, w: None if not reasons_include(r["exclusion_reasons"], w)
                         else f"{s}: exclusion_reasons {r['exclusion_reasons']!r} lack {w}")
    elif req == "R-IDN-7":
        out += check_field(run, o.get("families", {}), "family")
        out += check_gradients(run, o.get("gradients", {}))
    elif req == "R-LNK-1":
        out += check_links(run, o.get("links", {}))
    elif req == "R-LNK-2":
        out += check_links(run, o.get("links", {}))
    elif req == "R-LNK-3":
        out += check_decisions(run, o.get("decisions", {}))
        out += check_fates(run, o.get("fates", {}), extras_run)
        for prefix in o.get("not_in", []):
            bad = [r["selected_path"] for r in run.vrows if r["selected_path"].startswith(prefix)]
            if bad:
                out.append(f"files in {prefix}: {bad}")
    elif req == "R-CON-1":
        out += check_phases(run, o.get("phases", {}))
    elif req in ("R-CON-2", "R-RNK-3"):
        out += check_decisions(run, o.get("decisions", {}))
    elif req == "R-RNK-5":
        out += _each(run, o.get("coverage_mm", {}), lambda s, r, w: None if _close(r["extent_mm"], w, 0.01)
                     else f"{s}: extent_mm {r['extent_mm']!r}, expected {w}")
        flagged = sorted({str(r["_uid"]) for r in run.vrows if "partial_coverage" in r["exclusion_reasons"]})
        want = sorted(str(x) for x in o.get("partial_coverage", []))
        if flagged != want:
            out.append(f"partial_coverage on series {flagged}, expected {want}")
    elif req == "R-RNK-6":
        out += check_fates(run, o.get("fates", {}), extras_run)
        for sel, folder in o.get("companion_with", {}).items():
            uid, kind = sel.split(":")
            rows = [r for r in rows_for(run, uid) if r["fate"] in COPY_FATES]
            if publish_root is None:
                out.append(f"{sel}: needs a published visit to check")
                continue
            found = []
            for r in rows:
                stem = r["selected_path"][:-7]
                found.append(os.path.exists(os.path.join(publish_root, stem + "_" + kind.upper() + ".nii.gz")))
            if not rows or not all(found):
                out.append(f"{sel}: companion not published beside its volume in {folder}/")
        # Every volume has exactly one fate (R-RNK-6 second half).
        if any(not r["fate"] for r in run.vrows):
            out.append("a volume row has no fate")
    elif req == "R-RNK-7":
        out += check_fates(run, o.get("fates", {}), extras_run)
        for sel, want in o.get("reasons", {}).items():
            out += _each(run, {sel: want}, lambda s, r, w: None if split(r["exclusion_reasons"]) == w
                         else f"{s}: exclusion_reasons {r['exclusion_reasons']!r}, expected {w}")
    elif req == "R-REV-1":
        for role in o.get("empty_role_folders", []):
            if publish_root is None:
                out.append("R-REV-1 needs a published run")
                continue
            for dirpath, _d, files in os.walk(publish_root):
                rel = os.path.relpath(dirpath, publish_root).replace(os.sep, "/")
                if rel.split("/")[-1] == role and "OTHER" not in rel and files:
                    out.append(f"role folder {rel} holds {len(files)} file(s)")
    elif req == "R-REV-3":
        if run.code != o["exit_code"]:
            out.append(f"exit code {run.code}, expected {o['exit_code']}")
    return out


def exam_cases(keys, catalogs=None, exclude_families=("naming", "cleanup")):
    """(catalog name, exam id, requirement) for every exam whose expected block holds one of keys."""
    out = []
    for name, data in (catalogs or catalog.load_all()).items():
        if name in exclude_families:
            continue
        for exam in data["exams"]:
            for req in exam["expected"]:
                if req in keys:
                    out.append((name, exam["id"], req))
    return out


def evidence_dir():
    path = os.environ.get("R4_EVIDENCE_DIR")
    if path:
        os.makedirs(path, exist_ok=True)
    return path


def record(name, item):
    """Append one JSON line to <R4_EVIDENCE_DIR>/<name>.jsonl (no identifying values: catalog IDs,
    requirement IDs and technical fields only)."""
    path = evidence_dir()
    if not path:
        return
    with open(os.path.join(path, name + ".jsonl"), "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(item, sort_keys=True, default=str) + "\n")


def python():
    return sys.executable
