# Comprehension Test Report: Stage 6

**Target:** `docs/designs/production-hardening.md`
(FeatureRange 1-1340, DetailedRange 1341-2152)
**Standard:** none (self-containment test)
**Reviewer:** `egm-comprehension` (completed). The reviewer read only the
persona and the target document; it opened no other file.
**Mode:** Consolidated (uninterrupted); no edits were made to the target.

Compare the paraphrase below with what you intended. A wrong paraphrase is a
finding in itself.

## What this proposes

The document sets a behavior contract for `mri-dicom-pipeline`: ten
invariants, each with a guarantee, its exceptions, the evidence and a delivery
phase. It records the author's answers to 23 product questions (Q1-Q23),
assigns every baseline risk (R01-R20) and test group (T01-T18) to one of four
phases, and gives a file-level plan for phase 1 only.

Phase 1 does these things:

- Splits `pipeline.py` into modules. Step 0 only moves code.
- Adds state schema 3 and a one-time `migrate` command.
- Fixes P1 and P3-P11. `converted` will mean every MR-image series produced a
  volume. Canonical ZIPs become immutable. New uploads need marker files. A
  complete output folder is never reconverted automatically. State keeps the
  last good conversion separate from the last attempt.
- Adds locks on every storage root, intent files with reconciliation at
  startup, and SIGTERM handling with exit code 143.
- Numbers journals by sequence and keeps an index backup.
- Redacts converter output before it reaches disk. Publishes NIfTI bytes
  unchanged. Records provenance but never compares it, so P2 stays open on
  purpose.
- Adds classifier revision 2, an Apache-2.0 license, and a Linux/Windows CI
  matrix with smoke tests against the real `dcm2niix`.

Later phases add child-process containment, limits and retention (phase 2),
scale budgets and `relocate` (phase 3), and the README contract and first
release (phase 4).

## How the existing system works, per this document

Version 0.1.0 has two commands. `prepare` finds session ZIPs named like
`S001_20260725.zip` and checks the subject and date against a roster CSV. It
hashes the ZIP, moves it to a canonical path under `DicomUploads/SubjectsSorted/`,
and extracts the DICOM objects into `DicomExtracted/`, writing a JSON inventory
and an ownership receipt. A changed upload in `Uploads/` is held as a
conflict, but a changed ZIP at the canonical path is silently re-extracted (the
"replacement" branch).

`convert` runs `dcm2niix` on each prepared session, then:

- clears NIfTI header text and filters JSON sidecars;
- redacts the converter log only after the run;
- files outputs under `Niftis/Subjects/<id>/<session>/` using a substring
  classifier;
- marks the session `converted` if the converter exited 0 and at least one
  valid volume exists.

Other attempts go to `.pending/`. Folders are published by renaming through
`.incoming_*` and `.previous_*` siblings.

A single coordinator thread writes state (`Reports/.state/index.json` plus
per-event journals replayed at startup). Worker threads, one per subject, do
the work. Reruns skip finished sessions using stored sizes, mtimes and
directory-mtime maps, with no content reads unless `--audit` is given. One lock
file under `--project-root` guards each project. Reports are
`session_status.csv`, `issues.csv`, `run_history.csv` and `pipeline.log`. Exit
code 2 means unresolved issues remain.

No section was too unclear to paraphrase, so there are no Blockers.

## Findings

**Severity breakdown:** 0 Blocker, 5 Major, 9 Nit.

### CO-1. Decision status contradicts itself - Severity: Major (FeatureRange)
* **Affected Scope:** Open Questions (211-239); Goal 2 (156-160); Q7
  (603-606); Q20 (571-572); Q15 Resource limits (801-804); Q2 Python row
  (1112); Q17 (1165-1169).
* **Discrepancy Description & Rationale:**
  * Open Questions says the Q15 limit defaults, the Q17 proposals, Python
    3.11+, `--session` and `--accept-multi-study` were all approved on
    2026-11-04.
  * The Decision Log still marks the same items as unsettled: Q15 is "pending
    author confirmation ... for review at the section gate", Q17 is "proposed,
    to confirm at the section gate", the Q2 Python row says "proposed, confirm
    at section review", and Q7 and Q20 say "Proposed:".
  * Goal 2's evidence rule says Open Questions should hold only deferred items,
    each with a phase and a reason. The section holds approved items and
    nothing deferred, so its heading misleads.
  * A fresh reader cannot tell which values are final.

### CO-2. Receipts, inventories and the cache check are never explained - Severity: Major (FeatureRange)
* **Affected Scope:** lines 92, 100, 141-143, 342, 365-367, 646, 798,
  1026-1031; also "control files" at 1677 (DetailedRange).
* **Discrepancy Description & Rationale:**
  * "Receipt", "receipt descriptor", "receipt check", "preparation inventory"
    and "output/conversion inventory" carry several core guarantees: folders
    without a receipt are never replaced, P10, Q7's `output modified`, Q12
    adoption, Q23 relocation and R17.
  * The document never says where a receipt file sits, what it holds, how it
    differs from the descriptor stored in the index, what the fast check
    compares, or what each inventory lists. "Control files" is also undefined.
  * Without this, a reader cannot see why gunzipping one file trips the check,
    or why a move that loses directory mtimes invalidates receipts.

### CO-3. Project layout and root options are not described - Severity: Major (FeatureRange)
* **Affected Scope:** lines 64-65, 92-93, 96, 209, 836, 1302, 1395-1397
  (DetailedRange for the option list).
* **Discrepancy Description & Rationale:**
  * The document uses many paths (`Uploads/`, `DicomUploads/SubjectsSorted/`,
    `DicomExtracted/`, `Niftis/Subjects/`, `Niftis/.pending/`,
    `Reports/.state/`, `.work/convert_*`) and five named roots plus a project
    root.
  * It never gives the default layout under `--project-root`, and never maps
    root names to CLI options.
  * `--output-root` appears once (P6) and is missing from `migrate_parser`,
    which lists `--nifti-root` and `--input-root`. It is unclear whether these
    are the same thing.
  * It is unclear whether `Uploads/` sits inside `DicomUploads/`.
  * The staging root's default location, and that it may be on node-local
    disk, are only implied.
  * Q13 lock scope and Q23 relocation depend on this layout.

### CO-4. Issue and status model is only implicit - Severity: Major (FeatureRange, also DetailedRange)
* **Affected Scope:** Q4 (750-753); Q7 (597-599); Q12 (695-699); invariant 10
  (350); P4 (94); DetailedRange 1465-1466, 1690, 1698, 1708, 1713-1714.
* **Discrepancy Description & Rationale:**
  * The feature section relies on "blocking" and "nonblocking" issues but
    never says what an issue is, what "blocking" blocks (the session's
    conversion, exit code 2, or both), or how an issue clears.
  * The prefix rule `NONBLOCKING_PREFIXES = ("date:", "wait:", "missing:")`
    appears only in the DetailedRange, and `date:` is never explained.
  * `state:recovered` is called blocking but has no stated way to clear, so it
    reads as if every later run exits 2.
  * `session_status.csv` has separate `prepare_status` and `conversion_status`
    columns, mentioned only at 1713-1714. Today's status values are never
    listed (`needs_conversion` appears only in P4), so the reader cannot tell
    which statuses in invariant 10 are new, or which column shows "awaiting
    upload", "ready" and "interrupted".

### CO-5. Reconciliation table mixes intent kinds with discovery situations - Severity: Major (FeatureRange)
* **Affected Scope:** Q12 table (662-673); detection cost (681-682); CUJ 2
  (258-260); intent schema (1505-1509, DetailedRange).
* **Discrepancy Description & Rationale:**
  * The Intent column lists "Published folder with no state record" and "Stage
    directory", but only two intent kinds are defined: `zip_move` and
    `publish`.
  * Q12 says detection costs one listing of open intents, so it is unclear how
    an unrecorded published folder is found. Presumably it is an open
    `publish` intent whose event was never saved.
  * If so, the row "Destination present, no swap folders -> Done; clear the
    intent" would clear that intent without adopting the folder.
  * The order in which rows apply is not stated, so the reader cannot rebuild
    the recovery algorithm that CUJ 2 depends on.

### CO-6. Contract is promised for eight areas but organized by ten invariants - Severity: Nit (FeatureRange)
* **Affected Scope:** Overview (33-35, 43-52); Goal 1 (151-155); Behavior
  contract (339-352).
* **Discrepancy Description & Rationale:** The Overview promises guarantees
  across the eight review areas, but the table is organized by the baseline's
  ten invariants with no mapping between the two. Area 8 (compatibility,
  packaging, release) has no contract row; it appears only in Q2, Q17 and the
  release gates. Area 6 (scaling) maps only loosely to invariant 6. A reader
  cannot check whether Goal 1 is met.

### CO-7. Phase-scope statements are inconsistent - Severity: Nit (FeatureRange)
* **Affected Scope:** Q1 (1194); Goal 4 (164-171); implementation step 1
  (1357); Q19 (545-546) against Q8 (646-647); Q15 shutdown row (816) against
  CUJ 2 (256) and `test_sigterm_stops_converter_group_within_20s` (2003); run
  sequence (315).
* **Discrepancy Description & Rationale:**
  * Q1 says phase 1 "closes P1 to P11", but P2 is deliberately left open.
  * Goal 4 says severe risks come "before any work on performance, packaging,
    or reporting", yet implementation step 1 is packaging and CI, with no
    explanation.
  * Goal 4's evidence ("a regression test that fails on the baseline") cannot
    apply to P2 or P9.
  * Q19 calls the SOP class list "part of the conversion cache identity", but
    Q8 says the cache key is unchanged and nothing is compared.
  * The 20 s shutdown limit is a Q15 value, and Q15 is phase 2, yet a phase 1
    test asserts it.
  * The "after hardening" run sequence includes the phase 3 `relocate` refusal
    without marking its phase.

### CO-8. Existing CLI options and naming rules are assumed - Severity: Nit (FeatureRange)
* **Affected Scope:** lines 226, 341, 605, 744; DetailedRange 1413, 1633,
  1925-1926.
* **Discrepancy Description & Rationale:**
  * `--import-legacy` ("imports files already at rest") and "legacy" ZIPs are
    never explained.
  * `--allow-unlisted-dates` and `--limit` are not described.
  * The ZIP name rules (`SUBJECT_DATE[_N].zip`) are shown only by example. The
    rule that `_2` and `_10` are valid but `_1` and `_02` are not appears only
    in a test name.
  * The roster's contents (subject IDs and dates, the "pairs") are never
    stated.

### CO-9. Terms are used before they are defined - Severity: Nit (FeatureRange)
* **Affected Scope:** "companions" (47, 349; defined at 919-920); `.pending/`
  (127, 285, 612; location given at 836); `<scan_type>/` layout and
  classification in both stages (928-930).
* **Discrepancy Description & Rationale:** "Diffusion companions" are
  identified as `.bval`/`.bvec` only in Q9. Where `<scan_type>/` sits in the
  output path, and why `prepare` and `convert` both classify series, are never
  explained.

### CO-10. Process vocabulary is undefined or overloaded - Severity: Nit (FeatureRange)
* **Affected Scope:** lines 36, 74, 80, 218, 431, 804, 933, 1112, 1169, 1226,
  1317, 2152; Q15 headings at 801 and 825.
* **Discrepancy Description & Rationale:**
  * "Brief reconciliation", "section gate", "section review" and "decision
    gates" are not defined.
  * "Drafting session" and "this session" clash with the MRI meaning of
    "session".
  * Line 36 says "the owner's decision" while the rest says "the author".
  * "EGM" is linked but never spelled out.
  * Q15 is used for two separate decisions.
  * The Decision Log is not in Q-number order, so Q references are hard to
    look up.

### CO-11. State index structure and adoption triggers are not explained - Severity: Nit (DetailedRange)
* **Affected Scope:** `sources` (1633), "source-record keys" (1025), "source
  upserts" (1481); prepare adoption (1633-1638); convert adoption
  (1692-1697); reconciliation adoption (1517-1519); FeatureRange 1089-1091.
* **Discrepancy Description & Rationale:** The index's top-level parts
  (sessions, sources, issues) are never described. There are three adoption
  paths. The feature section motivates only reconciliation adoption and
  convert adoption after index loss. It never says when a canonical ZIP would
  be missing from `sources` while an owned extraction exists, which is what
  triggers the prepare-side adoption.

### CO-12. Index-loading behavior differs from Q21 - Severity: Nit (DetailedRange)
* **Affected Scope:** Q21 (1084-1088) against `state.py` Load (1463-1468).
* **Discrepancy Description & Rationale:** Q21 raises an issue when
  `index.json` is missing or does not parse. The detailed plan raises
  `state:recovered` only on a parse failure; a missing index silently falls
  back to `.bak`. This may be intended, because a crash between the two flush
  renames leaves the index missing, but the document does not say so.

### CO-13. Migration apply order and rollback side effects are not explained - Severity: Nit (DetailedRange)
* **Affected Scope:** Q22 (972, 988-989, 995-996); Rollback (1302-1303);
  `migrate.py` (1747-1748, 1767-1774).
* **Discrepancy Description & Rationale:**
  * `migrate` replays the schema-2 journals in memory, then copies the on-disk
    `index.json` (without those journal events) to `index.v2.json`, then
    deletes the journals. Rolling back to `index.v2.json` would seem to drop
    those events.
  * An interruption between writing the schema-3 index and deleting the
    journals leaves old journals behind. Their names have no sequence number,
    so it is unclear whether schema-3 replay ignores them.
  * Neither effect is mentioned. The reviewer asks a correctness reviewer to
    confirm.

### CO-14. Marker file name pattern is ambiguous - Severity: Nit (DetailedRange)
* **Affected Scope:** Q4 (737) against `marker_for` (1587-1591) and
  `move_archive` (1607-1609).
* **Discrepancy Description & Rationale:** Q4 names markers `<name>.zip.done`.
  The detailed plan says `<name>.done` and `<canonical name>.done`. These
  agree only if `<name>` includes the `.zip` extension, and that is not
  stated.
