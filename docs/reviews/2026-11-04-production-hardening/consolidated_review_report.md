# Consolidated Review Report: Production Hardening Design

**Target:** `docs/designs/production-hardening.md` (2,152 lines; FeatureRange
1-1340, DetailedRange 1341-2152)
**Review mode:** Consolidated, uninterrupted. Stages 5-9 ran in order with no
human checkpoints and no edits to the target.
**Date:** 2026-11-04
**Reviewer context (GoldfishContext):** the three linked files in
`docs/designs/` (concepts brief, baseline spec, source manifest) plus the
whole repository except `docs/designs/design_draft.md` (an older copy of the
target). Stage 6 read the target alone, by design. The two external EGM URLs
were left out at the author's request.

## Bottom line

The design is **not ready to approve or implement**. One defect blocks it on
substance: crash recovery can publish and record as `converted` a folder that
was only partly copied (CA-1). Three independent reviewers found this from
different angles, and the Elephant confirmed it against the code. The rest of
the problems fall into four groups:

1. **Phase 1 carries more than the safety work, and the critics' fixes would
   add more.** Stage 8 judged that most fixes can be made smaller, so the
   document gets shorter, not longer (CA-2 to CA-4).
2. **Migration and recovery have unspecified or unsafe paths.** These are
   version fields, interruption windows, rollback, and holds that cannot be
   cleared (CA-6 to CA-8).
3. **The phase 1 plan cannot be built step by step as written.** Steps depend
   on later steps, existing tests break without being listed, and the
   lifecycle of the new issue keys is undefined (CA-9, CA-10, CA-15).
4. **Labels and claims contradict each other.** "Proposed" against
   "approved", Windows containment described three ways, P8's severity, and
   claims of passing tests (CA-16, CA-17).

Several fixes reverse or narrow decisions the author has already made. They
are listed under [Decisions for the author](#decisions-for-the-author) and
should be settled before any edit.

## Reviewer status

| Stage | Reviewers | Status | Deduplicated findings (Blocker / Major / Nit) | Report |
| --- | --- | --- | --- | --- |
| 5 Initial Zoom Out | `egm-zoom-out` | completed | 0 / 7 / 5 | `zoom_out_report.md` |
| 6 Comprehension | `egm-comprehension` | completed | 0 / 5 / 9 | `comprehension_report.md` |
| 7 Critic | `egm-design`, `egm-structural`, `egm-style` | all completed | 1 / 28 / 18 | `critic_report.md` |
| 8 Intermediate Zoom Out | `egm-zoom-out` | completed (resumed once after an API usage limit) | 1 / 9 / 4 | `intermediate_zoom_out_report.md` |
| 9 Implementation Readiness | `egm-implementation-readiness`, `egm-codebase-details`, `egm-style` | all completed | 2 / 24 / 20 | `implementation_readiness_report.md` |

All nine reviewer runs completed; none is recorded as not run. Stage findings
use the prefixes ZO (Stage 5), CO (6), CR (7), ZZ (8) and IR (9).

**Comprehension check:** Stage 6's paraphrase of the proposal and of the
current system is at the top of `comprehension_report.md`. No section was too
unclear to paraphrase. Compare it with your intent; a wrong paraphrase is a
finding.

**Claims the Elephant checked against the source:**
* CA-1: `publish_tree` falls back to `shutil.copytree` across devices
  (`pipeline.py:107-113`), and the roll-forward row trusts only "has its
  receipt" (target line 669).
* CA-6: descriptor and source `version` checks (`pipeline.py:299`, `:322`,
  `:596`) against "Set entry `version: 3`" (target line 1755).
* CA-8: `flush()` says "rename" (target line 1477), which fails on Windows
  when the target exists.
* CA-10: `test_path_traversal_member_cannot_escape` expects `n_dicom == 2`.
* CA-11: `test_output_root_override_does_not_reuse_other_root` expects exit 0.
* CA-15: the `fast_valid` signature (target line 1441 against
  `pipeline.py:298`).
* CA-18: the `ImageType` "contains" rule against `tests/test_pipeline.py:254`.

All seven claims hold.

---

## Decisions for the author

These come first because several action items depend on them. Each one
reverses or narrows a recorded decision, or picks between reviewer proposals
that conflict.

**Author's direction (2026-11-04):** phase 1 runs on production data. Prefer
simplicity: no strict security hardening or extra checks are needed before the
pipeline runs. The Elephant decides which phase each item goes in. D2 is the
author's own choice; the Elephant made the other decisions under this
direction.

| # | Decision | Options | Decided 2026-11-04 | Affects |
| --- | --- | --- | --- | --- |
| D1 | Does phase 1 run on production data? | (a) Yes: move Q15's in-process size checks into phase 1. (b) No: say how studies are protected until phase 4 | **Yes.** Only one check moves into phase 1: before extracting, compare the sum of the ZIP's declared member sizes plus a reserve against the free space on the extraction root. The member-count and member-size caps and the per-archive child processes are not built, on the stated assumption that archives come from the lab's own scanner exports | CA-2 |
| D2 | Remove roll-forward from Q12 recovery? | (a) Remove it; recovery only rolls back or adopts after a stat check. (b) Keep it, with a `.copying_*` step or an intent `phase` | **(a), the author's choice**, plus the stat check before any adoption, one shared adoption rule, and lock-scoped reconciliation | CA-1 |
| D3 | Where are upload markers required? | (a) Only in `Uploads/`; nothing moves with the ZIP. (b) Optional everywhere. (c) As designed, plus a reconciliation rule for stranded markers | **Nowhere. Markers are dropped (a narrower choice than any listed option).** The Elephant checked that 0.1.0 already catches partial uploads. A partly copied ZIP has no end-of-archive record, so it fails to open. Reading each member checks its CRC-32 (`pipeline.py:420-421`). The size and mtime signature is compared before and after hashing and extraction (`pipeline.py:522-531`). A failed ZIP is retried once its size or mtime changes (`pipeline.py:596-606`). This removes the `zip_move` intent, the canonical-name marker handling, the `waiting for marker` issue and the upgrade note | CA-5 (Q4) |
| D4 | Replace "single migration" with "additive optional fields need no migration"? | (a) Yes, and drop geometry capture plus the unused `last_attempt` fields. (b) Keep the rule and add an `attempts` list | **(a)** | CA-3 (Q1, Q9 capture) |
| D5 | Allow `migrate` to read DICOM headers for flagged sessions? | (a) Yes, a header-only backfill inside `migrate`. (b) No; keep per-session `--force-reextract` | **Neither: `migrate` is dropped (decided 2026-11-05).** The author confirmed that no project has been processed by 0.1.0 and no output must be kept. Phase 1 refuses any state schema other than 3. CA-6 and CA-7 no longer apply | CA-6, CA-7 |
| D6 | What protects against a mistyped report root with empty state? | (a) Accept the cost (see the counterpoint below). (b) Refuse empty state while the roots hold receipts, unless an explicit option is given | **(a).** Under the D2 adoption rule, empty state costs one hash per ZIP plus adoption, and no data is lost. Add one runbook line | CA-8 |
| D7 | Is there an operator override for series dcm2niix cannot convert? | (a) Defer until the real-converter spike; record as an accepted failure mode. (b) Add `--accept-missing-series` now | **(a)** | CA-11 |
| D8 | Should phase 2-4 mechanisms leave this document? | (a) Keep only the guarantees, phases and open questions. (b) Keep the mechanisms and fix the contradictions | **(a).** Each later phase keeps its guarantee, its trigger and its open questions. One Windows statement replaces the three: "On Windows, stopping a run stops the converter but not processes the converter started." | CA-2 (Q2, Q14, Q15, Q23) |

### Revised phase plan (decided 2026-11-04)

**The rule:** phase 1 holds the safety fixes plus every change to the output
layout or the state format. Production data is then converted under a single
layout from its first run. Every other item waits for a named trigger, and a
later phase gets its own short design document only when its trigger occurs.

**Phase 1, the lab's production version** (installed from the repository; not
a public release):
* **Kept as designed:**
  * Q3, Q6, Q7, Q8 (record only), Q9 integrity checks, Q10, Q12 (with D2),
    Q13, Q19, Q20;
  * the Q2 ID rules, P7, P8;
  * per-root locks and the history row.
* **Kept, with the reason stated:** Q11 classifier revision 2. It changes
  folder names, so doing it before production data avoids two layouts. Goal 4
  is reworded to state the rule above instead of moving Q11 out.
* **Kept, reduced:**
  * Q21: sequence numbers and `.bak` only.
  * Q22 `migrate`: only if a 0.1.0 project must be kept (D5).
  * Packaging: `pyproject.toml`, a Linux and Windows CI matrix that runs the
    tests, and `LICENSE`/`NOTICE`. The distribution checks move to phase 4.
  * Shutdown: CA-12's one-shot stop flag. The 20 s promise is removed
    everywhere.
* **Added:**
  * the free-space check before extraction (D1);
  * report files held open on Windows no longer stop the run (CA-13);
  * NIfTI extensions named in invariant 8 (CA-14);
  * a README and runbook that describe phase 1 behavior.
* **Removed:**
  * upload markers (D3);
  * geometry capture and fields nothing reads (D4);
  * relative keys and the 300 s flush (CA-4);
  * the `test_cache_budget.py` instrumentation. The existing
    `test_1000_cached_sessions_do_not_read_scan_contents` remains Goal 5's
    evidence.
* **Order:**
  1. Step 0, a move-only commit.
  2. The real-converter smoke tests. A failure reopens Q19.
  3. Everything else.

**Phase 2, operations.** Each item starts only when its trigger occurs:
* Windows whole-tree stop (Job Objects): when leftover converter processes
  are seen on Windows.
* Cleanup of retained `.pending` and stage folders: when they use too much
  disk. Until then, the phase 1 runbook gives the manual cleanup.
* Windows path-length holds: when a long path causes a failure. Phase 1
  already reports such a session as failed.
* Archive caps and per-archive child processes: only if archives start coming
  from outside the lab.

**Phase 3, scale.** Starts only when runs are measured as too slow:
* Q18: subject index, report cadence, relative keys, flush interval, and the
  T15 benchmarks.
* Q23 `relocate`: when a project has to move.

**Phase 4, public release.** Starts only when the package is published:
* the README contract rewrite;
* `SECURITY.md` and the support table;
* the distribution checks and the versioning policy;
* the PyPI release.

**Release gates.** Phase 1 must pass every gate marked "evidence phase 1",
with two changes:
* G06 uses the existing cached-session test.
* G07's distribution checks move to phase 4.

G02's two-node lock check is needed only if runs start from more than one
machine on shared storage.

**Effect on the action items:**
* **Adopted unchanged:** CA-1, CA-8 (with D6 (a)), CA-10, CA-13, CA-14,
  CA-16, CA-18, CA-19, CA-20 and CA-21.
* **Resolved by this plan:** CA-2.
* **Decided by D4 (a):** CA-3.
* **Adopted:** CA-4.
* **CA-5:** D3 removes most of it. What remains is deleting Q4, its tests,
  and its README and upgrade-note references.
* **CA-6 and CA-7:** depend on D5. If `migrate` stays, CA-6 applies as
  written, recording only the package version for provenance. Grandfathering
  replaces CA-7.
* **CA-9:** adopted. It now covers fewer keys: no `wait:`, and CA-8's table
  removes `state:recovered`.
* **CA-11:** adopted, with D7 (a).
* **CA-12:** adopted in its reduced form.
* **CA-15:** adopted, minus the cache-budget items.
* **CA-17:** done last, after the cuts, in Stage 8's reduced forms only.

### Applied to the design doc (2026-11-05)

The pre-review text is in git history: it is the version of
`docs/designs/production-hardening.md` committed before these decisions were
applied. The revised doc is 2,284 lines, up from 2,152.

**Where the length changed:**
* **Shorter:** the migration and the phase 2-4 mechanisms (Q14, Q15, Q18,
  Q22, Q23).
* **Longer:** specifications the reviewers found missing:
  * the adoption rule;
  * the state-load table;
  * the issue table;
  * the preparation outcome table;
  * the crash matrix;
  * folder revalidation.

**Status of each item:**
* **Applied:**
  * D1-D8;
  * the revised phase plan;
  * CA-1 to CA-5, CA-8 to CA-16 and CA-18;
  * from CA-19: provenance records the package version only, and the
    drafting narration is gone;
  * from CA-21: IR-27, IR-28, IR-29, IR-30, IR-32 (grace period kept at
    today's 10 s), IR-33, IR-34, IR-35, IR-36 and part of IR-37.
* **No longer apply:**
  * CA-6 and CA-7 (D5);
  * CR-45 (the 20-call budget is gone).
* **Not applied (document polish, no behavior change):**
  * CA-17 (risk table, Decision Log index, terms block);
  * CA-20 (editorial nits);
  * the rest of CA-19 (moving code-level detail out of the decisions);
  * the rest of CA-21: CR-44, CR-46, IR-31, IR-38 and IR-40.

### Counterpoints the Elephant raises against the reviewers

* **D6, Stage 8 understates the cost.** Stage 8 says empty state "costs one
  hash per ZIP and republishes nothing". At the 20,000-session target that
  still means reading every canonical ZIP in full, which is the "hours to
  days" I/O that Q22 rejects for header reads (line 1266). Before choosing
  (a), check whether a cheap refusal is enough. Option (b) needs no identity
  file: `prepare` can refuse when state is empty but `DicomExtracted/Subjects/`
  holds receipts.
* **D3(a) reopens R06 for one workflow.** If the lab copies ZIPs straight into
  `SubjectsSorted/`, Uploads-only markers give those copies no readiness
  check. That is how 0.1.0 behaves. Confirm how uploads actually arrive.
* **D2(a) costs repeated work, which is acceptable only if crashes are rare.**
  Without roll-forward, a crash during a first publish redoes the whole
  conversion, up to the 14,400 s converter timeout per session. That is
  acceptable for rare crashes, but not if node loss is routine on the cluster.
* **IR-2 is rated Blocker by one reviewer.** The `fast_valid` signature
  mismatch is a one-line correction to the plan, not a design gap. The other
  two reviewers rated it Nit. This report treats it as a Major plan
  correction inside CA-15.

---

## Action items

### 🔴 Blocker

#### CA-1. Make crash recovery unable to publish or adopt an incomplete folder
* **Problem:** On a cross-device publish, `.incoming_*` is filled by
  `copytree` in listing order. The receipt can be copied before the volumes.
  A SIGKILL mid-copy leaves a receipt-bearing, incomplete folder. Recovery
  then rolls it forward, and adoption records it as `converted` without
  reading images. The fast cache check passes from then on, and Q7 forbids
  automatic reconversion. Stage 8 adds that `prepare` is exposed too: a
  half-copied extraction would give dcm2niix a subset of slices.
* **Related gaps:**
  * Three adoption paths, each with a different match rule.
  * No rule for re-publication over an existing record.
  * No else-branch for "destination and `.previous_*` both present".
  * Adoption records lack `n_volumes`, coverage and provenance sources.
  * Every command reconciles every intent, including intents on roots it has
    not locked.
* **Change (Stage 8 form; D2 decided (a) on 2026-11-04):**
  1. Replace the Q12 table with four rules:
     * Destination has a receipt that state does not record: adopt it.
     * Destination missing and `.previous_*` present: roll back.
     * Otherwise: remove `.incoming_*` and replan.
     * Anything else: touch nothing and raise an issue.
  2. Use one adoption rule, called from reconciliation, `plan_prepare` and
     `plan_convert`. Adopt only when the receipt matches the prepared content
     and every inventory file exists at its recorded size. This uses stat
     calls only and runs only during recovery. Rebuild the descriptor with
     `receipt_info`, recompute coverage with `output_series_keys`, and state
     how `n_volumes`, provenance and `created_utc` are filled.
  3. A command reconciles only intents whose roots it has locked.
  4. Add T07 cases:
     * a kill during a cross-device copy;
     * reconversion and re-extraction variants;
     * adoption of a folder with a deleted volume.
* **If roll-forward is kept (D2b):** define the intent `phase` values (for
  example `staged` → `incoming_complete` → `swapped`) and roll forward only
  from `incoming_complete`. Alternatively, copy into `.copying_*` and rename
  it to `.incoming_*` only after an fsync. Add the ZIP and source fields that
  a prepare-publish adoption needs.
* **Where:** Q12 (662-687), Q21 (1088-1091), CUJ 2 (259-260),
  `recovery.py` (1505-1523), `plan_prepare` (1633-1638), `plan_convert`
  (1692-1697), T07 (1985-1994).
* **Done when:** every Q12 row has a T07 test, and a crash at any point during
  a cross-device copy never produces `converted` for missing volumes.

### 🟡 Major

#### CA-2. Settle phase 1's production status and cut phase 1 to the safety work
* **Problem:**
  * Goal 4 puts safety before packaging and performance. Phase 1 also
    carries other work:
    * a 13-module split;
    * classifier revision 2;
    * upload markers;
    * index tuning;
    * geometry capture;
    * packaging, placed first in the step order.
  * The document fixes phase 2-4 mechanisms in detail. Those details already
    contradict each other: Windows containment is described three ways
    (lines 351, 786-788, 1119-1123).
  * The text contradicts itself on whether phase 1 is a release.
  * Three untested assumptions about dcm2niix carry Q6 and Q19, and none has
    a fallback.
* **Change:**
  * Answer D1 in one sentence.
  * If phase 1 runs on production data, move only Q15's in-process size
    checks into phase 1: member count, member size, total bytes and a
    free-space reserve. Its gates are the ones already marked "evidence
    phase 1".
  * Merge step 0 (move only) as its own deliverable.
  * Run the real-converter smoke tests right after step 0, not at step 10. At
    the non-image test, state what a failure triggers (reopen Q19).
  * For phases 2-4, keep the guarantee, the phase and the open questions, and
    delete the mechanisms (D8). Keep one Windows containment statement.
  * Move classifier revision 2 out of phase 1, or reword Goal 4. CA-3 and
    CA-4 cover geometry capture and index tuning; CA-5 covers markers.
* **Where:** Goal 4 (164-171), Phase map (390-404), Q1, Q2, Q11, Q14, Q15,
  Q23, NFRs (1289-1307), Implementation order (1354-1367), 2050-2052.

#### CA-3. Adopt "additive optional fields need no migration"
* **Problem:** The single-migration rule puts fields nothing reads into phase
  1. One example is geometry capture: seven fields per object for a check
     that is not approved yet. The one later-phase field the rule anticipated
     (`last_attempt.created_utc`) cannot support Q15 retention either.
* **Change (D4a):**
  * Readers default any missing optional field.
  * Phase 1 carries only the fields it reads.
  * Drop geometry capture, `created_utc` and `diagnostics_removed`.
  * Do not add an `attempts` list; phase 2 designs retention.
* **Where:** State schema 3 (354-369), Q1 (1201-1203), Q9 (904-910), Q15
  retention (837-846), `prepare.py` (1601-1603), `support.py` (1857-1860).

#### CA-4. Defer index-size tuning until it is measured
* **Problem:** Relative directory keys and the 300 s flush rest on estimates
  ("about 60 MB"). They change the cache path Goal 5 protects and widen crash
  replay to five minutes of journals.
* **Change:**
  * Keep `seq` and `.bak` in phase 1.
  * Defer relative keys and the flush interval until the T15 numbers exist.
  * Mark the 60 MB figure as an estimate.
* **Where:** Q18 (1053-1054), Q21 (1068-1071, 1092-1096), `receipts.py`
  (1439-1442), `state.py` (1474-1476), `migrate.py` (1755).

#### CA-5. Settle the upload-marker protocol
* **Problem:** Moving the marker with the ZIP splits one atomic rename into
  two. A crash in between leaves a canonical ZIP `waiting for marker` forever
  with an orphaned marker. The `zip_move` intent records markers, but no rule
  uses them.
* **Other gaps:**
  * The rule "not in `sources` needs a marker" contradicts Q4's upgrade note.
  * The file-name pattern appears in three forms.
  * `.sha256` failures have no defined outcomes.
  * The `zip_move` intent has no root field.
* **Change (pending D3):**
  * Markers gate only `Uploads/`, and nothing moves with the ZIP.
  * Drop the `zip_move` intent and the canonical-name marker handling. A
    crash then repairs itself by replanning.
  * Define the pattern once with a literal example
    (`S001_20260725.zip.done`).
  * State one rule for which ZIPs need a marker.
  * Define the outcomes for a malformed `.sha256`, a mismatched `.sha256`,
    and a ZIP that has both marker kinds.
* **Where:** Q4 (733-761), interface (381), Q12 rows (664-666), intent schema
  (1505-1509), `marker_for` (1587-1591), `move_archive` (1607-1609),
  `plan_prepare` (1633-1638), tests (1933-1936).

#### CA-6. Make `migrate` correct, resumable and reversible
* **Problem:**
  * Only "entry `version: 3`" is set, so the version-2 descriptors and source
    records fail the fast path. On the first run after migration, every ZIP
    is re-hashed and every session is re-extracted or flagged (verified).
  * "Mark series required" has no storage location. Rewriting inventories
    would invalidate every receipt.
  * An interruption after step 2 leaves schema-2 journals that have no `seq`
    beside a schema-3 index, and a rerun is a no-op.
  * Rollback restores an index that lacks the replayed journal events, and
    0.1.0 then replays the schema-3 journals.
  * The command's interface is unspecified: `configure`, the root mapping,
    locks, exit codes, the history row and the dry-run format.
  * Migrated provenance claims SOP-list revision 1.
  * Leftover 0.1.0 swap folders are never reported.
* **Change:**
  * Give a field-by-field transform for session entries, descriptors, sources
    and issues, for each status. State that receipts and inventories on disk
    are not modified.
  * Keep today's `content_sha256` definition (ZO-9), so no `source_sha256`
    rewrite is needed.
  * Replay only schema-2 journals newer than the index, compared by file
    mtime.
  * Build the schema-3 state in a sibling directory and swap whole `.state`
    directories:
    * a rerun completes a half-done swap;
    * `prepare` and `convert` refuse while a swap is half done;
    * rollback swaps back.
  * Add one rollback test and a runbook note on the P3/P10 behavior that
    returns under 0.1.0.
  * `migrate` takes the legacy project lock.
  * Report 0.1.0 swap-named siblings in the dry run.
  * Record the package version only (ZO-8), or name each revision field and
    its migrated value.
  * Add one sentence putting power loss out of scope.
* **Where:** Goal 6 (178-183), Q22 (963-1009), NFR Rollback (1302-1307),
  `config.py` (1395-1403), `migrate.py` (1742-1774), `reports.py`
  (1717-1718).

#### CA-7. Clear migration's false `partial` sessions and multi-study holds without re-extraction
* **Problem:**
  * Every old series becomes SOP class `unknown`. Sessions with any
    non-image series (common in Siemens exports) become `partial`, and every
    run exits 2.
  * The remedy is one `--force-reextract` per session. That costs more I/O
    than the header-read option Q22 rejected, and it fails when the ZIP is
    missing, for multi-study sessions, and for conflicting repeated
    instances.
  * `--accept-multi-study` cannot clear a migrated hold, because the session
    is cached and never reaches `prepare_one`.
* **Change (pending D5):**
  * Run a header-only backfill inside `migrate` for the flagged sessions. It
    reads every object in those sessions, because schema-2 rows have no
    series key.
  * The dry run reports affected sessions and objects.
  * Make multi-study acceptance of a prepared session a state-only event.
  * This removes `prepare_one` step 5 and its read of the unlocked NIfTI
    root. Re-check whether `--session` on `prepare` is still needed.
* **Where:** CUJ 6 (289-298), Q20 (571-581), Q22, `prepare_one` step 5
  (1625-1629), `migrate.py` (1757-1762), runbook (2090), Alternatives (1266).

#### CA-8. Specify state loading, and decide on missing state
* **Problem:**
  * Several index × `.bak` combinations are unspecified.
  * A missing index is silent in the plan but raises an issue in Q21.
  * `state:recovered` blocks forever, because nothing clears it.
  * The next flush after a recovery renames the corrupt index over the good
    `.bak`.
  * "Rename" fails on Windows (verified).
  * Exception base classes are not given; the existing test expects
    `ValueError`.
  * Empty state with full roots silently starts a new project (D6).
* **Change:**
  * Write one decision table:
    * index missing: use `.bak` silently;
    * index unreadable: use `.bak`, log at error level and note it in the
      history row, with no persistent issue;
    * both unusable: stop and name both files;
    * neither exists: new project.
  * Move a corrupt index aside, use the in-memory `last_seq`, and say
    `os.replace`.
  * Give the base classes and messages for `StateError` and `LockBusy`.
  * Resolve D6.
* **Where:** Q21 (1084-1091), Q23 (1016-1018), `state.py` (1463-1480),
  `locks.py` (1491-1499).

#### CA-9. Write an issue lifecycle table and define how report statuses are derived
* **Problem:**
  * The new keys (`state:recovered`, `wait:`, `missing:`, `output:`,
    migrate's `convert:`, the unnamed multi-study and Q12 issues, HoldError)
    have no clearing path. Several would make every later run exit 2.
  * "Blocking" is never defined.
  * Waiting ZIPs would be counted as `held`, which gives exit 2 and breaks
    the plan's own test.
  * Statuses for sessions without an entry (`waiting_for_marker`, `held`,
    `output_modified`) have no source.
  * The new column formats are unspecified.
* **Change:**
  * Add a table with one row per key: format, stage, subject/session
    fields, blocking or not, raised by, cleared by.
  * Define "blocking" in one sentence.
  * Count waiting ZIPs separately as nonblocking.
  * Define each status's source and each column's format.
* **Where:** Q4 (750-753), Q7 (597-599), Q12 (695-699), invariant 10 (350),
  `reports.py` (1706-1718), plus every place that raises an issue.

#### CA-10. Specify the interface between the prepare planner and the worker
* **Problem:**
  * The candidate 5-tuple has no fields for the marker result, adopt mode or
    audit re-hash.
  * `refresh` updates only `archive_signature`, so the ZIP is re-hashed on
    every run.
  * `held` is counted as `completed`, is re-hashed every run, and writes a
    `failed` session.
  * Where the `--audit` re-hash runs is unstated.
  * "Repeats are not written" conflicts with hash-while-writing extraction
    and breaks `test_path_traversal_member_cannot_escape` (verified).
* **Change:**
  * Define the candidate structure.
  * Define each outcome's event fields, counting, source record and cache
    rule.
  * Refresh the source signature together with `archive_signature`.
  * State where the audit re-hash runs.
  * State the repeat mechanism and file numbering, and list the test change.
* **Where:** `extract_archive` (1592-1606), `prepare_one` (1610-1629),
  `plan_prepare` (1630-1642), `runner` (1736-1740).

#### CA-11. Fix conversion-selection rules and the "restore" promise
* **Problem:**
  * The "restore the folder" promise cannot be kept: a restored folder never
    passes the directory-mtime check, so `--force-reconvert` is the only way
    out.
  * "Complete" is undefined. `--retry-failed` on a migrated `partial` can
    overwrite the 0.1.0 folder (P10) or the record (P4).
  * A different `--output-root` now raises `output:` and breaks
    `test_output_root_override_does_not_reuse_other_root` (verified).
  * No series dcm2niix cannot convert can ever be excused.
* **Change:**
  * Make `convert --audit --session KEY` revalidate a complete folder that
    fails only the mtime check, hashes clean and has no extra files. No new
    flag is needed.
  * Define "complete" once, and apply the overwrite protections to migrated
    partials.
  * Add the other-root case.
  * Define the coverage re-evaluation condition.
  * Record unconvertible series as an accepted failure mode until the spike
    (D7).
* **Where:** CUJ 4 (271-279), Q6 (492-497), Q7 (596-602), invariant 3 (343),
  `convert_one` (1663-1685), `plan_convert` (1687-1701).

#### CA-12. Simplify shutdown handling
* **Problem:**
  * The 20 s promise has no measurement behind it, and the post-stop work
    (gzip, hashing, publishing, a large flush) is unbounded.
  * Slurm sends SIGTERM to dcm2niix directly, so the session is classified
    `failed`, not `interrupted`.
  * The raising handler can interrupt a flush partway through.
  * The handler's installation point is unstated: a signal during planning
    escapes `main` with exit 1, and a second signal aborts the cleanup.
* **Change:**
  * Use a one-shot handler, installed for the whole run, that only sets the
    stop flag.
  * After a stop, keep the stage, record `interrupted`, and skip organize,
    gzip, hashing and publishing.
  * Record `interrupted` only for the stop flag or a SIGTERM/SIGKILL return
    code (SIGSEGV stays `failed`).
  * Make the final flush and report rewrite best-effort.
  * The phase 1 test asserts exit 143 and that the converter group is gone.
    Move the numeric 20 s budget to Q15 (phase 2).
  * Add the `sbatch --signal` line to the runbook.
* **Where:** CUJ 2 (252-260), Q12 (692-694), Q15 (816), `run_converter`
  (1650-1658), `runner` (1724-1735), test at 2003.

#### CA-13. Keep running when Windows report files are held open
* **Problem:** Excel's lock on `session_status.csv` or `issues.csv` makes
  `os.replace` fail. Today that ends the run as RUN ABORTED with no history
  row.
* **Change:**
  * Make report and history write failures non-fatal: log them and retry at
    the next cadence.
  * Add one test with `session_status.csv` held open.
  * Put this in phase 1 if phase 1 runs on Windows production data.
  * No side file and no rename backoff until a failure is observed.
* **Where:** Q2 (1105-1137), `reports.py`.

#### CA-14. Name NIfTI extensions in the privacy contract
* **Problem:** Pass-through publishing (Q10) lets any extension the converter
  writes reach published files. Invariant 8 does not list extensions.
* **Change:**
  * Add extensions to invariant 8's channel list.
  * Have the byte-identity smoke test assert that the tested dcm2niix
    versions write none.
  * Do not add detection machinery.
* **Where:** invariant 8 (348), Q10 (852-876), `test_real_converter.py`
  (2043-2055).

#### CA-15. Make the implementation plan buildable one step at a time
* **Problem:**
  * Steps 4-5 need intents (step 7) and the nonblocking prefixes (step 8).
    The step-1 CI job needs a test file created in step 10.
  * Strict coverage turns every existing conversion test `partial`, because
    the fake converter hardcodes a series key.
  * Test and helper edits have no step.
  * The step-0 move map leaves things without a home: `problem()`, `run()`,
    `VERSION`, `sanitized_nifti`, `redact_diagnostic` and the `cli.py` import.
  * `fast_valid` drops `kind` while claiming to be unchanged (verified).
  * The crash matrix has no mechanism or expected outcomes.
  * Helper signatures are incomplete.
  * The cache-budget counter misses `lstat` and `io.open`.
  * "Every step's tests fail on step 0" includes tests of preserved behavior.
* **Change:**
  * Reorder the steps, or state what each step stubs.
  * Assign every test and helper edit to a step.
  * Complete the move map and keep `kind`, or list its removal.
  * Add a crash-matrix table: case, command, fixture, patch target and call
    count, before or after the call, expected result.
  * Give the full helper signatures.
  * Name the cache-budget patch targets for each Python version.
  * Limit the "fails on step 0" claim to the P-item tests.
  * List every existing test that changes.
* **Where:** 1354-1383, 1441-1442, 1852-1904, 1985-1994, 2037-2040,
  2110-2125.

#### CA-16. Make statuses, labels and claims agree
* **Problem:**
  * "Open Questions" lists approvals only, while Q2, Q7, Q15, Q17 and Q20
    still say "proposed" or "pending".
  * Q19 says the SOP-class list is part of the cache identity, but Q8 says
    the cache key is unchanged.
  * Q7 says converter changes are "reported", but Q8 says nothing reports
    them.
  * P8 is rated Medium although it is a disclosure channel, which the
    document's own scale calls Critical.
  * "Phase 1 closes P1 to P11", although P2 stays open by design.
  * G06 drops the "cached rerun" and "coordinator memory" qualifiers, and
    G07's "install cleanly" cannot be checked.
  * The 0.1.0 test pass is stated as current fact, but the suite was not run.
  * The relocate refusal in the run sequence has no phase marker.
* **Change:** Fix each label and claim as described in CR-18, CR-22, CR-23,
  CR-25 to CR-28 and CR-43.
* **Where:** 26, 98, 141-145, 211-239, 315, 443-444, 545-546, 571-572,
  590-591, 603-606, 801-806, 1112, 1165-1169, 1194.

#### CA-17. Restructure for navigation by replacing text, not adding it
* **Problem:**
  * No Risks & Mitigations section exists; the risks are scattered across 12
    entries.
  * The Decision Log is out of order: Q15 has two entries and "Module layout"
    has no ID.
  * Alternatives credit the author instead of stating trade-offs.
  * Two subsections sit under the wrong parent.
  * The eight review areas are not mapped to the ten invariants.
  * Core terms are never explained: receipt, inventory, roots, layout, issue,
    blocking.
  * Process words ("section gate", "brief reconciliation", "this session")
    mean nothing to a later reader.
* **Change (Stage 8 reduced forms):**
  * A four-column risk table (risk, phase exposed, mitigation or "accepted",
    link) that absorbs the scattered text.
  * One Decision Log index (ID, topic, phase, link), with one heading per ID.
  * Concrete trade-offs and links in Alternatives.
  * Promote Additional Resources and Future Improvements to H2.
  * Fix Goal 1's wording and map area 8 to G07, Q2 and Q17.
  * A short terms-and-layout block that links baseline §8, §12.1 and §15.
  * Dated, named events in place of the process words.
* **Where:** whole FeatureRange; see CR-16, CR-17, CR-19 to CR-21, CR-29, CO-2,
  CO-3, CO-6, CO-8 to CO-10.

#### CA-18. Fix contract strings in the plan
* **Problem:**
  * "`ImageType` contains `P`" matches `PRIMARY` under the defined meaning of
    "contains". That breaks a kept test (verified).
  * There are two different `--dry-run` help strings, and a test asserts one.
  * The README update list leaves six claims that phase 1 contradicts.
* **Change:**
  * Use "an `ImageType` value equals `P` or `PHASE`".
  * Pick one help string and quote it in both places.
  * Extend item 36's README list.
* **Where:** 379, 1392-1393, 1533-1549, 2061-2081.

#### CA-19. Lift code-level detail out of decision sections
* **Problem:**
  * The Decision Log carries code-level detail: loop expressions, map
    shapes, the `preexec_fn` ban, the import convention and a 15-row rule
    table.
  * Three revision constants repeat the package version.
  * The Q9 history appears five times.
  * Several sentences narrate how the document was drafted.
* **Change:**
  * Keep the policy in the decisions and move the mechanisms to the plan.
  * Record only the package version.
  * Keep the Q9 history once.
  * Drop the drafting narration.
* **Where:** see ZO-8, ZO-11, CR-37, CR-38.

### 🟢 Nit

#### CA-20. Editorial fixes
* Covers CR-30 to CR-35, CR-39 to CR-42 and IR-41 to IR-46:
  * split the joined R/T table and repeat the intent on each Q12 row;
  * one template for the Decision Log;
  * consistent links;
  * stray backticks at line 321;
  * calibrated framing, and "checks" in place of "proves";
  * vague words;
  * the `--accept-multi-study` scope sentence;
  * one cadence wording and one name for the decision-maker;
  * shorten the P11 row;
  * wording in the plan sections.
* Each finding gives exact replacement text.

#### CA-21. Small technical fixes
* **Contract and locks:**
  * CR-44: fix the two mis-cited evidence rows.
  * CR-45: state the metadata bound as "proportional to the directory count,
    never the file count".
  * CR-46/ZZ-14: note that a mount-path alias looks like a moved project;
    treat read-only DICOM roots as unsupported; no separate busy exit code.
* **Plan corrections:**
  * IR-27: `clear_intents` arity.
  * IR-28: keep `argv=None` on `main`.
  * IR-29: compose `ProjectLock` and reuse `State.apply`.
  * IR-32: flag the Windows grace period change from 10 s to 5 s.
  * IR-33: say what happens to `latest_convert.log`.
  * IR-34: the provenance return value.
  * IR-35: the `state` module name collision.
  * IR-37: lock edge cases.
  * IR-40: give the README checklist and the runbook lock check a location.
* **Packaging and CI:**
  * IR-30: the MANIFEST line is redundant.
  * IR-31: check the twine floor (not verified).
  * IR-36: CI job count, package install step, `terminated` outcome.
  * IR-38: missing contract tests.

---

## Traceability matrix

Every deduplicated stage finding maps to at least one action item.

| Stage | Raw finding IDs | Action item |
| --- | --- | --- |
| 5 | ZO-1, ZO-7 | CA-2 |
| 5 | ZO-2 | CA-3 |
| 5 | ZO-3 | CA-4 |
| 5 | ZO-4 | CA-1 (adoption), CA-5 (`zip_move`) |
| 5 | ZO-5 | CA-5 |
| 5 | ZO-6 | CA-7 |
| 5 | ZO-8 | CA-19, CA-16 (Q19/Q8 contradiction), CA-6 (provenance) |
| 5 | ZO-9, ZO-10 | CA-6 |
| 5 | ZO-11 | CA-19 |
| 5 | ZO-12 | CA-16 |
| 6 | CO-1, CO-7 | CA-16 (CO-7's shutdown budget also CA-12) |
| 6 | CO-2, CO-3, CO-6, CO-8, CO-9, CO-10 | CA-17 |
| 6 | CO-4 | CA-9 |
| 6 | CO-5, CO-11 | CA-1 |
| 6 | CO-12 | CA-8 |
| 6 | CO-13 | CA-6 |
| 6 | CO-14 | CA-5 |
| 7 | CR-1 | CA-1 |
| 7 | CR-2 | CA-1 (a, b, d), CA-5 (c) |
| 7 | CR-3 | CA-12 |
| 7 | CR-4 | CA-8 |
| 7 | CR-5, CR-6 | CA-11 |
| 7 | CR-7 | CA-7 |
| 7 | CR-8, CR-9, CR-12 | CA-6 |
| 7 | CR-10 | CA-3 |
| 7 | CR-11, CR-14, CR-24 | CA-2 |
| 7 | CR-13 | CA-13 |
| 7 | CR-15 | CA-14 |
| 7 | CR-16, CR-17, CR-19, CR-20, CR-21, CR-29 | CA-17 |
| 7 | CR-18, CR-22, CR-23, CR-25, CR-26, CR-27, CR-28, CR-43 | CA-16 |
| 7 | CR-30, CR-31, CR-32, CR-33, CR-34, CR-35, CR-39, CR-40, CR-41, CR-42 | CA-20 |
| 7 | CR-36 | CA-4 |
| 7 | CR-37, CR-38 | CA-19 |
| 7 | CR-44, CR-45, CR-46 | CA-21 (CR-46's legacy lock also CA-6) |
| 7 | CR-47 | CA-6 |
| 8 | ZZ-1 | CA-1 |
| 8 | ZZ-2 | CA-5 |
| 8 | ZZ-3, ZZ-4 | CA-7 |
| 8 | ZZ-5 | CA-6 |
| 8 | ZZ-6 | CA-12 |
| 8 | ZZ-7, ZZ-11 | CA-8 |
| 8 | ZZ-8 | CA-11 |
| 8 | ZZ-9 | CA-3 |
| 8 | ZZ-10 | CA-2, CA-13 |
| 8 | ZZ-12 | CA-14 |
| 8 | ZZ-13 | CA-17 |
| 8 | ZZ-14 | CA-6 (legacy lock), CA-21 |
| 9 | IR-1, IR-13 | CA-1 |
| 9 | IR-2, IR-3, IR-15, IR-16, IR-19, IR-20, IR-21 | CA-15 |
| 9 | IR-4, IR-5, IR-6, IR-25 | CA-6 |
| 9 | IR-7 | CA-8 |
| 9 | IR-8, IR-10 | CA-9 |
| 9 | IR-9, IR-18 | CA-10 |
| 9 | IR-11 | CA-5 |
| 9 | IR-12, IR-17 | CA-11 |
| 9 | IR-14 | CA-12 |
| 9 | IR-22, IR-23, IR-24 | CA-18 |
| 9 | IR-26 | CA-2 |
| 9 | IR-27, IR-28, IR-29, IR-30, IR-31, IR-32, IR-33, IR-34, IR-35, IR-36, IR-37, IR-38, IR-40 | CA-21 |
| 9 | IR-39 | CA-3 |
| 9 | IR-41, IR-42, IR-43, IR-44, IR-45, IR-46 | CA-20 (IR-44's command text also CA-7) |

## Report files

This file is the final review output. The five stage reports it cites by
finding ID (ZO, CO, CR, ZZ, IR) were working files. They were removed on
2026-11-05 and remain in git history at commit `90b5d83`
(`git show 90b5d83:docs/reviews/2026-11-04-production-hardening/<name>`):

* `zoom_out_report.md` (Stage 5)
* `comprehension_report.md` (Stage 6, with the paraphrase)
* `critic_report.md` (Stage 7)
* `intermediate_zoom_out_report.md` (Stage 8, with the Adopt / Shrink /
  Reject table for every earlier recommendation)
* `implementation_readiness_report.md` (Stage 9)

Line numbers in these reports refer to the pre-review design and the 0.1.0
`pipeline.py`, both kept in git history.
