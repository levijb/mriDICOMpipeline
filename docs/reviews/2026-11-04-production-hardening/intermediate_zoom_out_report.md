# Goldfish Review Report: Stage 8 (Intermediate Zoom Out)

**Target:** `docs/designs/production-hardening.md` (full file, lines 1-2152)
**Standard:** `zoom-out-standard.md`
**Mode:** Consolidated (uninterrupted); the target has not changed since
Stages 5-7.
**Task given to the reviewer:** judge the additions and fixes that Stages 6-7
recommend (adopt, shrink or reject each), and report simplicity problems
Stage 5 missed.

## Reviewer Status

| Reviewer | Status |
| --- | --- |
| `egm-zoom-out` | completed (the first attempt stopped at an API usage limit; the same reviewer was resumed and finished) |

**Severity breakdown:** 1 Blocker (carried over from CR-1), 9 Major, 4 Nit.
Three findings are new problems Stage 5 missed (ZZ-2, ZZ-3, ZZ-11). The
reviewer disagrees with one Stage 5 fix (ZO-5) and adds caveats to ZO-4,
ZO-6 and ZO-10.

**Reviewer's overall verdict:** Not ready to approve; the CR-1 Blocker
stands. Most Stage 6-7 findings describe real problems, but many of the
proposed fixes add mechanism the design does not need. In the reduced forms
below, the earlier findings make the design smaller, not larger.

**Coverage:** persona, standard, the whole target, all three earlier reports,
`concepts_brief.md`, baseline spec §12-§20 plus its heading list, all of
`pipeline.py`, the test list in `tests/test_pipeline.py`, and targeted
searches of README.md and the baseline spec. Not read (no finding depends on
them): `cli.py`, `__init__.py`, `__main__.py`, `Scripts/`,
`test_project_configuration.py`, `check_distribution.py`, `ci.yml`,
`pyproject.toml`, `MANIFEST.in`, `examples/sessions.csv`, the manifest JSON.
`design_draft.md` was not read. No tests were run.

## Prioritized Findings (Blockers -> Major -> Nits)

### ZZ-1. Fix the CR-1 Blocker by removing roll-forward, not by adding a copy protocol - Severity: Blocker
* **Reviewer Lens:** `egm-zoom-out`
* **Responds to:** CR-1, CR-2(a)(b)(d), ZO-4, CO-5, CO-11.
* **Affected Scope:** Q12 table (662-673, especially 669-672) and safety rules
  (683-687); Q21 lost-index adoption (1088-1091); `recovery.reconcile`
  (1513-1523); `plan_prepare` adopt candidate (1633-1638); `plan_convert`
  unreferenced folder (1692-1697); `fsutil.publish_tree` (1430-1432); T07
  crash points (1985-1994).
* **Discrepancy Description:**
  * **CR-1 is right, and affects more than it says.** `publish_tree` falls
    back to `copytree` into `.incoming_*` (`pipeline.py:107-113`). This also
    affects `prepare`, whose stage is in the same staging root
    (`pipeline.py:518`). A half-copied extraction that is rolled forward would
    give dcm2niix a subset of slices, which the series-coverage check cannot
    detect.
  * **CR-1's fix is too heavy:** a third swap-folder name, an fsync step,
    pre-roll-forward size checks, a receipt field, and crash points inside the
    copy.
  * **Roll-forward is not needed.** It only saves rework on a first publish,
    and rollback is always safe because inputs are kept.
* **Rationale & Recommendation:** Replace the ten-row table with four rules:
  1. The destination has a receipt that state does not record: apply the
     single adoption rule, then remove `.previous_*`. If state already records
     that receipt, clear the intent and remove `.previous_*`.
  2. Destination missing and `.previous_*` present: rename it back.
  3. Otherwise: remove `.incoming_*` and replan.
  4. Anything else: touch nothing and raise an issue (CR-2(b)'s else-branch).

  In every case, remove the intent's stage unless an issue refers to it.

  **One adoption rule (ZO-4), called from reconciliation, `plan_prepare` and
  `plan_convert`:** adopt only if the receipt matches the prepared content and
  every inventory file is present at its recorded size (stat-only,
  recovery-only, so Goal 5 holds). Rebuild the descriptor with the existing
  `receipt_info`. Recompute coverage with the planned
  `receipts.output_series_keys`, so the receipt needs no new field. The
  trigger "state does not record this receipt" also covers CR-2(a) without a
  new intent field. `publish_tree` stays unchanged. Add one lock rule
  (CR-2(d)): a command reconciles only intents whose roots it has locked.

### ZZ-2. [new] Markers must gate only `Uploads/` before the `zip_move` intent can go; ZO-5's "do not move markers" makes things worse - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Responds to:** ZO-4, ZO-5 (disagrees), CR-2(c), CR-4, CO-14.
* **Affected Scope:** Q4 (742-744, 756-757); interface row 381; Q12 ZIP-move
  rows (664-666); intent schema (1505-1509); `marker_for` (1587-1591);
  `move_archive` (1607-1609); `plan_prepare` (1633-1638); marker tests
  (1933-1936).
* **Discrepancy Description:**
  * **The crash window.** `prepare_one` extracts to the stage, moves the ZIP,
    then publishes (`pipeline.py:530-548`), and saves the event only after
    publication. A crash in between leaves a canonical ZIP with no `sources`
    entry.
  * **Every proposed fix either leaves the ZIP stranded or adds mechanism:**
    * Current design: the ZIP is rescued only if its marker also moved;
      CR-2(c) shows the gap between the two renames strands it.
    * ZO-5 ("do not move the markers"): the canonical ZIP never has a marker,
      so every such crash leaves it `waiting for marker` forever.
    * ZO-4 (drop the `zip_move` intent): deletes the only record of the move.
    * CR-2(c) (record markers in the intent): adds rows and keeps the intent
      kind.
* **Rationale & Recommendation:** Markers gate only `Uploads/`, where
  transfers are in flight. A ZIP in `SubjectsSorted/` that state does not
  know is planned as normal work; if an owned extraction exists for its key,
  the ZZ-1 adoption check runs first. Then:
  * Nothing moves with the ZIP.
  * The canonical-name handling in `marker_for`, `test_marker_moves_with_zip`
    and the `zip_move` intent can all go.
  * The crash window repairs itself by replanning.
  * A marker left in `Uploads/` is harmless: a later upload with that name is
    a duplicate or a conflict for an existing session (Q3/Q5).
  * CR-4's asymmetry disappears (ZIPs moved with their markers would otherwise
    be fully re-extracted after state loss).

  **Cost:** new ZIPs placed directly in `SubjectsSorted/` lose the readiness
  gate, as in 0.1.0; document "upload through `Uploads/` with a marker". This
  keeps the owner's required-marker decision where it matters; optional
  markers (ZO-5) remain a further simplification.

### ZZ-3. [new] Migration's multi-study hold has no clearing path short of re-extraction - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** Q22 step 4 (983-984); `migrate.py` (1762); Q20
  (571-576); `extract_archive` (1592, 1604-1605); `plan_prepare`
  (1630-1642); test at 2028.
* **Discrepancy Description:** `migrate` gives every schema-2 session with
  `study_count > 1` a blocking hold and says `--accept-multi-study` clears it.
  The plan applies the acceptance only inside `extract_archive`, and a cached
  ready session gets no task, so `prepare --accept-multi-study KEY` does
  nothing. Only `--force-reextract` clears the hold: the same high cost as
  ZO-6, and it fails if the canonical ZIP is missing. Every run exits 2
  meanwhile.
* **Rationale & Recommendation:** Make accepting an already-prepared session a
  state-only event (set `accepted_multi_study`, clear the hold). One test, no
  new option.

### ZZ-4. Clear migrated false `partial` sessions by reading headers of the extracted files; reduce CR-7, CR-8 and CR-9 - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Responds to:** ZO-6, ZO-9, CR-7, CR-8, CR-9.
* **Affected Scope:** CUJ 6 (289-298); Q22 (963-1009); Q20 digest note
  (577-581); `prepare_one` step 5 (1625-1629); `migrate.py` (1742-1774);
  runbook item (2090); Alternatives (1266).
* **Discrepancy Description:**
  * **Agrees with ZO-6/CR-7, with one caveat.** Schema-2 file rows carry no
    series key (`pipeline.py:468`), so the backfill must read the header of
    every object in a flagged session, not only the missing series. It is
    still strictly cheaper (no ZIP hash, no re-extraction, no republish) and
    works where the design's remedy fails: missing canonical ZIP, multi-study
    sessions, conflicting repeated instances.
* **Rationale & Recommendation:**
  * Run the backfill inside `migrate` for flagged sessions; `migrate` already
    holds every lock and reads the sidecars in step 3. This removes
    `prepare_one` step 5 (and its read of the unlocked NIfTI root),
    `--session` on `prepare`, and one runbook procedure.
  * Trade-off: an interrupted `migrate` restarts the backfill, so the dry run
    must report affected session and object counts (the one CR-7 item to
    adopt).
  * Reject from CR-7: the distinct nonblocking status and batch
    `--force-reextract`.
  * CR-8: the conditional `source_sha256` rewrite is moot under ZO-9 (digest
    unchanged). CR-8's second question still needs one sentence: what happens
    to a migrated `needs_conversion` session with a complete folder? Only
    pre-upgrade P3 replacements can produce one, since Q3 prevents new ones.
  * CR-9: report swap-named siblings in the dry run (one listing per subject
    directory, once). Do not apply Q12 rules to them: no intent exists, and
    their contents can be rebuilt from canonical ZIPs and extractions.
  * **Owner decision needed:** Q22's "no DICOM reads" title would change.

### ZZ-5. One `.state` directory swap replaces migrate's apply steps and the rollback copies - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Responds to:** ZO-10, CR-12, CO-13, CR-47 (journal part).
* **Affected Scope:** Goal 6 (178-183); schema row 369; Q22 (970-997); NFR
  Rollback (1302-1307); `migrate.py` (1747-1748, 1767-1774); `reports.py`
  (1717-1718).
* **Discrepancy Description:** `migrate` copies the index, writes the new
  index, deletes journals and rotates the history file; rollback copies two
  files back. CR-12 shows rollback leaves schema-3 journals for 0.1.0 to
  replay, and ZO-10 shows the interruption windows.
* **Rationale & Recommendation:**
  * `migrate` builds schema-3 state in a sibling directory, renames `.state`
    to `.state.v2`, then renames the new directory into place. A rerun
    completes a half-done swap. `prepare` and `convert` refuse while
    `.state.v2` exists without `.state`. Rollback renames the directories
    back.
  * This removes the journal-deletion step, the copy-back steps, the window
    where schema-2 journals sit beside a schema-3 index, and the history
    rotation (if the reconciled count is logged rather than added as a
    column, per ZO-10).
  * Refinement to ZO-10: replay schema-2 journals only if newer than the
    index, comparing file mtimes, not names. Names come from the compute
    node's clock; mtimes come from the file server's.
  * CR-47: one sentence putting power loss out of scope, matching baseline
    §12.2. Reject the added directory fsyncs.
  * Keep from CR-12: one rollback test and the runbook note on which P3/P10
    behaviors return under 0.1.0.

### ZZ-6. Shutdown: adopt the stop flag and drop work after stop; reject per-step time budgets - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Responds to:** CR-3, CO-7.
* **Affected Scope:** CUJ 2 (252-260); Q12 (692-694); Q15 shutdown row (816);
  `runner` signals (1732-1735); `run_converter` (1650-1658); `convert_one`
  (1663-1685); test at 2003.
* **Rationale & Recommendation:**
  * **Adopt:** the handler only sets the stop event. This removes
    `Terminated(BaseException)` and also fixes Ctrl+C, since a
    `KeyboardInterrupt` can land mid-flush today.
  * **Adopt:** after stop, keep the stage and record `interrupted`; skip
    organize, gzip, hashing and publishing. This removes work.
  * **Adopt, narrowed:** classify as `interrupted` only for the stop flag or a
    SIGTERM/SIGKILL return code. A crash signal such as SIGSEGV stays
    `failed`; interrupted sessions are retried automatically (617-619), so a
    crashing converter would otherwise retry on every run.
  * **Reject:** per-step budgets and the large-output timing harness. With
    post-stop work gone, shutdown is the 5 s group stop, draining events whose
    journals are already written, and the history row. The final flush and
    report rewrite can be best-effort because journals keep state
    recoverable.
  * Phase-1 test: assert exit 143 and that the converter group is gone. Move
    the numeric 20 s budget to Q15 in phase 2 (resolves CO-7).
  * Also adopt the `sbatch --signal` runbook line; it costs nothing.

### ZZ-7. Missing state: no identity files and no new option - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Responds to:** CR-4, CO-12.
* **Affected Scope:** Q21 (1088-1091); `RootLocks` (1491-1492);
  `plan_prepare` (1633-1638); Q23 refusal (1016-1018).
* **Rationale & Recommendation:**
  * CR-4's harm is cost, not loss: inputs are kept, and outputs are adopted,
    not replaced.
  * ZZ-2 removes most of that cost. Once an owned extraction is checked before
    any marker rule, empty state costs one hash per ZIP and republishes
    nothing. When the operator returns to the right report root, every
    descriptor is still valid, so the "second full re-preparation" disappears.
  * **Reject** identity files in every root and the new-project/adopt option.
    They would be a second detector for the wrong-root condition that the Q23
    refusal already covers when state is not empty. They are also the
    mechanism the rejected root-relative Q23 alternative needed; if ever
    added, revisit Q23 instead of carrying both.
  * **Adopt:** specify the four missing/corrupt index combinations (ZZ-11) and
    record the remaining cost of a mistyped root as accepted.

### ZZ-8. Operator overrides: reuse `--audit` for CR-5 and defer CR-6 - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Responds to:** CR-5, CR-6.
* **Affected Scope:** CUJ 4 (271-279); Q7 (596-602); Q6 (492-497); invariant
  3 (343); `fast_valid` (1441-1442); `convert_one` step 4 (1675-1678).
* **Rationale & Recommendation:**
  * **CR-5 is right, but needs no new flag.** A restored folder can never pass
    the mtime check, so the "restore" promise in Q7 and CUJ 4 is false. Use
    `convert --audit --session KEY`, which already hashes every inventoried
    file. Rebuild the descriptor and clear the issue when a complete folder
    fails only the directory-mtime check, hashes clean, and holds no file
    outside its inventory (the listing `convert_one` step 4 already makes).
    No new option or state.
  * **Defer CR-6.** `--accept-missing-series` adds an option, a state field
    and a report value for a case no evidence yet sizes; the real-converter
    spike (ZZ-10) will provide that evidence. For now record it as an
    accepted failure mode in Q6: outputs stay usable in `.pending/`, and the
    issue names the series. If an override is needed later, extend the
    `--accept-multi-study` acceptance path rather than adding another.

### ZZ-9. Do not add CR-10's `attempts` list; CR-10 confirms ZO-2 - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Responds to:** ZO-2, CR-10.
* **Affected Scope:** schema (356-357, 364); Q1 (1201-1203); Q15 retention
  (837-846); `convert_one` step 6 (1680-1685).
* **Rationale & Recommendation:**
  * The single-migration rule has already failed once: the one later-phase
    field it anticipated, `last_attempt.created_utc`, cannot age earlier
    attempts (CR-10). CR-10's seven-field list is another guess at a phase-2
    design that does not exist yet.
  * Adopt ZO-2's rule that additive optional fields need no migration.
  * Also drop `created_utc` and `diagnostics_removed` from the phase-1
    `last_attempt`. `diagnostics_removed` is in the schema table (364) but not
    in the plan (1683), and only Q15 uses either.
  * Phase 2 decides how to age attempts made before it (keep them, or clean
    them once through the runbook).
  * CR-17's note that Enhanced MR multi-frame objects store nulls further
    weakens the case for geometry capture (ZO-2).

### ZZ-10. Phase-1 scope after Stages 6-7: run the spike first, answer CR-11 in one sentence, keep phase-2 mechanisms out - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Responds to:** ZO-1, ZO-7, CR-11, CR-13, CR-14, CR-24.
* **Affected Scope:** Goal 4 (164-171); Phase map (390-404); Q14 (771-799);
  Q2 Windows items (1117-1137); NFRs (1289-1307); Implementation order
  (1354-1367).
* **Rationale & Recommendation:**
  * Stages 6-7 strengthen ZO-1. Taken as written, the critic fixes grow phase
    1 (CR-1 copy protocol, CR-4 identity files, CR-5 flag, CR-6 override,
    CR-9 reconciliation, CR-10 list, CR-13 side files, CR-15 detection). The
    reductions in this report limit that to a few rules.
  * **CR-14:** adopt the reordering (real-converter smoke tests right after
    step 0, not at step 10). Reject choosing fallbacks (per-series runs, a
    sibling folder for non-image objects) before the spike shows they are
    needed.
  * **CR-11:** state in one sentence whether phase 1 runs on production data.
    If it does, its gates are those already marked "evidence phase 1" (no new
    gates), and only Q15's in-process size checks move into phase 1 (member
    count, member size, total bytes, free-space reserve). They need no child
    process and already bound decompression bombs (ZO-7). Per-archive child
    processes stay in phase 2 and must justify themselves against those
    checks.
  * **CR-24:** resolved by ZO-7. Delete the Q14/Q2 mechanism text and keep one
    contract statement.
  * **CR-13:** adopt non-fatal report and history writes. Today a CSV held open
    by Excel ends the run as RUN ABORTED with workers stopped and no history
    row (`pipeline.py:982-983, 996, 1000-1006`). Put this in phase 1 if phase
    1 runs on Windows production data. Reject the per-run side file and the
    state-rename backoff until a failure is observed. One test with
    `session_status.csv` held open, not one per file.

### ZZ-11. [new] A recovered index needs no persistent blocking issue - Severity: Nit
* **Reviewer Lens:** `egm-zoom-out`
* **Responds to:** CO-4, CO-12.
* **Affected Scope:** Q21 (1084-1088); `state.py` Load (1463-1468).
* **Rationale & Recommendation:** Recovery from `.bak` plus journals is
  complete by construction (1081-1083). The blocking `state:recovered` issue
  has no clearing rule, so every later run would exit 2. An error-level log
  line and a note in that run's history row are enough. State the load rule
  in one sentence: index missing, use `.bak` silently (normal between the two
  flush renames); index unreadable, use `.bak` and log; both unusable, stop
  and name both files; neither exists, new project.

### ZZ-12. CR-15: list extensions and assert in the existing smoke test; no detection mechanism - Severity: Nit
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** invariant 8 (348); Q10 (852-876);
  `test_real_converter.py` (2043-2055).
* **Rationale & Recommendation:** The exposure is already bounded: converter
  arguments are fixed and supported dcm2niix versions are a tested range
  (Q2), so a new version reaches users only after passing the smoke tests.
  Adopt: add NIfTI extensions to invariant 8's channel list, and have the
  byte-identity smoke test assert the tested versions write none. Reject:
  extension codes in provenance and a maintained known-code list.

### ZZ-13. New sections from Stages 6-7 should replace text, not add it - Severity: Nit
* **Reviewer Lens:** `egm-zoom-out`
* **Responds to:** CR-16, CR-17, CR-19, CR-21/CO-6, CR-44, CR-45,
  CO-2/CO-3/CO-4, CR-11 (phase tags).
* **Affected Scope:** whole document; contract (339-352); traceability table
  (2110-2125); release gates (429-445); Decision Log (463-1222); Alternatives
  (1223-1283).
* **Discrepancy Description:** At 2,152 lines the document already has three
  traceability records (contract evidence column, phase-1 regression table,
  Goal 7 checklist with G01).
* **Rationale & Recommendation:**
  * **CR-16:** a four-column risk table (risk, phase exposed, mitigation or
    "accepted", link) that absorbs the scattered "Known failure mode" text.
    Drop the owner column (single maintainer), the remaining-risk column, and
    the test column (the contract table has tests).
  * **CR-19:** one index table (ID, topic, phase, link), which also carries
    CR-11's phase tags. No per-entry tags and no status column once CR-18 is
    fixed.
  * **CR-17:** link each row to its Decision Log entry instead of adding a
    chosen-option column.
  * **CR-21/CO-6:** fix Goal 1's wording and add one sentence mapping area 8
    to G07, Q2 and Q17.
  * **CR-44:** correct the two mis-cited rows only.
  * **CR-45:** replace "at most 20 metadata calls" with the code's own bound,
    "proportional to the session's directory count, never its file count"
    (`pipeline.py:299`). No formula.
  * **CO-2/3/4:** one short terms-and-layout block linking baseline §8, §12.1
    and §15 instead of repeating them; define "blocking" in one sentence.
  * The ZO-7 and ZO-11 removals more than pay for these additions.

### ZZ-14. CR-46 locks: adopt two lines, reject the rest - Severity: Nit
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** Q13 (703-731); `configure` lock roots (1399-1402);
  `LockBusy` (1731).
* **Rationale & Recommendation:**
  * Adopt: `migrate` takes the legacy project lock. That is where a running
    0.1.0 matters most, because 0.1.0 refuses schema 3 at load
    (`pipeline.py:211-212`).
  * Adopt: a note that stored absolute paths make a mount-path alias look like
    a moved project.
  * Reject: a separate lock-busy exit code (new interface value with no shown
    scheduler need) and special handling for read-only DICOM roots (document
    them as unsupported).

---

## Earlier recommendations: Adopt / Shrink / Reject

| Earlier ID | Verdict | Reason |
| --- | --- | --- |
| ZO-1 | Adopt | Stages 6-7 strengthen it (ZZ-10) |
| ZO-2 | Adopt | CR-10 shows front-loading already failed; also drop `created_utc`/`diagnostics_removed` (ZZ-9) |
| ZO-3 | Adopt | Rests on estimates only (CR-36); no safety need |
| ZO-4 | Adopt | Only together with ZZ-2; otherwise dropping `zip_move` strands ZIPs |
| ZO-5 | Adopt, amended | Weighing optional markers is right; "do not move" alone strands ZIPs, so gate `Uploads/` only (ZZ-2) |
| ZO-6 | Adopt | Caveat: backfill reads every object of a flagged session; dry-run count decides (ZZ-4) |
| ZO-7 | Adopt | Also resolves CR-24 and the Q14 tense problem in CR-11 |
| ZO-8 | Adopt | Resolves CR-25 |
| ZO-9 | Adopt | Makes CR-8's rewrite moot |
| ZO-10 | Adopt | Implement via the `.state` swap; compare mtimes, not names (ZZ-5) |
| ZO-11 | Adopt | Unchanged |
| ZO-12 | Adopt | Same issue as CO-1, CR-18, CR-43 |
| CO-1 | Adopt | Label fix |
| CO-2, CO-3, CO-4 | Shrink | One terms/layout block linking the baseline; one-sentence "blocking" definition |
| CO-5 | Adopt | Resolved by the four rules in ZZ-1 |
| CO-6 | Shrink | Wording fix, no new column |
| CO-7 | Adopt | Wording; the 20 s budget moves to Q15 in phase 2 (ZZ-6) |
| CO-8, CO-9, CO-10 | Adopt | Definitions or links only |
| CO-11 | Adopt | Resolved by the single adoption rule (ZZ-1) |
| CO-12 | Adopt | One-sentence load rule (ZZ-11) |
| CO-13 | Adopt | Resolved by the `.state` swap (ZZ-5) |
| CO-14 | Adopt | Wording; mostly moot after ZZ-2 |
| CR-1 | Shrink | No roll-forward; stat check plus existing coverage function in one adoption rule; no `.copying_*` |
| CR-2(a) | Shrink | Trigger "state doesn't record this receipt"; no new field |
| CR-2(b) | Adopt | One-line else-branch |
| CR-2(c) | Reject | Markers gate `Uploads/` only; no marker reconciliation |
| CR-2(d) | Adopt | One-line lock-scope rule |
| CR-3 | Shrink | Flag, skip post-stop work, narrow `interrupted` rule; no per-step budgets |
| CR-4 | Shrink | No identity files or new option; specify combinations; adoption before the marker rule |
| CR-5 | Shrink | `--audit --session` revalidates; no new flag |
| CR-6 | Reject | Defer until the spike gives evidence; record as accepted failure mode |
| CR-7 | Shrink | Keep dry-run counts; header backfill per ZO-6; no new status |
| CR-8 | Shrink | Rewrite moot under ZO-9; one sentence on legacy `needs_conversion` |
| CR-9 | Shrink | Report-only in the migrate dry run |
| CR-10 | Reject | ZO-2's additive-field rule instead |
| CR-11 | Shrink | One sentence; existing phase-1 gates; only in-process size checks; tags via the index table |
| CR-12 | Shrink | `.state` swap plus one rollback test |
| CR-13 | Shrink | Non-fatal report/history writes; no side file or backoff |
| CR-14 | Shrink | Reorder the spike; do not pick fallbacks now |
| CR-15 | Shrink | Doc change plus a smoke-test assertion |
| CR-16 | Shrink | Four-column table that replaces scattered text |
| CR-17 | Shrink | Trade-offs plus links; no chosen-option column |
| CR-18 | Adopt | Labels |
| CR-19 | Shrink | One index table (ID, topic, phase, link) |
| CR-20 | Adopt | Moves only |
| CR-21 | Shrink | Wording, no new column |
| CR-22, CR-23 | Adopt | Wording and label only |
| CR-24 | Adopt | Resolved by ZO-7 |
| CR-25 | Adopt | Resolved by ZO-8 |
| CR-26 to CR-29 | Adopt | Wording |
| CR-30 | Adopt | Split the R/T table; the Q12 table shrinks anyway |
| CR-31 to CR-35 | Adopt | Editorial |
| CR-36 | Adopt | Label the estimate; ZO-3 defers the tuning |
| CR-37, CR-38 | Adopt | Deletes text |
| CR-39 to CR-42 | Adopt | Wording |
| CR-43 | Shrink | Mark the phase; the dcm2niix range does not change behavior, so Goal 8 holds |
| CR-44 | Shrink | Fix the two mis-cited rows only |
| CR-45 | Shrink | Bound by directory count, not a number or formula |
| CR-46 | Shrink | Migrate takes the legacy lock; alias note; no busy code; read-only roots unsupported |
| CR-47 | Shrink | Scoping sentence; journal filter via ZO-10; no directory fsyncs |
