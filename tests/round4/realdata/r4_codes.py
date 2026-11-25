"""The visit-code table (plan 2.1 item 43; spec 4.2 "Data handling").

    python tests/round4/realdata/r4_codes.py --project D:\\mri_project --private D:\\round4\\private

Reads the folder names below <project>/Niftis/Subjects and <project>/DicomExtracted/Subjects (names
only; no file is opened) and writes <private>/visit_codes.json: subject -> P###, visit (subject,
date) -> V###, session label -> V### or V###_<n>. The table links codes to subject IDs and dates, so
it stays in the private folder and is never copied into evidence. Prints counts only. Refuses a
private folder inside the project.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def _inside(path, root):
    p, r = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return p == r or p.startswith(r + os.sep)


def build(project):
    sessions = set()
    for top in ("Niftis", "DicomExtracted"):
        base = os.path.join(project, top, "Subjects")
        if not os.path.isdir(base):
            continue
        for sid in os.listdir(base):
            if os.path.isdir(os.path.join(base, sid)):
                for label in os.listdir(os.path.join(base, sid)):
                    if os.path.isdir(os.path.join(base, sid, label)) and not label.startswith("."):
                        sessions.add((sid, label))
    subjects = {sid: f"P{i:03d}" for i, sid in enumerate(sorted({s for s, _ in sessions}), 1)}
    visits = {}
    for sid, label in sorted(sessions):
        visits.setdefault((sid, label.split("_", 1)[0]), None)
    visit_codes = {k: f"V{i:03d}" for i, k in enumerate(sorted(visits), 1)}
    session_codes = {}
    for sid, label in sorted(sessions):
        date, _, suffix = label.partition("_")
        session_codes[f"{sid}/{label}"] = visit_codes[(sid, date)] + (f"_{suffix}" if suffix else "")
    return {"subjects": subjects, "visits": {f"{s}/{d}": c for (s, d), c in visit_codes.items()},
            "sessions": session_codes}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the round 4 visit-code table")
    parser.add_argument("--project", required=True)
    parser.add_argument("--private", required=True)
    args = parser.parse_args(argv)
    if _inside(args.private, args.project):
        parser.error("the private folder must not be inside the project")
    table = build(args.project)
    os.makedirs(args.private, exist_ok=True)
    with open(os.path.join(args.private, "visit_codes.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(table, f, indent=1, sort_keys=True)
    print(f"subjects={len(table['subjects'])} visits={len(table['visits'])} sessions={len(table['sessions'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
