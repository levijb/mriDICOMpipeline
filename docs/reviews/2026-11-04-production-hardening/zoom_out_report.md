# Goldfish Review Report: Stage 5 (Initial Zoom Out)

**Target:** `docs/designs/production-hardening.md` (full file, lines 1-2152)
**Standard:** `zoom-out-standard.md`
**Mode:** Consolidated (uninterrupted); no edits were made to the target.

## Reviewer Status

| Reviewer | Status |
| --- | --- |
| `egm-zoom-out` | completed |

**Severity breakdown:** 0 Blocker, 7 Major, 5 Nit.

**Reviewer's overall verdict:** The motivation is clear, but the design falls
short on simplicity. Phase 1 is overloaded, and it ships fields and
optimizations nothing yet needs. The reviewer recommends revising for findings
1-7 before approval.

**Coverage:** The reviewer read the persona, the standard, all 2,152 lines of
the target, and the baseline context: the brief, the baseline spec, the
manifest, README, `pyproject.toml`, `MANIFEST.in`, `.gitignore`, CI workflow,
all of `src/`, `Scripts/` and `tests/`, and `examples/sessions.csv`. It did not
read `design_draft.md`. It ran no tests.

**Context sufficiency:** Passes. The objective, the reproduced problems
(P1-P11) and their severities are clear and supported by evidence.

**Reuse search:** The reviewer searched for existing signal handling,
`ExitStack`/`contextlib`, rlimit/prlimit, `ctypes`, `start_new_session`,
`.bak`, intent, marker and `last_seq` machinery. None exists, so the recovery,
lock and signal machinery is new. The design reuses `atomic_json`, `owned`,
`publish_tree`, the `ProjectLock` mechanism and the redaction patterns. The
only reinvention found is `utc_now` (finding 11).

**Judged already simple (keep):** Q10 (NIfTI pass-through, removing sanitizer
code), Q9 (narrowed to integrity checks), Q8 (record provenance only), the
single JSON index instead of SQLite or bucketed files, and the
thread-per-subject model.

## Prioritized Findings (Blockers -> Major -> Nits)

### ZO-1. Phase 1 carries non-safety work, contradicting Goal 4 - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** Goal 4 (lines 164-171); Phase map (390-394); Departures
  A1 (459); Q11 (925-961); Q1 (1204-1205); Module layout (1210-1221);
  Implementation order (1354-1367; packaging is step 1 at 1357, classifier
  revision 2 is in step 5 at 1361).
* **Discrepancy Description:** Goal 4 says phase 1 removes the
  priority-category risks "before any work on performance, packaging, or
  reporting". Phase 1 also contains:
  * A full split of `pipeline.py` into 13 modules, which the brief never
    raised.
  * Classifier revision 2 (Q11). Mislabels fall in none of the six priority
    categories, and the design accepts mixed folder layouts as a side effect
    (958-961).
  * The upload-marker protocol (R06, not a P-item; see ZO-5).
  * Index tuning (ZO-3) and geometry capture (ZO-2).
  * Packaging and license work, placed first in the order.

  Q1 concedes this is "the largest phase and the slowest to review". Every
  extra item enlarges the crash-injection and migration test surface the
  safety fixes rely on.
* **Rationale & Recommendation:**
  * Limit phase 1 to the P-item fixes, the schema-3 fields and `migrate` those
    fixes need, and the real-converter smoke tests as evidence.
  * Merge step 0 (move-only refactor) as its own deliverable before any
    behavior change.
  * Move Q11, markers, index tuning and geometry capture to later phases.
  * Packaging and license work is independent and can land any time, but
    should not be step 1 unless Goal 4 is reworded.

### ZO-2. The single-migration rule front-loads fields nothing reads - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** State schema 3 (354-369, especially 356-357 and 366); Q1
  (1201-1203); Q9 tag capture (904-910); Future Improvements (1330-1337);
  `prepare.py` (1578-1581, 1601-1603); `support.py` (1857-1860); test at
  1977-1978; Alternatives (1263).
* **Discrepancy Description:** Lines 356-357 put every later-phase field into
  phase 1 "so there is a single migration", which assumes any added field
  forces a migration. Additive optional fields do not need one: inventories
  are per-session files outside the index, and the design already defaults a
  missing field (schema-2 inventories without `sop_classes` are treated as
  all-required, 1449-1451). The main cost is geometry capture, seven
  per-object geometry and shape fields plus a per-file `series_key` (366,
  1601-1603):
  * "Nothing reads these fields in phase 1."
  * They serve a cross-check the doc calls "a starting point, not an approved
    design" (1336-1337).
  * They enlarge every inventory, which every `convert_one` and `--audit`
    reads.
  * They add fixture work and a test.
  * Pre-phase-1 sessions never get them anyway (1750-1752).
* **Rationale & Recommendation:** Adopt a rule that additive optional fields
  do not bump the schema and readers default them. Phase 1 then carries only
  fields it reads: `sop_classes`, `required`, `last_attempt`, `provenance`,
  `coverage`, `last_seq`, `accepted_multi_study`. Add geometry capture in the
  phase that approves the check. The owner approved the capture (Q9 history),
  so present this as a trade-off to reconsider: an unapproved feature against
  re-preparing the sessions prepared in between.

### ZO-3. Index-size tuning lands before measurement - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** Q21 (1068-1071, 1092-1095); Q18 (1053-1054); schema row
  at 365; `receipts.py` (1439-1442); `state.py` (1474-1476); `migrate.py`
  (1755); NFR Performance (1295-1298).
* **Discrepancy Description:** Two changes rest on estimates ("about 60 MB",
  "roughly halves the estimated index size"): relative directory-map keys, and
  raising the flush interval from 10 s to 300 s. The 20,000-session budgets
  are only measured in phase 3 (1048-1049, 1100-1101). Neither change is
  needed for the safety fixes: sequence numbers alone fix P11, and `.bak`
  alone covers R09. Relative keys change `receipt_info` and `fast_valid`, the
  cache path Goal 5 protects, and add a migration step. The 5-minute flush
  widens crash replay to five minutes of journals. This conflicts with
  baseline §18 ("Benchmark acceptance thresholds must be agreed before
  optimization claims"); the baseline 1,000-session rerun took about 3 s per
  command (spec §16.1). Deferring costs no migration: if `fast_valid` resolves
  `Path(entry["root"]) / key`, pathlib returns absolute keys unchanged, so one
  reader accepts both forms.
* **Rationale & Recommendation:** Keep `seq` and `.bak` in phase 1. Defer
  relative keys and the flush interval until the T15 numbers exist.

### ZO-4. Recovery has three adoption paths and an intent type that does nothing - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** Q12 intent table (662-673); recovery rules (1513-1523);
  `plan_prepare` adopt candidate (1633-1638); `plan_convert`
  unreferenced-folder rule (1692-1697); Q21 lost-index case (1088-1091);
  crash-matrix requirement (685-687).
* **Discrepancy Description:** "Adopt a receipt-bearing folder that state
  doesn't know about" is specified three times, each with a different match
  rule and its own tests:
  * The reconciliation row (671-672) matches "source digest matches the
    prepared content", which is undefined for an extraction folder with no
    state record.
  * `plan_prepare` hashes the ZIP and compares it with the extraction receipt.
  * `plan_convert` checks that the receipt is `complete` and its
    `source_sha256` matches.

  The `zip_move` intent rows (664-666) either clear the intent and defer to
  the planner, or flag "both exist". `move_archive` cannot produce "both
  exist" because it refuses an existing destination (`pipeline.py:507-508`).
  A single rename is already atomic, so the intent's only real content is its
  marker list, which no rule uses (ZO-5).
* **Rationale & Recommendation:**
  * Specify one adoption rule in one place, implemented once (the entry
    builders in `receipts.py` are already shared), and call it wherever an
    unrecorded folder is found.
  * Drop the `zip_move` intent kind.
  * Limit reconciliation to what planners cannot see cheaply: swap-folder
    roll-forward/rollback and stage cleanup (667-670, 673). This shrinks the
    T07 matrix, which needs one test per row.

### ZO-5. Required upload markers add a workflow change and an unhandled crash state - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** Q4 (733-761); Interface changes (381); intent schema
  (1505-1509); `move_archive` (1607-1609); `plan_prepare` (1633-1638); Q12 row
  at 664; Alternatives (1253-1254); tests (1933-1936).
* **Discrepancy Description:** Markers address R06, which is not one of the
  reproduced problems. Without markers, the doc's own trade-off line (1253)
  names only "noisy failures" plus writes that continue after the move:
  * A truncated ZIP fails to open (93) and is retried once its size or mtime
    changes.
  * Under Q3, a write that continues after the move produces a hold, not a
    replacement.

  In return, every upload needs a sidecar file. The design also adds a
  nonblocking `waiting for marker` state (the run exits 0 indefinitely,
  750-754), an adopt-candidate exception, a legacy exemption, an upgrade note,
  and a `.sha256` parser. Moving the marker splits the atomic ZIP rename into
  two renames, creating an unhandled crash state:
  * A crash between the two renames leaves a canonical ZIP with no marker, no
    `sources` entry and no extraction.
  * The planner reports it as `waiting for marker` forever (1633-1636).
  * The reconciliation row clears the intent and defers to the planner (664),
    and nothing uses the intent's `markers` list (1507-1508).

  The doc never weighs an optional-marker design: verify a `.sha256` marker
  when present, otherwise keep the size/mtime checks plus the Q3 hold.
* **Rationale & Recommendation:** Weigh optional against required markers; the
  owner reconfirmed required markers (733), so this asks them to revisit. If
  markers stay required, either do not move them (state already records
  acceptance and `archive_sha256`) or add an explicit reconciliation rule for
  stranded markers.

### ZO-6. The migration's false-partial remedy costs more than the alternative it rejected - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** CUJ 6 (289-298); Q22 (966-968, 999-1009);
  `receipts.output_series_keys` (1452-1455); `prepare_one` step 5
  (1625-1629); `migrate.py` (1757-1761); runbook entry (2090); Alternatives
  (1266).
* **Discrepancy Description:** Migration marks every old series as SOP class
  `unknown`, so any session with a non-image series becomes `partial` with a
  blocking issue (1759-1761), and every run exits 2 until each session is
  cleared. Vendor CSA/report series, secondary captures and presentation
  states are routine in exports, so this may hit most sessions; the doc gives
  no prevalence estimate. The remedy is `prepare --session KEY
  --force-reextract` per session: a full ZIP hash, full re-extraction and
  republish. That is strictly more I/O than the header-only reads Q22 rejected
  as taking "hours to days" (1266). The remedy also brings machinery that
  exists mainly to support it:
  * `--session` on `prepare`.
  * `prepare_one` step 5, which re-evaluates conversion coverage, a conversion
    concern placed in preparation that reads the NIfTI tree, which `prepare`
    does not lock (713-714).
  * A runbook procedure.

  A targeted option was not weighed: for only the sessions migrate step 3
  flags, read SOP Class and Series UID with `stop_before_pixels` from the
  already-extracted files. That needs no ZIP hash and no republish.
* **Rationale & Recommendation:** Weigh the targeted backfill. If adopted,
  step 5 and `--session` on `prepare` can likely go.

### ZO-7. Later-phase mechanisms are fixed at implementation level, and already contradict each other - Severity: Major
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** Overview (39-41); Non-Goals (206); contract containment
  row (351); Q14 (771-799); Q15 (801-850); Q23 (1011-1037); Q18 (1052-1064);
  Q2 Windows items (1117-1137).
* **Discrepancy Description:** Later phases "must follow this contract"
  (40-41), and their file-level plans are out of scope (206). Yet the document
  fixes phase 2-3 mechanisms in detail:
  * Per-archive child processes, with `RLIMIT_AS`/`RLIMIT_CPU` set "before
    importing pydicom".
  * A custom launcher, and an explicit ban on `preexec_fn`.
  * Windows Job Objects via `ctypes`.
  * Ten numeric limits, the retention rules, and the scope of what `relocate`
    rewrites.

  These harden before any phase 2-3 evidence exists, and they already
  conflict:
  * Q14 says Windows gets only a wall-clock limit and "the contract states
    this weaker containment" (786-788).
  * Q2 says Job Objects give Windows "the same caps as Linux" (1119-1123).
  * The contract row claims caps "on Linux and Windows" (351).

  The detail also hides a necessity question for phase 2. Q15's in-process
  size limits already bound decompression bombs (810), and where the site
  enforces it, the Slurm job's own memory limit applies. Neither is weighed
  against per-archive process isolation.
* **Rationale & Recommendation:** For phases 2-4, keep only the guarantee, the
  phase and the open questions. Move mechanisms, numbers and test names into
  those phases' own documents.

### ZO-8. Per-algorithm revision constants duplicate the package version - Severity: Nit
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** Q19 (545-546); Q8 recorded fields (630-635) and cache key
  (646-647); `CLASSIFIER_REVISION` (1528), `SANITIZER_REVISION` (1563),
  `SOP_LIST_REVISION` (1582-1583); migrate (985-987).
* **Discrepancy Description:** The classifier, sanitizer and SOP-class list
  ship inside the package, so the recorded package version already identifies
  them. Three hand-bumped constants add a maintenance step no test enforces,
  and feed only the "outdated" comparison Q8 rejected (1327-1329). Lines
  545-546 call the SOP-class list "part of the conversion cache identity",
  while 646-647 say the cache key is unchanged.
* **Rationale & Recommendation:** Record the package version only, and fix
  lines 545-546.

### ZO-9. Changing the content-digest definition creates migration work - Severity: Nit
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** Q20 (577-581); schema row at 366; migrate step 2
  (973-977); `migrate.py` (1753-1755); test at 2027.
* **Discrepancy Description:** Counting byte-identical repeats once changes
  `content_sha256`, so migration must recompute digests and rewrite each
  conversion's `source_sha256`, with a dedicated test. Writing each repeated
  object only once (the actual Q20 fix) does not require changing the digest.
* **Rationale & Recommendation:** Keep today's definition, which counts every
  copy (`pipeline.py:493`). This removes the digest recompute, the
  `source_sha256` rewrite, and the mismatch where schema-2 and schema-3
  digests of the same ZIP disagree so a duplicate looks like a conflict.

### ZO-10. Migrate's apply path has uncovered crash windows and an avoidable step - Severity: Nit
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** Goal 6 (178-183); `run_history.csv` change (384); Q22
  (970-997); NFR Rollback (1302-1307); `migrate.py` (1747-1748, 1767-1774);
  `reports.py` (1717-1718).
* **Discrepancy Description:**
  * **P11 survives in migrate.** Migrate replays schema-2 journals "in name
    order" (1747-1748), which is the P11 rollback pattern, at the one step
    every existing study goes through; a rollback there becomes the schema-3
    baseline.
  * **Interruption windows break Goal 6.** Because "a rerun on schema 3 is a
    no-op" (1773), a stop after step 2 leaves either schema-2 journals without
    `seq`, which the new `State` cannot interpret, or an unrotated history
    file with the old header. Goal 6 promises the migration either completes
    or leaves the project unchanged.
  * **History rotation exists for one column.** Rotating `run_history.csv` is
    needed only for the new `reconciled` column. Reconciliation already logs
    every action (1520).
* **Rationale & Recommendation:** Skip journals older than the index's write
  time (0.1.0 writes no journal during a flush). Have a rerun finish the
  remaining steps instead of being a no-op. Log the reconciled count, or start
  a new file when the header differs, to remove one migrate step and one
  rollback step.

### ZO-11. Decision sections drop to code level - Severity: Nit
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** Q20 (568-570); Q14 (789-791); Q10 (874-876); Q11
  tokenizing (949-951); Q18 (1057-1061); import convention (1220-1221,
  repeated at 1372-1374); `utc_now` (1433); classifier rule table
  (1537-1556).
* **Discrepancy Description:** The Decision Log includes loop expressions
  (`k.startswith(sid + "/")`), map shapes, the `preexec_fn` ban and module
  import style. The import convention exists to keep test patch targets
  working, which is a test concern. The 15-row classifier table repeats the
  T13 test table that lines 954-957 say will define the expected labels.
  `fsutil.utc_now() -> float (epoch seconds)` re-wraps `time.time()`.
* **Rationale & Recommendation:** Move these details to the implementation
  plan or the code. Keep decision sections at policy level: whole-token
  matching, specific rules before general ones, and passing `ImageType`.

### ZO-12. Status and phase labels contradict each other - Severity: Nit
* **Reviewer Lens:** `egm-zoom-out`
* **Affected Scope:** Goal 2 (156-160); Open Questions (211-239); Run sequence
  (315); Q15 (801, 804); Q2 (1112, 1142-1143); Q17 (1165, 1169); `state.py`
  plan (1462-1482); `ci.yml` plan (1838-1839).
* **Discrepancy Description:**
  * "Open Questions" contains only approvals and no open questions.
  * Q15, Q17 and the Q2 table still say "pending author confirmation" or
    "confirm at section review", although lines 218-224 list them as approved.
  * The run sequence shows the phase-3 relocate refusal (315) without a phase
    marker, and the phase 1 `state.py` plan does not implement it.
  * The CI real-converter matrix (2 OS x 2 versions, 1838-1839) is four jobs,
    but Q2 says "two real-converter jobs" (1142-1143).
* **Rationale & Recommendation:** Goal 8 promises no decision is left open, so
  make these labels agree.
