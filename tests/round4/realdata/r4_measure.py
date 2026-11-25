"""R4-VAL-03 and the measures of spec 8.3: review rate, no-selection rate, fact agreement, consequential errors
and pixel sampling. Each is given for all decisions, then split by role and by scanner make and model; every
rate carries a 95% Wilson interval.

    python tests/round4/realdata/r4_measure.py --private D:\\round4\\private --reports D:\\round4\\private\\runA2 \\
        --evidence D:\\round4\\evidence\\realdata

Review rate (owner decision at G1, N = 10%): among role decisions with at least one candidate, excluding
outcomes missing, session_excluded and source_not_ready, the share whose outcome is review_required. The overall
rate is judged against the ceiling; per-modality and per-model rates are reported only. Reads facts.csv,
objective.csv and pixels.csv from the evidence folder when present. Writes measures.json (no identifiers).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

import r4real

CEILING = 0.10
EXCLUDED = {"missing", "session_excluded", "source_not_ready"}
RANKED = ("T1", "T1_POST", "T2", "FLAIR", "DWI", "SWI", "QSM", "GRE")
LIMIT = ("D-9: these measures compare select with the header reader, its own written rules and the objective "
         "checks. Whether those rules agree with an expert's choice of scan is outside what they measure.")


def rates(rows, hit, group=None):
    out = {"overall": r4real.wilson(sum(1 for r in rows if hit(r)), len(rows))}
    if group:
        groups = defaultdict(list)
        for r in rows:
            groups[group(r)].append(r)
        out["by"] = {g: r4real.wilson(sum(1 for r in rs if hit(r)), len(rs)) for g, rs in sorted(groups.items())}
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="R4-VAL-03 measures")
    p.add_argument("--private", required=True)
    p.add_argument("--reports", required=True)
    p.add_argument("--evidence", required=True)
    a = p.parse_args(argv)
    codes = r4real.Codes(a.private)
    decisions = r4real.read_csv(os.path.join(a.reports, "selection.csv"))
    vrows = r4real.read_csv(os.path.join(a.reports, "selection_volumes.csv"))
    models, makers = defaultdict(Counter), defaultdict(Counter)
    for r in vrows:
        if r["model"]:
            models[(r["subject_id"], r["session"])][r["model"]] += 1
        if r["manufacturer"]:
            makers[(r["subject_id"], r["session"])][r["manufacturer"]] += 1
    model_of = {k: v.most_common(1)[0][0] for k, v in models.items()}
    maker_of = {k: v.most_common(1)[0][0] for k, v in makers.items()}
    by_code_model = {codes.visit(s, d): m for (s, d), m in model_of.items()}

    def model(r):
        return f"{maker_of.get((r['subject_id'], r['session']), '?')} {model_of.get((r['subject_id'], r['session']), '?')}"

    base = [d for d in decisions if d["outcome"] not in EXCLUDED and int(d["n_candidates"] or 0) >= 1]
    review = {"role_decisions": len(base),
              "rate": rates(base, lambda r: r["outcome"] == "review_required", None),
              "by_modality": rates(base, lambda r: r["outcome"] == "review_required", lambda r: r["role"])["by"],
              "by_scanner_model": rates(base, lambda r: r["outcome"] == "review_required", model)["by"],
              "reasons": dict(Counter(x.split("(")[0] for d in base if d["outcome"] == "review_required"
                                      for x in d["reasons"].split(";") if x)),
              "ceiling": CEILING}
    overall = review["rate"]["overall"]
    review["within_ceiling"] = bool(overall and overall["rate"] <= CEILING)
    no_sel = {"rate": rates(base, lambda r: r["outcome"] == "no_eligible_candidate", None)["overall"],
              "by_modality": rates(base, lambda r: r["outcome"] == "no_eligible_candidate", lambda r: r["role"])["by"],
              "reasons": dict(Counter(x.split("(")[0] for d in base if d["outcome"] == "no_eligible_candidate"
                                      for x in d["reasons"].split(";") if x))}
    measures = {"run": "first run (reports of run A2), before any fix: the unbiased result (D-5)", "limit": LIMIT,
                "visits": len({(d["subject_id"], d["session"]) for d in decisions}),
                "outcomes": dict(Counter(d["outcome"] for d in decisions)), "review": review, "no_selection": no_sel}
    facts_path = os.path.join(a.evidence, "facts.csv")
    if os.path.exists(facts_path):
        facts = r4real.read_csv(facts_path)
        per_field = defaultdict(Counter)
        for r in facts:
            per_field[r["field"]][r["result"]] += 1
        measures["fact_agreement"] = {
            "series_fields_compared": sum(c["equal"] + c["mismatch"] + c["absent"] for c in per_field.values()),
            "by_field": {f: {"equal": c["equal"], "compared": c["equal"] + c["mismatch"] + c["absent"],
                             "unclear": c["unclear"],
                             "agreement": round(c["equal"] / (c["equal"] + c["mismatch"] + c["absent"]), 4)
                             if c["equal"] + c["mismatch"] + c["absent"] else None}
                         for f, c in sorted(per_field.items())},
            "mismatches_by_model": dict(Counter(by_code_model.get(r["session"].split("_")[0], "?") for r in facts
                                                if r["result"] in ("mismatch", "absent")))}
    obj_path = os.path.join(a.evidence, "objective.csv")
    if os.path.exists(obj_path):
        obj = [r for r in r4real.read_csv(obj_path) if r["rule"] != "harness"]
        chose = [d for d in decisions if d["role"] in RANKED and d["outcome"] in ("selected", "selected_fallback")]
        flagged_all = {(r["visit"], r["role"]) for r in obj}
        flagged_frozen = {(r["visit"], r["role"]) for r in obj if r["rule"] == "frozen"}
        key = lambda d: (codes.visit(d["subject_id"], d["session"]), d["role"])  # noqa: E731
        # Owner reading R4-F-026 (accepted 2026-11-25): 5.5 groups native volumes only, so a `copies` flag whose
        # same-identity partners are all derived maps, or Philips ADC maps labelled ORIGINAL, is not a consequential
        # error. A `copies` flag whose other partners hold a voxel copy of the chosen image (copies.csv) chose the
        # same image: left out of the headline, kept in the conservative figure.
        partners = defaultdict(list)
        copies_path = os.path.join(a.evidence, "copies.csv")
        if os.path.exists(copies_path):
            for r in r4real.read_csv(copies_path):
                partners[(r["visit"], r["role"], r["chosen_series"])].append(r)

        def is_map(p):
            return "derived" in p["partner_kind"] or ("outside" in p["partner_kind"] and "ADC" in p["partner_image_type"].split("\\"))

        def flag_class(r):
            ps = partners.get((r["visit"], r["role"], r["series_number"]), [])
            if r["category"] != "copies" or not ps:
                return "consequential"
            if all(is_map(p) for p in ps):
                return "R4-F-026"
            if all(is_map(p) or p["voxels"].startswith("copy") for p in ps):
                return "voxel copy"
            return "consequential"

        per_decision = defaultdict(list)
        for r in obj:
            per_decision[(r["visit"], r["role"])].append(r)
        classes = {}
        listing = []
        for k, rows in sorted(per_decision.items()):
            fc = [flag_class(r) for r in rows]
            cls = next(c for c in ("consequential", "voxel copy", "R4-F-026") if c in fc)
            classes[k] = cls
            ps = [p for r in rows for p in partners.get((r["visit"], r["role"], r["series_number"]), [])]
            listing.append({"visit": k[0], "role": k[1], "series_number": rows[0]["series_number"],
                            "flags": ";".join(f"{r['category']}({r['rule']})" for r in rows), "class": cls,
                            "frozen_flag": any(r["rule"] == "frozen" for r in rows),
                            "partner_kinds": ";".join(sorted({p["partner_kind"] for p in ps})),
                            "partner_voxels": ";".join(sorted({p["voxels"] for p in ps if p["voxels"]}))})
        r4real.write_csv(os.path.join(a.evidence, "consequential.csv"), listing,
                         ["visit", "role", "series_number", "flags", "class", "frozen_flag", "partner_kinds", "partner_voxels"])
        headline = {k for k, c in classes.items() if c == "consequential"}
        conservative = {k for k, c in classes.items() if c in ("consequential", "voxel copy")}
        # A check outside the objective checks (R4-F-029): the chosen acquisition holds no volume of the decision's
        # own role (select maps SWI_PHASE acquisitions into SWI).
        by_acq = defaultdict(set)
        for r in vrows:
            by_acq[(r["subject_id"], r["session"], r["acquisition_id"])].add(r["role"])
        role_mismatch = sorted({key(d) for d in chose
                                if d["role"] not in by_acq.get((d["subject_id"], d["session"], d["candidate_id"]), {d["role"]})})
        measures["consequential_errors"] = {
            "decisions_where_select_chose": len(chose),
            "headline": r4real.wilson(sum(1 for d in chose if key(d) in headline), len(chose)),
            "headline_basis": "frozen checks plus the owner's SWI amendment, with readings R4-F-026 and voxel copies (owner, 2026-11-25)",
            "conservative": r4real.wilson(sum(1 for d in chose if key(d) in conservative), len(chose)),
            "conservative_basis": "as the headline, but a choice whose partner is a voxel copy of it still counts",
            "rate_frozen_checks_only": r4real.wilson(sum(1 for d in chose if key(d) in flagged_frozen), len(chose)),
            "rate_with_amendment": r4real.wilson(sum(1 for d in chose if key(d) in flagged_all), len(chose)),
            "decisions_by_class": dict(Counter(classes.values())),
            "chosen_acquisition_without_the_role_R4_F_029": {"decisions": [list(k) for k in role_mismatch],
                                                             "headline_with_them": r4real.wilson(
                                                                 len(headline | set(role_mismatch) & {key(d) for d in chose}), len(chose))},
            "by_modality_headline": rates(chose, lambda d: key(d) in headline, lambda d: d["role"])["by"],
            "by_scanner_model_headline": rates(chose, lambda d: key(d) in headline, model)["by"],
            "by_category": dict(Counter(r["category"] for r in obj)),
            "by_modality_with_amendment": rates(chose, lambda d: key(d) in flagged_all, lambda d: d["role"])["by"]}
    pix_path = os.path.join(a.evidence, "pixels.csv")
    if os.path.exists(pix_path):
        pix = r4real.read_csv(pix_path)
        measures["pixel_sampling"] = {"series_checked": len(pix), "status": dict(Counter(r["status"] for r in pix)),
                                      "not_compared_reasons": dict(Counter(r["reason"] for r in pix
                                                                           if r["status"] == "not compared"))}
        tri_path = os.path.join(a.evidence, "pixels_triage.csv")
        if os.path.exists(tri_path):
            chosen = [r for r in r4real.read_csv(tri_path) if r["chosen"] == "True"]
            measures["pixel_sampling"]["chosen_series_parts"] = {
                "parts": len(chosen), "matched": r4real.wilson(sum(1 for r in chosen if r["status"] == "matched"), len(chosen)),
                "status": dict(Counter(r["status"] + (f": {r['reason']}" if r["reason"] else "") for r in chosen))}
    with open(os.path.join(a.evidence, "measures.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(measures, f, indent=1, sort_keys=True)
    o = review["rate"]["overall"]
    print(f"role decisions {len(base)}; review rate {o['rate']:.4f} [{o['low']:.4f}, {o['high']:.4f}]; "
          f"within the {CEILING:.0%} ceiling: {review['within_ceiling']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
