# Goldfish Review Report: Stage 9 (Implementation Readiness Test)

**Target:** `docs/designs/production-hardening.md` (DetailedRange, lines
1341-2152)
**Standards:** `egm-implementation-readiness` and `egm-codebase-details` used
`implementation-readiness-standard.md`; `egm-style` used its persona's own
criteria (no standard supplied).
**Mode:** Consolidated (uninterrupted), one round, no fixes applied. Findings
were deduplicated across the three reviewers. Where reviewers rated the same
problem differently, the highest severity is shown, and the Elephant notes
the disagreement.

## Reviewer Status

| Reviewer | Status | Raw result |
| --- | --- | --- |
| `egm-implementation-readiness` | completed | 1 Blocker, 11 Major, 10 Nit |
| `egm-codebase-details` | completed | 1 Blocker, 22 Major, 10 Nit |
| `egm-style` | completed | 0 Blocker, 10 Major, 13 Nit |

(`egm-implementation-readiness` reported "9 Nit" and `egm-codebase-details`
reported "20 Major"; the counts above are of the findings each actually
listed.)

**After deduplication:** 2 Blocker, 24 Major, 20 Nit.

**Verdicts:** `egm-implementation-readiness`: NOT READY.
`egm-codebase-details`: does not yet meet the standard. `egm-style`: tone is
dry and factual; findings are inconsistencies, overloaded terms and
over-broad claims, which matter because line 1345-1346 says "signatures,
schemas, and rule tables define the behavior".

**Already in good shape (per reviewers):**
* The module split and file list are complete; every baseline file is marked
  Create, Modify, Delete or "not changed".
* The classifier rule table (1537-1556) gives every Q11 expected label and
  passes the four existing `classify` assertions (subject to IR-23).
* Packaging is consistent (SPDX `license`, `license-files`, setuptools>=77,
  `.dist-info/licenses/`); `reports.py` does not trip
  `check_distribution.py`'s forbidden "Reports" path component.
* The 30-case count (15 + 15), test names in items 26-27, patch targets,
  helper locations, the `assert`-based `check_distribution.py`, README
  phrases quoted for replacement, and baseline references §4, §12.1, §16.3,
  §21 are accurate.
* Function names and signatures described as moved match the source, except
  `fast_valid` (IR-2).

**Elephant verification (claims checked against the source):**
* IR-1: `publish_tree` copies across devices with `shutil.copytree`
  (`pipeline.py:107-113`); the plan keeps the roll-forward rule unchanged.
* IR-2: the plan says `fast_valid(entry, audit=False)` (1441); the code is
  `fast_valid(entry, kind, audit=False)` (`pipeline.py:298`) with five callers
  passing `kind` (`:526`, `:597`, `:771`, `:854`, `:865`).
* IR-4: `fast_valid` rejects a descriptor whose `version` is not current
  (`pipeline.py:299`), descriptors carry `version` (`:322`), and the planner
  compares `sources[...].version` (`:596`). The plan says only "Set entry
  `version: 3`" (1755).
* IR-7: the plan's `flush()` says "rename `index.json` to `index.json.bak`"
  (1477), which fails on Windows when the target exists.
* IR-17: `test_output_root_override_does_not_reuse_other_root` expects exit 0
  and one volume in the new root (`tests/test_pipeline.py:186-192`).
* IR-18: `test_path_traversal_member_cannot_escape` writes the same object
  twice and asserts `n_dicom == 2` (`tests/test_pipeline.py:207-214`).
* IR-23: rule 6 reads "`ImageType` contains `P`" with "contains" defined as
  substring of a token (1535, 1546); the existing test expects `SWI` for
  `ORIGINAL\PRIMARY\M` (`tests/test_pipeline.py:254`), and today's code uses
  set membership (`pipeline.py:382, 392`).

---

## Prioritized Findings (Blockers -> Major -> Nits)

### IR-1. Reconciliation can publish a partly copied tree as complete, and several publish states have no rule - Severity: Blocker
* **Reviewer Lens:** `egm-implementation-readiness` (#1). Cross-stage: same
  root cause as CR-1 / ZZ-1.
* **Affected Scope:** `recovery.py` (1505-1523; intent schema 1505-1509);
  crash matrix (1985-1994); implements the Q12 table (662-673) unchanged.
  Code: `pipeline.py:108-113`, `:546-548`, `:811-817` (receipts written into
  the stage before `publish_tree`).
* **Discrepancy Description:**
  * The intent schema lists a `phase` field but never defines its values, who
    updates it, or how `reconcile` uses it.
  * With node-local `--staging-root` the stage-to-incoming rename fails with
    EXDEV and `copytree` runs; the receipt can land before the volumes. A
    SIGKILL mid-copy leaves `.incoming_*` with a receipt but missing NIfTIs,
    which roll-forward publishes and Adopt records as `converted`, breaking
    invariants 3 and 5.
  * Missing rows: destination present plus `.incoming_*` present with no
    `.previous_*` (a crash during reconversion or re-extraction, one of the
    plan's own crash points at 1987); `.incoming_*` without a receipt.
  * "No state record" is undefined when a reconversion was published over an
    already-recorded destination; the new folder is neither adopted nor
    rolled back and becomes an `output:` issue next run.
  * Adopting a prepare publish needs the canonical ZIP path,
    `archive_sha256`, `archive_signature` and a `sources` record; the intent
    fields (1509) carry none, so the planner re-treats the ZIP as new.
  * No rule says which command reconciles which intents; `prepare` does not
    lock the NIfTI root and `convert` does not lock the upload root
    (1401-1402), so reconciling every intent touches unlocked roots.
* **Rationale & Recommendation:** Define phase values (for example `staged` ->
  `incoming_complete` -> `swapped`, each written with `atomic_json`) and roll
  forward only from `incoming_complete` or after a metadata-only inventory
  check, or remove roll-forward as Stage 8 proposes (ZZ-1). Add the missing
  rows; define "no state record" as a receipt-ID mismatch; add the ZIP and
  source fields to prepare publish intents; reconcile only intents whose roots
  are in `cfg.lock_roots` and report the rest; add crash cases for a
  cross-device copy and the reconversion variants.

### IR-2. `fast_valid` signature misstated as "moved ... otherwise unchanged" - Severity: Blocker (codebase-details) / Nit (style, implementation-readiness)
* **Reviewer Lens:** `egm-codebase-details` (B1); `egm-style` (#13);
  `egm-implementation-readiness` (N1, part).
* **Affected Scope:** 1441-1442; `pipeline.py:298` and callers at 526, 597,
  771, 854, 865.
* **Discrepancy Description:** The plan writes `fast_valid(entry,
  audit=False)`. Every caller passes `"extract"` or `"convert"` as the second
  argument. Read literally, the step-0 move-only commit would shift `audit`
  into `kind`'s slot or silently change the signature.
* **Rationale & Recommendation:** Keep `kind` in step 0, or list its removal
  and the five call-site edits as a stated change in a later step.
* **Elephant note:** Verified. The highest-severity rule puts this at Blocker,
  but it is a one-line correction to the plan, not a design gap. The
  consolidated report treats it as a Major plan correction.

### IR-3. Steps depend on later steps, so the per-step checkpoints cannot be green - Severity: Major
* **Reviewer Lens:** `egm-implementation-readiness` (#2);
  `egm-codebase-details` (M16); `egm-style` (#12).
* **Affected Scope:** 1354-1370; 1622-1624, 1679, 1708, 1838-1844, 1852-1904,
  1933, 1975-1976, 2003. Code: `pipeline.py:997`; `tests/test_pipeline.py:39,
  52-59, 86, 122-125, 219-221`.
* **Discrepancy Description:**
  * Steps 4 and 5 write intents and carry intent IDs, but
    `recovery.write_intent` and the clearing in `accept` arrive in step 7.
  * `test_zip_without_marker_waits_nonblocking` expects exit 0 at step 4, but
    the nonblocking `wait:` prefix is in `reports.NONBLOCKING_PREFIXES`
    (step 8); today every non-`date:` issue is unresolved
    (`pipeline.py:997`).
  * Step 5 is verified by `test_conversion.py`, which includes
    `test_status_columns_and_last_attempt_reported` (a step-8 feature).
  * The `real-converter` CI job is added in step 1, but
    `tests/test_real_converter.py` arrives in step 10; pytest exits 4 on a
    missing file.
  * Step 5's strict coverage makes every existing conversion test `partial`:
    the fake converter hardcodes `series_1.2.3_...`
    (`tests/test_pipeline.py:86`), which never matches a real inventory key.
    After step 4 every test ZIP lacks a marker and waits.
  * Test-file edits (`.done` markers at 1904, the swap-name change at
    1914-1915, helper moves into `support.py`/`conftest.py`) have no step.
  * The step table's "Verified by" column misplaces tests: step 4 lists
    `--session` but `test_session_filter_limits_work` is in
    `test_conversion.py` (1975); step 5 lists "process groups" but that test
    is in `test_recovery.py` (2003, step 7); step 2's "test_recovery.py state
    tests" is unnamed.
* **Rationale & Recommendation:** Reorder, or state per step which functions
  are stubbed. Assign every test and helper edit (including the new fake
  converter and markers) to a step. Name the tests in each "Verified by" cell.

### IR-4. `migrate` leaves descriptor and source versions unaddressed, and the series marking has no location - Severity: Major
* **Reviewer Lens:** `egm-implementation-readiness` (#3);
  `egm-codebase-details` (M3); `egm-style` (#8). Elephant verified.
* **Affected Scope:** 1745-1764; 1449-1451. Code: `pipeline.py:299`, `:320-322`,
  `:557-558`, `:564-565`, `:574-575`, `:596`.
* **Discrepancy Description:**
  * The plan says only "Set entry `version: 3`". It never mentions the
    `version` inside `extraction`/`conversion` descriptors or the `sources`
    records. If descriptors stay at 2, every extraction fails `fast_valid`
    and is re-extracted, and every converted session gets a blocking
    `output:` issue under Q7. If `sources` stay at 2, every canonical ZIP is
    re-hashed on the first `prepare`. Either breaks CUJ 6 and Goals 5 and 6.
  * "Mark series `required = true` with `sop_classes = ["unknown"]`" has no
    target. Session entries hold no series rows; rewriting `.inventory.json`
    changes the extraction directory mtime and invalidates its receipt.
    `receipts.required_series` (1449-1451) already treats inventories
    without `sop_classes` as required, implying files are not rewritten.
  * Unspecified: sessions with no inventory (failed or interrupted); converted
    sessions with missing or unreadable sidecars; defaults for
    `accepted_multi_study`, `last_attempt` and `coverage` on unconverted
    sessions; whether rebuilt provenance goes into receipt files (changing
    the receipt signature) or only into state.
* **Rationale & Recommendation:** Give a field-by-field transform for session
  entries, descriptors, sources and issues, per session status. State that
  on-disk receipts and inventories are not modified and that "required" is
  derived at read time.

### IR-5. The `migrate` apply sequence is not crash-safe, and rollback loses journal events - Severity: Major
* **Reviewer Lens:** `egm-implementation-readiness` (#4, part);
  `egm-codebase-details` (M4); `egm-style` (#6 and its out-of-scope note).
  Cross-stage: ZO-10, CO-13, CR-12, ZZ-5.
* **Affected Scope:** 1467-1468, 1472-1473, 1745-1748, 1767-1774; rollback
  1302-1303; Goal 6 (178-183). Code: `pipeline.py:215-216`, `:231`,
  `:1002-1006`.
* **Discrepancy Description:**
  * Schema-2 journals are named `<time_ns hex>_<4 hex>.json` with no `seq` in
    the body. Interrupted between apply steps 2 and 3, a schema-3 index
    (`last_seq 0`) sits beside schema-2 journals. If `seq` is parsed from the
    name, the huge time_ns value replays every schema-2 event over the
    migrated index; because "a rerun on schema 3 is a no-op", it cannot be
    repaired.
  * Interrupted between steps 3 and 4, new history rows with the `reconciled`
    column are appended under the old header (the header is written only to
    an empty file).
  * Step 1 copies only the on-disk `index.json`, which excludes journal events
    migrate replayed in memory; step 3 deletes those journals, so rollback
    restores incomplete state.
  * The same `seq` handling is undefined for a 0.1.0 project with journals but
    no `index.json`.
  * `test_migrate_interrupted_then_rerun` does not name its interruption
    point.
* **Rationale & Recommendation:** Make each apply step resumable, or use the
  single `.state` directory swap from ZZ-5. Decide whether `index.v2.json`
  holds the replayed state or migrate refuses while journals are pending.
  Name the interruption points the test covers.

### IR-6. The `migrate` command's plumbing is unspecified - Severity: Major
* **Reviewer Lens:** `egm-implementation-readiness` (#4, part);
  `egm-codebase-details` (M2); `egm-style` (#17).
* **Affected Scope:** 1395-1403, 1739-1740, 1745-1748. Code:
  `pipeline.py:342`, `:347`, `:356-376`.
* **Discrepancy Description:**
  * `configure` reads `args.reference_csv`, `args.workers` and `args.limit`,
    which `migrate_parser` does not define, and derives the NIfTI root only
    from `--output-root` when phase is "convert", so `--nifti-root` would be
    ignored. The plan does not say whether migrate calls `configure`.
  * `migrate_parser` uses `--dicom-root`/`--nifti-root`, while `prepare` and
    `convert` use `--output-root` for those roots; no mapping is stated.
  * "Per the Q13 table (... `migrate`: all five)": the Q13 table (711-714) has
    no `migrate` row; the source is Q22 step 1. "Take all locks" does not say
    whether that is the five root locks plus `ProjectLock`.
  * "The accepted multi-study set" on `cfg` (1740) is never defined.
  * Unspecified: run ID and log setup; the required history row and which
    file it goes to given the rename; whether the legacy lock is taken;
    whether `--dry-run` locks; exit codes for lock busy, schema 1, no index
    and already schema 3; the dry-run output format.
* **Rationale & Recommendation:** Specify `migrate`'s configuration path,
  root mapping, locks, logging, history row, exit codes and dry-run output.

### IR-7. State load and flush edge cases are left open - Severity: Major
* **Reviewer Lens:** `egm-implementation-readiness` (#5, N3);
  `egm-codebase-details` (M5, M6); `egm-style` (#9). Cross-stage: CR-4,
  CO-12, ZZ-11. Elephant verified the "rename" wording.
* **Affected Scope:** 1463-1480, 1498, 1731, 1911-1913, 1990-1991; Q21
  (1084-1091). Code: `pipeline.py:63`, `:902`;
  `tests/test_project_configuration.py:89`.
* **Discrepancy Description:**
  * Uncovered: `index.json` unparseable with no `.bak` (first flush damaged);
    `index.json` missing with an unparseable `.bak`.
  * A missing `index.json` with a good `.bak` sets no recovery flag, but Q21
    treats missing and unparseable the same. This is the normal state after a
    kill between the two flush renames, so the choice decides whether that
    crash test expects exit 2.
  * After recovering from `.bak`, the next flush renames the corrupt
    `index.json` over the good `.bak`, then deletes journals by "the backup's
    `last_seq`" of a file that cannot be parsed.
  * First flush with no `index.json`: the deletion bound is unstated.
  * `flush()` says "rename"; `Path.rename` onto an existing `.bak` raises
    `FileExistsError` on Windows from the second flush on. The existing
    primitive uses `os.replace`.
  * No base class is given for `StateError` or `LockBusy`. The existing test
    expects `ValueError` matching `'Unsupported state version'`, and `main`
    catches only `ValueError`, `RuntimeError` and `OSError`.
* **Rationale & Recommendation:** Give a full decision table of index state x
  backup state. Move a corrupt index aside (for example
  `index.json.corrupt-<run_id>`) and use the in-memory `last_seq` for journal
  deletion. Say `os.replace`. Give the exception base classes and messages.

### IR-8. The raise/clear lifecycle of the new issue keys is undefined - Severity: Major
* **Reviewer Lens:** `egm-implementation-readiness` (#6);
  `egm-codebase-details` (M10). Cross-stage: CO-4, ZZ-11.
* **Affected Scope:** `state:recovered` (1466); `wait:<path>` (1634);
  `missing:<key>` (1640); `output:<key>` (1688, 1696); migrate's
  `convert:<key>` (1759-1761); the unnamed multi-study issue (1762); the
  unnamed Q12 "raise an issue" rows (666, 672); the HoldError issue
  (1584-1585). Code: `pipeline.py:516`, `:563`, `:569-570`, `:598-601`,
  `:612-616`, `:867-868`.
* **Discrepancy Description:** Today only stale `prepare:` keys,
  `convert:` keys on a cache hit, and `date:` keys on re-prepare are cleared.
  As written, these are never cleared: `state:recovered` (every later run
  exits 2); `output:` after a restore or a successful reconversion; `wait:`
  once the marker appears; `missing:` when the ZIP reappears; migrate's
  `convert:<key>`, which preparation's coverage re-evaluation (1625-1629)
  would have to clear from a different stage. The cached-branch clear at
  `pipeline.py:867-868` also silently removes the `convert:<key>` issue of a
  failed explicit reconversion on the next plain run; the plan does not say
  whether that is intended. For a migrated multi-study session,
  `--accept-multi-study KEY` never reaches `prepare_one`, because the ZIP is
  cached.
* **Rationale & Recommendation:** Add an issue table: key format, `stage`,
  `subject`/`session` fields (`plan_convert`'s blocked set depends on them,
  1698-1699), blocking or not, raised by, cleared by.

### IR-9. The handoff between preparation planning and the worker is incomplete - Severity: Major
* **Reviewer Lens:** `egm-implementation-readiness` (#7);
  `egm-codebase-details` (M7, M8, M14). Cross-stage: ZZ-3.
* **Affected Scope:** 1584-1585, 1592-1606, 1610-1642, 1736-1740. Code:
  `pipeline.py:515`, `:574-575`, `:596-600`, `:602-606`, `:883`, `:944`,
  `:960-962`; `tests/test_pipeline.py:245, 270`.
* **Discrepancy Description:**
  * Candidates are positional 5-tuples indexed by `process_subject`, the
    dry-run logger and tests. There is no field for the marker result, the
    adopt-candidate flag or the audit re-hash state, and the adopt path is
    missing from `prepare_one`'s ordered steps (1611-1629).
  * The refresh event "updates only `archive_signature`" (1617-1618), but the
    cache check also compares `sources[path].signature`, so the ZIP is
    re-hashed every later run. Whether `prepare_one` returns after the
    refresh, and what the outcome is called, are unstated.
  * `held`: `accept()` counts every outcome except duplicate, failed and
    interrupted as `completed`, so a hold is reported as completed. The
    planner short-circuits only `duplicate`/`failed`, so a held source is
    re-hashed and re-extracted every run; cached like `failed`, it is never
    re-planned for `--accept-multi-study` without `--retry-failed`. The
    current exception handler writes a `failed` session when there is no
    previous entry, contradicting "no session entry changes".
  * `--accept-multi-study KEY` has no defined path for a source cached as held
    or for a migrated, already-prepared multi-study session.
  * Whether the `--audit` canonical re-hash (1641-1642) runs in the planner
    (serial, coordinator thread) or in workers is unstated; a mismatch found
    in the planner is a plan-time issue, not a HoldError.
* **Rationale & Recommendation:** Specify the candidate structure, each event
  type's fields (`session`, `sources`, `issues`, `outcome`), counting, and
  planner caching for every outcome, including an acceptance event that needs
  no re-extraction.

### IR-10. Report status derivation and counting are unspecified; waiting ZIPs would force exit 2 - Severity: Major
* **Reviewer Lens:** `egm-implementation-readiness` (#10);
  `egm-codebase-details` (M9, M11).
* **Affected Scope:** 1633-1636, 1710-1718, 1933-1934; invariant 10 (350);
  interface (383). Code: `pipeline.py:281-288`, `:608`, `:945`, `:998`,
  `:1009`.
* **Discrepancy Description:**
  * `prepare_status` comes from the session entry and `conversion_status` from
    `conv.status`. Waiting ZIPs and HoldError create no session entry, so
    `waiting_for_marker`, `held` and `output_modified` must come from issues;
    the mapping is not given, nor how issues map to rows.
  * Where "partial (with reason)" shows its reason, and whether
    `required_series`, `converted_series` and `excused_series` are counts or
    lists (and with what separator), are unstated.
  * Every planning hold increments `counts["held"]`, and any nonzero `held`
    gives exit 2. The plan does not say where a waiting ZIP is counted;
    counted as held, it breaks `test_zip_without_marker_waits_nonblocking`.
    Q4 says waiting ZIPs are "counted in the run summary", but the history
    columns have no place for that count.
* **Rationale & Recommendation:** Define each status's source, the column
  formats, and a separate nonblocking counter for waiting ZIPs.

### IR-11. Upload-marker rules leave decisions open - Severity: Major
* **Reviewer Lens:** `egm-implementation-readiness` (#8);
  `egm-codebase-details` (M20, marker and `zip_move` parts); `egm-style` (#4,
  #7). Cross-stage: ZO-5, CR-2(c), CO-14, ZZ-2.
* **Affected Scope:** 1505-1509, 1515-1516, 1587-1591, 1607-1609, 1633-1638;
  Q4 (735, 760-761). Code: `pipeline.py:502-503`, `:564-565`, `:572-573`.
* **Discrepancy Description:**
  * "A non-legacy ZIP whose path is not in `sources` needs a marker" conflicts
    with Q4's upgrade note: pre-upgrade held, failed and duplicate ZIPs left
    in `Uploads/` are already in `sources`, yet Q4 says they need markers. A
    new ZIP re-uploaded to a previously failed path also bypasses the marker.
    "Otherwise" at 1634 grammatically points back to "needs a marker".
  * `<name>.done` (1588) and `<canonical name>.done` (1608) disagree with
    `<name>.zip.done` (381, 736-737, 246). An operator reading item 11 alone
    could create `S001_20260725.done`.
  * Unspecified: outcome for a malformed `.sha256`; a `.sha256` naming a
    different file; which marker wins when both exist; case sensitivity.
  * Marker rename has no order relative to the ZIP rename and no collision
    rule. The intent records marker pairs but no reconciliation rule uses
    them; a crash between the two renames leaves a canonical ZIP with no
    marker, a `wait:` issue forever, and an orphaned marker.
  * The `zip_move` intent has no root field, yet rule 1515 requires `owned()`
    against "the intent's root".
* **Rationale & Recommendation:** Pick one rule (for example "not in
  `sources`, or signature differs from the source record"), or gate only
  `Uploads/` as ZZ-2 proposes. Define the file-name pattern once with a
  literal example (`S001_20260725.zip.done`). Specify the `.sha256` outcomes
  and the marker move and reconciliation, or drop the move. Name each intent
  kind's root.

### IR-12. "Complete" and coverage re-evaluation are undefined; a retry on a migrated `partial` can overwrite the 0.1.0 folder - Severity: Major
* **Reviewer Lens:** `egm-implementation-readiness` (#9);
  `egm-codebase-details` (M13).
* **Affected Scope:** 1625-1629, 1675-1685, 1687-1701, 1757-1761. Code:
  `pipeline.py:817`, `:870-872`.
* **Discrepancy Description:**
  * "A `partial` caused only by missing series that are now excused returns to
    `converted`" has no test. Conversion records do not store the cause, and a
    migrated partial (folder in `Subjects/`, receipt complete) and a real
    partial (in `.pending/`) are distinguishable only heuristically.
  * "Complete session / conversion / record / folder" drive branches in
    `plan_convert` and `convert_one` step 6 but are never defined.
  * `migrate` records `partial` while pointing at the complete 0.1.0 folder.
    Today's retry rule selects it under `--retry-failed`. The protections
    (refusal at 1675-1678, last-good retention at 1680-1683) are worded for a
    "complete" record, which a `partial` is not. A successful retry runs
    `publish_tree` over the old folder and deletes added files (P10); a failed
    retry overwrites the record (P4).
* **Rationale & Recommendation:** Define "complete" once (for example, a
  receipt with `complete: true` at the canonical root), define the
  re-evaluation condition and its status, coverage and issue changes, and
  make the protections apply to migrated partials.

### IR-13. Adoption has no source for the record's required fields - Severity: Major
* **Reviewer Lens:** `egm-codebase-details` (M19);
  `egm-implementation-readiness` (#9, adoption part).
* **Affected Scope:** 1443-1448, 1517-1519, 1692-1697. Code:
  `pipeline.py:812-813`.
* **Discrepancy Description:** `conversion_record` requires `provenance`,
  `coverage`, `n_volumes`, `created_utc` and `run_id`. A 0.1.0 conversion
  receipt holds only version, `complete`, `source_sha256` and `receipt_id`,
  and `n_volumes` is in no file. Both adoption paths leave these values
  undefined; `prepared_entry`'s `accepted_multi_study` has the same gap. How
  adoption is counted and what a dry run does are also unstated.
* **Rationale & Recommendation:** Say how each field is derived during
  adoption (for example, count volumes and recompute coverage from sidecars,
  mark provenance as unknown), and state the event, counting and dry-run
  behavior.

### IR-14. SIGTERM handling scope is unspecified - Severity: Major
* **Reviewer Lens:** `egm-implementation-readiness` (#11). Cross-stage: CR-3,
  ZZ-6.
* **Affected Scope:** 1724-1735; CUJ 2 (252-260); interface (382). Code:
  `pipeline.py:902`, `:987`.
* **Discrepancy Description:** The plan does not say where the handler is
  installed. A SIGTERM during roster load, reconciliation, planning or
  planner-side `--audit` hashing raises `Terminated(BaseException)` outside
  the worker loop and escapes `main` (traceback, exit 1, not 143); whether the
  history outcome is `terminated` or `aborted` is unclear. A second signal
  during the `finally` drain or flush aborts it. Nothing bounds
  `pool.shutdown(wait=True)` against an in-flight `copytree` or publish.
* **Rationale & Recommendation:** Specify where the handler is installed, a
  one-shot handler, and the outcome for each phase of the run. ZZ-6's
  flag-only handler removes most of this.

### IR-15. The crash-injection matrix lacks a mechanism and per-case expected outcomes - Severity: Major
* **Reviewer Lens:** `egm-implementation-readiness` (#12);
  `egm-codebase-details` (M18, `run_with_crash` part).
* **Affected Scope:** 1871-1873, 1985-1994; Q12 (686-687).
* **Discrepancy Description:**
  * `run_with_crash(root, phase, crash_point, *args)` has no N parameter and
    no mapping from each boundary to a patched function and call count.
    `Path.rename` and `os.replace` are also used by `move_archive`,
    `atomic_json` and flush, so N depends on the fixture. Whether the crash is
    before or after the original call is unstated, and the list needs both.
  * "Prior valid state or its new result" is not a pass condition.
  * Missing cases: zip_move "both or neither"; adoption "digest mismatch or
    receipt missing"; stage removal; cross-device copy; marker move; migrate
    interruption; a kill during reconciliation for each rule.
* **Rationale & Recommendation:** Provide a table: case, command, fixture
  (first publish or reconversion), patch target and N, before/after, and the
  expected filesystem and state after the rerun.

### IR-16. The step-0 move map leaves existing code without a home - Severity: Major
* **Reviewer Lens:** `egm-codebase-details` (M1);
  `egm-implementation-readiness` (N1).
* **Affected Scope:** 1356, 1378-1383, 1385-1797, 1389. Code:
  `pipeline.py:37`, `:294-295`, `:620-675`, `:752-758`, `:915-1009`;
  `cli.py:6`; `tests/test_pipeline.py:260, 299, 308`.
* **Discrepancy Description:** Not assigned to any module: `problem()`;
  `run()` with its nested `accept` closure and the run_history writer; where
  `VERSION` lives at step 0 (the plan defines only `SCHEMA_VERSION = 3`, which
  step 0 must keep at 2, and `test_1000` references `sp.VERSION`); where
  `sanitized_nifti` and `redact_diagnostic` sit at step 0
  (`test_pipeline.py:260` calls `sp.sanitized_nifti`). `cli.py:6` (`from
  .pipeline import main as run_phase`) must be retargeted in step 0, or
  deleting `pipeline.py` breaks the CLI. The `cfg` attribute names for
  `--session` and `--accept-multi-study` are not given.
* **Rationale & Recommendation:** Complete the move map for every top-level
  name in `pipeline.py`, including `cli.py`, and state the step-0 schema
  constant.

### IR-17. `plan_convert` does not handle a different output root, and an existing test breaks - Severity: Major
* **Reviewer Lens:** `egm-codebase-details` (M12). Elephant verified the test.
* **Affected Scope:** 1687-1689, 1883-1904. Code: `pipeline.py:865`;
  `tests/test_pipeline.py:186-192`.
* **Discrepancy Description:** Today a converted session whose `conv.root`
  differs from the configured NIfTI root is converted into the new root, and
  `test_output_root_override_does_not_reuse_other_root` expects exit 0 and one
  volume there. The new "complete session, not forced" rule does not separate
  "receipt for another root" from "invalid receipt at this root"; read
  literally it gives `output:<key>` and exit 2. Item 26 does not list this
  test as changing.
* **Rationale & Recommendation:** Add the other-root case to `plan_convert`,
  or list the test change.

### IR-18. "Repeats are not written" conflicts with the streaming copy, and an existing test breaks - Severity: Major
* **Reviewer Lens:** `egm-codebase-details` (M15). Elephant verified the
  test. Cross-stage: ZO-9.
* **Affected Scope:** 1594-1597, 1883-1904. Code: `pipeline.py:457-468`;
  `tests/test_pipeline.py:207-214`.
* **Discrepancy Description:** `extract_archive` hashes while writing, so the
  digest is known only after the bytes are on disk, and file numbering uses
  `len(files)+1`. The plan forces an unstated choice: read twice, or write
  then unlink, and how numbering behaves. `test_path_traversal_member_cannot_escape`
  packs the same object twice and asserts `n_dicom == 2`; under
  deduplication that may become 1, and item 26 does not list the test.
* **Rationale & Recommendation:** State the mechanism and numbering, and list
  the test change (or define `n_dicom` as members, not unique objects).

### IR-19. Cache-budget instrumentation misses metadata calls - Severity: Major
* **Reviewer Lens:** `egm-codebase-details` (M17);
  `egm-implementation-readiness` (N6).
* **Affected Scope:** 2037-2040. Code: `pipeline.py:320`, `:413`, `:587`.
* **Discrepancy Description:** Counting only `os.stat`, `os.scandir` and
  `os.listdir` misses `os.lstat` from `Path.is_symlink` (discover and
  `plan_prepare`). On recent Python versions on Windows, `os.path` predicates
  use C fast paths that bypass a patched `os.stat` (the reviewer is
  moderately confident about the exact versions). `Path.open`/`read_text` go
  through `io.open`, not `builtins.open`. The per-session formula (total
  divided by sessions, with or without fixed overhead) is undefined.
* **Rationale & Recommendation:** Specify patch targets per Python version in
  the CI matrix, and the formula.

### IR-20. Test helper contracts are underspecified or overstated - Severity: Major
* **Reviewer Lens:** `egm-codebase-details` (M18, rest);
  `egm-implementation-readiness` (N7); `egm-style` (#21).
* **Affected Scope:** 1857-1873, 1904, 2050-2054. Code:
  `tests/test_pipeline.py:32, 52-59, 93, 167`; `pipeline.py:109, 115, 117,
  120, 511`.
* **Discrepancy Description:**
  * `archive(root, relative, members, marker=...)`: the type of `members`,
    whether today's ancillary members (`ddv/viewer.dll`, `preview.jpg`) are
    kept, and the default `marker` are not given.
  * `dicom_bytes` "gains" `series_uid`/`study_uid`, but `series`/`study`
    parameters already exist.
  * The fake converter's exit-code switch does not say whether it keeps
    `FAKE_CONVERTER_EXIT`; its sidecars need `ConversionSoftwareVersion` for
    the provenance test; there is no synthetic recipe for the "unconvertible
    series" or the CSA Non-Image object.
  * "So the real converter accepts the output" (1859-1860) is untested;
    "intended to ...; confirmed by `test_real_converter.py`" is accurate.
    Baseline §12.1 covers the index and receipts, not inventories or sidecars.
* **Rationale & Recommendation:** Give each helper's full signature, defaults
  and behavior, and calibrate the claims.

### IR-21. "Every step's regression tests fail on the step-0 code" is too broad - Severity: Major
* **Reviewer Lens:** `egm-style` (#2); `egm-codebase-details` (M21).
  Cross-stage: CR-27.
* **Affected Scope:** 1369-1370, 1925-1926, 1935-1936, 2016-2018; table header
  2112; rows P2 (2115) and P9 (2122). Code: `pipeline.py:139-140`,
  `:176-199`, `:409-410`, `:898-901`.
* **Discrepancy Description:** `test_dry_run_takes_no_lock`,
  `test_legacy_project_lock_still_excludes`,
  `test_legacy_import_needs_no_marker` and (if it uses `parse_name`)
  `test_session_key_grammar` check preserved behavior and pass on step 0.
  Tests that fail only because a new function does not exist yet are not
  evidence of a defect. The header "fails at step 0, passes after phase 1"
  does not fit P2 ("Deliberately open") or P9 (CI jobs and a script).
* **Rationale & Recommendation:** Limit the claim to the P-item tests in the
  traceability table, label preservation tests as such, and relabel the P2
  and P9 rows.

### IR-22. The README update list leaves contradicted claims in place - Severity: Major
* **Reviewer Lens:** `egm-codebase-details` (M22).
* **Affected Scope:** 2061-2081. Code: `README.md:121-122, 180, 182, 183,
  185-187, 247-249`.
* **Discrepancy Description:** These README claims would remain: run_history
  as "one compact row per completed/interrupted invocation" (now also
  `aborted`, plus `reconciled`); the session_status fields; the `.state`
  contents (now also `.bak` and `intents/`); date-mismatch warnings as the
  only nonblocking issue (now also `wait:` and `missing:`); "interrupted ...
  work is retried automatically" (not for explicit reconversion); "State
  schema version 2 uses ... `.mri_dicom_pipeline.lock`".
* **Rationale & Recommendation:** Add each to item 36's update list.

### IR-23. "contains" has two meanings in the classifier rules - Severity: Major
* **Reviewer Lens:** `egm-style` (#1). Elephant verified.
* **Affected Scope:** 1533-1536, 1546, 1549; existing test kept by item 26
  (1900-1901). Code: `pipeline.py:382, 392`; `tests/test_pipeline.py:254`.
* **Discrepancy Description:** *contains* is defined as "the pattern is a
  substring of a token". Rule 6 says "`ImageType` contains `P` or `PHASE`".
  With the defined meaning, `ORIGINAL\PRIMARY\M` matches (`PRIMARY` contains
  `P`) and the series becomes `SWI_PHASE`, but the kept test expects `SWI`.
  Rule 9's "the raw text contains `+C`" is a third meaning.
* **Rationale & Recommendation:** Use a separate term, for example "an
  `ImageType` value equals `P` or `PHASE`", and define the raw-text match.

### IR-24. Two different `--dry-run` help texts - Severity: Major
* **Reviewer Lens:** `egm-style` (#3).
* **Affected Scope:** 1392-1393 against 379; test at 1949-1950, 2120.
* **Discrepancy Description:** "Plan only: no project writes or converter
  calls; hashes files only when combined with --audit" against "no writes or
  converter calls; hashes only with `--audit`".
  `test_dry_run_audit_hashes_but_writes_nothing` asserts the help text, and
  "no project writes" and "no writes" promise different scopes.
* **Rationale & Recommendation:** Pick one string and quote it in both places.

### IR-25. Migrated provenance overstates what was applied - Severity: Major
* **Reviewer Lens:** `egm-style` (#5); `egm-codebase-details` (M20,
  provenance part); `egm-implementation-readiness` (N4). Cross-stage: ZO-8.
* **Affected Scope:** 1583, 1670-1672, 1762-1764; Q22 (985-987). Code:
  `pipeline.py:777-779`.
* **Discrepancy Description:** "Revision 1 for everything" gives migrated
  sessions SOP-list revision 1, the same value as new outputs, although
  0.1.0 had no SOP-class list and Q22 gives revision 1 only to the classifier
  and sanitizer. The "fixed 0.1.0 argument list" includes per-session `-o`
  and input paths and is not written out; the 0.1.0 converter path was never
  recorded. "Argument list after the executable" includes per-run stage
  paths, and for `.py` converters the executable is `sys.executable`.
* **Rationale & Recommendation:** Name each revision field and its migrated
  value, write out the argument template, and say how per-run paths and
  unknown converter paths are recorded. ZO-8's "package version only" removes
  most of this.

### IR-26. A hypothesis is asserted as a result, with no fallback decision - Severity: Major
* **Reviewer Lens:** `egm-style` (#10). Cross-stage: CR-14, ZZ-10.
* **Affected Scope:** 2050-2052; Q19 (551-552).
* **Discrepancy Description:** `test_real_converter_with_non_image_object_converts`
  asserts the session is `converted`, while Q19 calls this "a hypothesis to
  test". If dcm2niix exits nonzero when non-image objects are present, the
  Q19 design fails and Goal 8 ("no further design decisions") becomes false.
* **Rationale & Recommendation:** At this test, state the decision a failure
  triggers (for example "stop and reopen Q19").

### IR-27. `clear_intents` arity and the "intent's root" - Severity: Nit
* **Reviewer Lens:** `egm-implementation-readiness` (N2);
  `egm-codebase-details` (N1); `egm-style` (#15).
* **Affected Scope:** 1511, 1515-1516, 1737.
* **Discrepancy Description:** Defined as `clear_intents(reports_root, ids)`,
  called as `recovery.clear_intents(event.get("intents", []))`.
* **Rationale & Recommendation:** Make them agree (the root-field part is in
  IR-11).

### IR-28. `runner.main` loses `argv`'s default - Severity: Nit
* **Reviewer Lens:** `egm-codebase-details` (N2).
* **Affected Scope:** 1724. Code: `Scripts/*.py:11`; `pipeline.py:892`.
* **Rationale & Recommendation:** Keep `main(phase, argv=None)`; the launchers
  call `main("prepare")`.

### IR-29. Reuse existing code instead of duplicating it - Severity: Nit
* **Reviewer Lens:** `egm-codebase-details` (N3). Cross-stage: ZO-11.
* **Affected Scope:** 1433, 1491-1499, 1748. Code: `pipeline.py:176-202`,
  `:215-228`.
* **Rationale & Recommendation:** `RootLocks` should compose `ProjectLock`
  and translate its `RuntimeError` into `LockBusy`. Migrate's schema-2 replay
  should reuse `State.apply` and the sorted-glob replay. `fsutil.utc_now()`
  is `time.time()`.

### IR-30. The MANIFEST change is redundant - Severity: Nit
* **Reviewer Lens:** `egm-codebase-details` (N4).
* **Affected Scope:** 1820.
* **Rationale & Recommendation:** With setuptools >= 77, `license-files`
  already puts LICENSE and NOTICE in the sdist. Harmless.

### IR-31. The `twine` floor may be too low - Severity: Nit
* **Reviewer Lens:** `egm-codebase-details` (N5). Not verified (the reviewer
  could not run it).
* **Affected Scope:** "dependencies unchanged"; `pyproject.toml:24`
  (`twine>=5,<7`).
* **Rationale & Recommendation:** Older twine releases are reported to fail
  `twine check` on Metadata-Version 2.4 with PEP 639 `License-File` fields.
  Consider `twine>=6.1` after checking.

### IR-32. The Windows stop grace period changes without being flagged - Severity: Nit
* **Reviewer Lens:** `egm-codebase-details` (N6).
* **Affected Scope:** 1655-1657. Code: `pipeline.py:791` (10 s today; the
  plan says 5 s).
* **Rationale & Recommendation:** List it as a change.

### IR-33. Filtered converter output in `latest_convert.log` is unaddressed - Severity: Nit
* **Reviewer Lens:** `egm-codebase-details` (N7).
* **Affected Scope:** convert plan. Code: `pipeline.py:800`, `:963-968`;
  `README.md:179`.
* **Rationale & Recommendation:** Say whether `event["detail"]` and the
  handler copying it into `latest_convert.log` are kept.

### IR-34. Provenance needs another return value - Severity: Nit
* **Reviewer Lens:** `egm-codebase-details` (N8).
* **Affected Scope:** 1670-1671. Code: `pipeline.py:749`.
* **Rationale & Recommendation:** `organize_conversion` returns `(volumes,
  files, errors)`; the plan adds only `converted_keys`, but provenance also
  needs sidecar `ConversionSoftwareVersion` values.

### IR-35. The module name `state` collides with existing locals - Severity: Nit
* **Reviewer Lens:** `egm-codebase-details` (N9).
* **Affected Scope:** 1372-1374, 1862. Code: `pipeline.py:273, 581, 839, 925`.
* **Discrepancy Description:** With `from . import state`, `state =
  state.State(...)` raises UnboundLocalError inside functions that use a local
  `state`; it also collides with the `support.state` test helper.
* **Rationale & Recommendation:** Rename the module or the locals, or import
  `State` directly in those modules.

### IR-36. CI and interface consistency - Severity: Nit
* **Reviewer Lens:** `egm-codebase-details` (N10); `egm-style` (#20, #23
  part). Cross-stage: ZO-12.
* **Affected Scope:** 1718, 1838-1849, 2063-2081; interface table 384; Q2
  (1142-1143).
* **Discrepancy Description:** 2 OS x [OLDEST, NEWEST] is four real-converter
  jobs; Q2 says two, and `OLDEST = NEWEST` produces duplicate entries. The job
  does not install the package before pytest. The "README support table"
  belongs to phase 4 and is missing from item 36. The `terminated` history
  outcome is missing from the interface table.
* **Rationale & Recommendation:** Align the counts, add the install step, and
  list `terminated`.

### IR-37. Lock edge cases - Severity: Nit
* **Reviewer Lens:** `egm-implementation-readiness` (N5). Cross-stage: CR-46.
* **Affected Scope:** 1491-1499.
* **Discrepancy Description:** A root equal to the project root makes the
  process conflict with its own `ProjectLock`, and `configure` does not check
  for it. The order of owner-file removal versus unlock is unstated.

### IR-38. Contract-to-test gaps - Severity: Nit
* **Reviewer Lens:** `egm-implementation-readiness` (N8); `egm-style` (#22).
* **Affected Scope:** 2039-2040, 2140-2142; invariants 4 and 9 (349);
  1596-1597; phase map T02 (409).
* **Discrepancy Description:** No test for companion byte identity, the
  missing-SOP-Instance-UID failure, T02 malformed CSV quoting, `--session` on
  `prepare`, or an unknown `--session` key. "Every phase 1 row ... has at
  least one named, passing test" ignores that invariant 4's evidence includes
  a site check. "At most 20 for `prepare` and at most 20 for `convert`" can be
  "at most 20 per command".

### IR-39. Geometry tag JSON types are unspecified - Severity: Nit
* **Reviewer Lens:** `egm-implementation-readiness` (N9). Moot if geometry
  capture is deferred (ZO-2, ZZ-9).
* **Affected Scope:** 1601-1603.

### IR-40. Unlocated artifacts - Severity: Nit
* **Reviewer Lens:** `egm-implementation-readiness` (N10).
* **Affected Scope:** 1367, 2088, 2147-2148.
* **Discrepancy Description:** The README-to-test checklist has no named
  location. The runbook's two-node lock check has no defined steps, although
  it is the only evidence for invariant 4 on shared storage.

### IR-41. Decision history in instructions, and an unclear "That" - Severity: Nit
* **Reviewer Lens:** `egm-style` (#11).
* **Affected Scope:** 1343-1344, 1350-1352.
* **Rationale & Recommendation:** Drop "as the author decided". Rewrite "That
  was the recorded risk of the restructure option" as "This addresses the
  recorded risk of mixing refactoring and behavior changes in one diff."

### IR-42. Mislabels in the `fsutil` and `receipts` descriptions - Severity: Nit
* **Reviewer Lens:** `egm-style` (#14).
* **Affected Scope:** 1427-1432, 1447-1448.
* **Rationale & Recommendation:** `publish_tree` is listed under "Added" but is
  moved and changed. Write "includes the `receipt_info` fields" instead of
  "adds `receipt_info`".

### IR-43. Imprecise wording in `convert.py` - Severity: Nit
* **Reviewer Lens:** `egm-style` (#16). Cross-stage: CO-2 ("control files").
* **Affected Scope:** 1655, 1678, 1681, 1694-1695, 1700-1701.
* **Rationale & Recommendation:** "On a stop request or timeout:"; name
  `.pipeline_complete.json` and `.inventory.json` instead of "control files";
  use the status names `failed`, `partial`, `interrupted`; "if `complete` is
  true and `source_sha256` equals the prepared `content_sha256`"; "Selection
  of failed, partial, and interrupted sessions."

### IR-44. Uncalibrated "small"; operator messages omit the command - Severity: Nit
* **Reviewer Lens:** `egm-style` (#18).
* **Affected Scope:** 1749, 1759-1762.
* **Rationale & Recommendation:** "one inventory read per session" (a large
  session's inventory is not small). The issue text should name `prepare
  --session KEY --force-reextract` (CUJ 6 at 296, Q22 at 1002). Name the
  multi-study hold's issue key.

### IR-45. Drafting provenance and an unclear referent in the NOTICE paragraph - Severity: Nit
* **Reviewer Lens:** `egm-style` (#19).
* **Affected Scope:** 1811-1815.
* **Rationale & Recommendation:** Drop "taken from `git config user.name`".
  "It replaces the name" makes the employer the subject; say who edits the
  file. State whether "/" in "mriDICOMpipeline / Copyright 2026 Levi Davis" is
  literal or a line break. Move the ownership question to the phase-4 G07
  checklist.

### IR-46. The `.gitignore` claim holds only for default roots - Severity: Nit
* **Reviewer Lens:** `egm-style` (#23, part).
* **Affected Scope:** 2101-2104. Code: `.gitignore` line 7;
  `README.md:242-244`.
* **Rationale & Recommendation:** Say that custom roots inside the checkout
  need their own rules.
