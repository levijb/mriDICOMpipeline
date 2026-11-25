"""R4-OPS-01: file listings of the live project around an owner run (plan 2.1 item 44; spec 8.2).

    python tests/round4/realdata/r4_list.py before --project D:\\mri_project --private D:\\round4\\private --run A
    python tests/round4/realdata/r4_list.py after  --project D:\\mri_project --private D:\\round4\\private --run A \\
        --allowlist tests/round4/expected/allowlist_ops1.json --evidence D:\\round4\\evidence\\realdata

`before` lists every file below the project (relative path, size, modification time in ns) and
every session folder's modification time, and saves it to <private>/listing_<run>_before.json.
`after` lists again, compares with the saved listing under the R-OPS-1 allow-list, saves the full
difference to <private>/listing_<run>_diff.json and a coded difference (subject IDs and dates
replaced through <private>/visit_codes.json, built by r4_codes.py) to
<evidence>/listing_diff_<run>.json. The project is only walked and stat'ed, never written. Full
listings carry subject IDs and dates in file names, so they stay in the private folder. Prints
counts only; exits 1 when a change falls outside the allow-list.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from r4lib import listing  # noqa: E402


def _inside(path, root):
    p, r = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return p == r or p.startswith(r + os.sep)


def coder(private):
    path = os.path.join(private, "visit_codes.json")
    if not os.path.exists(path):
        raise SystemExit("visit_codes.json is missing in the private folder; run r4_codes.py first")
    with open(path, encoding="utf-8") as f:
        table = json.load(f)
    pairs = sorted(((k.split("/")[0], k.split("/")[1], v) for k, v in table["sessions"].items()),
                   key=lambda x: -len(x[0] + x[1]))
    subjects = sorted(table["subjects"].items(), key=lambda x: -len(x[0]))

    def code(rel):
        out = rel
        for sid, label, c in pairs:
            out = out.replace(f"{sid}/{label}", c).replace(f"{sid}_{label}", c)
        for sid, c in subjects:
            out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(sid)}(?![A-Za-z0-9])", c, out)
        out = re.sub(r"(?<!\d)(19|20)\d{6}(?!\d)", "<DATE>", out)
        return out
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description="R-OPS-1 listings of the live project")
    parser.add_argument("phase", choices=("before", "after"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--private", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--allowlist")
    parser.add_argument("--evidence")
    args = parser.parse_args(argv)
    for name in ("private", "evidence"):
        value = getattr(args, name)
        if value and _inside(value, args.project):
            parser.error(f"--{name} must not be inside the project")
    if not re.fullmatch(r"[A-Za-z0-9_]+", args.run):
        parser.error("--run is a short name such as A, B, C or A2")
    os.makedirs(args.private, exist_ok=True)
    current = listing.list_tree(args.project)
    saved = os.path.join(args.private, f"listing_{args.run}_{args.phase}.json")
    listing.save(current, saved)
    if args.phase == "before":
        print(f"run {args.run} before: files={len(current['files'])} session_folders={len(current['session_dirs'])}")
        return 0
    if not args.allowlist or not args.evidence:
        parser.error("after needs --allowlist and --evidence")
    before = listing.load(os.path.join(args.private, f"listing_{args.run}_before.json"))
    with open(args.allowlist, encoding="utf-8") as f:
        allow = json.load(f)
    d = listing.diff(before, current, allow["allow"], allow["forbidden_after_normal_end"])
    with open(os.path.join(args.private, f"listing_{args.run}_diff.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, indent=1)
    coded = listing.coded(d, coder(args.private))
    os.makedirs(args.evidence, exist_ok=True)
    unexplained = listing.unexplained(d)
    with open(os.path.join(args.evidence, f"listing_diff_{args.run}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"run": args.run, "files_before": len(before["files"]), "files_after": len(current["files"]),
                   "unexplained": len(unexplained), "difference": coded}, f, indent=1)
    counts = {k: len(v) for k, v in d.items()}
    print(f"run {args.run} after: " + " ".join(f"{k}={v}" for k, v in counts.items())
          + f" unexplained={len(unexplained)}")
    return 1 if unexplained else 0


if __name__ == "__main__":
    sys.exit(main())
