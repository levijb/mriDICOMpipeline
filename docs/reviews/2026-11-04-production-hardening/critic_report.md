# Goldfish Review Report: Stage 7 (Critic Review)

**Target:** `docs/designs/production-hardening.md` (FeatureRange, lines 1-1340)
**Standards:** `egm-design` and `egm-structural` used `feature-spec-standard.md`;
`egm-style` used its persona's own criteria (no standard supplied).
**Mode:** Consolidated (uninterrupted), one round, no fixes applied. Findings
were deduplicated across the three reviewers; merged findings list every lens
that raised them, and the highest severity wins.

## Reviewer Status

| Reviewer | Status | Raw result |
| --- | --- | --- |
| `egm-design` | completed | 1 Blocker, 14 Major, 7 Nit |
| `egm-structural` | completed | 0 Blocker, 6 Major, 4 Nit |
| `egm-style` | completed | 0 Blocker, 10 Major, 12 Nit |

**After deduplication:** 1 Blocker, 28 Major, 18 Nit.

**Verdicts:** `egm-design`: not ready to approve. `egm-structural`: heading
tree valid; one required section (Risks & Mitigations) missing. `egm-style`:
several claims and labels need calibration.

**Standard compliance (design and structural lenses):** Problem (54-145),
Goals and Non-Goals (147-209), Architecture (300-461) and Alternatives
(1223-1283) are present. Risks & Mitigations is missing (CR-16). No heading
levels are skipped, the code block at 308-331 is closed, all six in-document
anchors and the three file links resolve, and every table has consistent
column counts.

**Elephant verification of the Blocker:** `publish_tree` falls back to
`shutil.copytree(stage, incoming)` on a cross-device rename
(`src/mri_dicom_pipeline/pipeline.py:107-113`), and the Q12 roll-forward row
(line 669) conditions only on "`.incoming_*` has its receipt". The mechanism
the reviewer describes is present in both the code and the design.

**Checked by `egm-style` and found accurate (no change needed):** the 19-file
manifest and `pipeline.py` SHA-256; 10 s index flush and 15 s status rewrite
(`pipeline.py:233`, `:982`); unsorted journal deletion vs sorted replay (`:238`
vs `:215`); hex-timestamp journal names; "performs no hashes" help text
(`:336`); Q11 substring mislabels ("HEADCOIL" contains "EADC", "PROGRESSIVE"
contains "GRE"); 20-hex series key; 16 MiB extension cap and gzip settings;
14,400 s timeout; 10 MB x 3 log rotation; no `run_history.csv` rotation;
per-subject key scan; `session_date` in every sidecar; full-ZIP hash in
`prepare_one`; Python 3.11 classifier with no 3.11 CI job; Slurm `KillWait`
30 s; `setuptools>=77` for PEP 639; Python 3.10 end-of-life date.

---

## Prioritized Findings (Blockers -> Major -> Nits)

### CR-1. Roll-forward and adoption trust the receipt being present, so a partly copied folder becomes `converted` - Severity: Blocker
* **Reviewer Lens:** `egm-design` (B1); verified by the Elephant (see above).
* **Affected Scope:** Q12 "Roll forward" and "Adopt" rows (662-673); 683-687;
  Q21 empty-state adoption (1088-1091); CUJ 2 (259-260); invariants 3 and 5
  (343, 345); schema rows 364 vs 367.
* **Discrepancy Description:**
  * On a cross-device rename, `publish_tree` falls back to
    `shutil.copytree(stage, incoming)`. This is the normal path whenever
    `convert` uses node-local `--staging-root`, which the design supports
    (677-679, 847-849).
  * `copytree` creates `.incoming_*` first and fills it in directory-listing
    order, so the top-level `.pipeline_complete.json` (`complete: true`) and
    `.inventory.json` can land before the scan-type subfolders finish.
  * A SIGKILL mid-copy (30 s after SIGTERM, CUJ 2) or node loss leaves
    `.incoming_*` with a receipt but missing or truncated `.nii.gz` files.
  * Reconciliation then rolls it forward (669), the Adopt row records it and
    rebuilds receipt metadata "without reading images". The fast cache check
    passes from then on, and Q7 forbids automatic reconversion. The result is
    false success and possible image corruption, both in the document's own
    Critical list (84).
  * Q21 empty-state adoption (1088-1091) also re-approves a complete folder
    from which volumes were deleted; normally that would raise `output
    modified`.
  * The adoption rows do not say how series coverage (Q6) is rebuilt;
    `coverage` lives in state (364), not in the receipt file (367).
* **Rationale & Recommendation:**
  1. Make `.incoming_*` appear only when complete: copy across devices into a
     `.copying_*` sibling, fsync, then rename to `.incoming_*`; or write the
     receipt last, after the rest is fsynced.
  2. Before any roll-forward or adoption, stat every inventory file and check
     its size. Stat-only and recovery-only, so Goal 5 holds. On mismatch,
     change nothing and raise an issue.
  3. Put coverage in the conversion receipt, or recompute it from sidecars
     during adoption.
  4. Add T07 crash points inside the cross-device copy, and an adoption test
     with a deleted volume.

### CR-2. The reconciliation table has unhandled cases - Severity: Major
* **Reviewer Lens:** `egm-design` (M1)
* **Affected Scope:** 662-673, 259-260, 713-714, 742-757.
* **Discrepancy Description:**
  * **(a) Re-publication over an existing record.** A successful
    `--force-reconvert` or `--force-reextract` that crashes after the final
    rename but before the event is recorded matches "Destination present, no
    swap folders -> Done; clear the intent", but state still holds the old
    receipt signature, directory map and provenance. The next run raises a
    blocking `output modified` for a folder the pipeline itself wrote. This
    contradicts CUJ 2's "adopts any folder that was published but not yet
    recorded"; the Adopt row covers only "no state record".
  * **(b)** "Destination present and `.previous_*` present -> Remove
    `.previous_*` only if ..." has no else-branch.
  * **(c)** The ZIP-move rows ignore markers (756-757). A crash between the
    ZIP rename and the marker rename leaves a ZIP without its marker; the
    planner reports a nonblocking `waiting for marker`, the run exits 0, the
    session stalls silently, and a `.sha256` checksum is never verified.
  * **(d)** The run sequence has every command reconcile every intent, but
    `convert` has no `--input-root` and does not lock the upload root
    (713-714), so it should not reconcile ZIP-move intents.
* **Rationale & Recommendation:** When state already has a record, compare
  the destination's receipt ID with the one in state; if it differs and
  matches the intent's stage receipt, adopt it (with the CR-1 checks). Define
  the missing else-branch (keep both, raise an issue). Record markers in the
  ZIP-move intent and reconcile them. Reconcile each intent only under locks
  that cover its roots. Add T07 cases for each.

### CR-3. The 20-second shutdown promise is unbacked, and termination handling has races - Severity: Major
* **Reviewer Lens:** `egm-design` (M2)
* **Affected Scope:** 252-260, 382, 692-694, 816, 1079-1081, 1096-1097,
  1205-1208.
* **Discrepancy Description:**
  * After stop, each of four workers still validates, gzips (level 6) and
    SHA-256s every partial volume, then publishes to `.pending/` (a
    cross-device copy with node-local staging). The coordinator then flushes a
    30-60 MB index and rewrites reports on network storage. Nothing bounds or
    skips this work, and no measurement supports 20 s; the margin before
    Slurm's KillWait is 10 s. Missing it moves the orderly path onto the
    SIGKILL path, in the CR-1 window.
  * Slurm signals every process in the job step (cgroup tracking), so
    dcm2niix gets SIGTERM directly and can exit -15 before the stop event is
    set. Current logic then classifies the session `partial`/`failed` (not
    retried) instead of `interrupted`, contradicting CUJ 2.
  * "Same orderly stop path as Ctrl+C" (692-693) means the handler raises at
    an arbitrary point. That can interrupt a flush between its two renames
    (1079-1081), and `finally` re-runs the flush on a half-renamed state.
* **Rationale & Recommendation:** After stop, skip organize, gzip and
  publish; keep the stage and record `interrupted` with the stage path. Make
  the handler only set a flag the coordinator checks. Classify any converter
  exit seen after a termination signal, or any negative return code, as
  `interrupted`. Give each shutdown step a time budget and measure it with
  large synthetic outputs and node-local staging. Document `sbatch
  --signal=B:TERM@<seconds>` in the runbook.

### CR-4. Missing state silently means "new project", even when the roots are full of data - Severity: Major
* **Reviewer Lens:** `egm-design` (M3)
* **Affected Scope:** 314, 742-744, 756, 1016-1018, 1084-1091.
* **Discrepancy Description:** If neither `index.json` nor `.bak` exists, the
  project is treated as new (1088-1089). Plausible triggers: a mistyped
  `--report-root` (the planned lock code creates missing roots, 1492), a
  deleted `Reports/` folder, or a restored old backup. The Q23 moved-project
  guard fires only when the index is not empty (1016-1018). Because every
  phase-1 canonical ZIP keeps its marker (756), `prepare` treats every one as
  new: hashing every ZIP, re-extracting every session and swapping every tree
  under a parallel state. Returning to the real report root then finds stale
  directory times and forces a second full re-preparation, days of I/O at
  20k sessions. The text (1084-1088) also covers only the both-corrupt case,
  not index-missing/.bak-corrupt or index-corrupt/.bak-missing.
* **Rationale & Recommendation:** Write a small project-identity file in each
  managed root and check it at startup. Refuse to start with empty state while
  any root holds receipts, unless an explicit new-project or adopt option is
  given. Specify all four missing/corrupt combinations.

### CR-5. "Restore the folder" cannot work, so `output modified` always ends in reconversion - Severity: Major
* **Reviewer Lens:** `egm-design` (M4)
* **Affected Scope:** CUJ 4 (271-279), 344, 596-602, 958-961.
* **Discrepancy Description:** The cache check compares stored directory
  mtimes. Undoing an edit changes the directory time again, and a re-gzipped
  file has different bytes, so the folder never passes again. The only exit
  from the blocking issue is `--force-reconvert`, which may use a newer
  converter (mixed provenance and folder layouts, 958-961) for a trivial edit.
  Filesystem clutter takes the same path with no researcher involved:
  `.DS_Store`, `Thumbs.db`, `desktop.ini`, NFS `.nfsXXXX` files.
* **Rationale & Recommendation:** Add a revalidate path (for example `convert
  --session KEY --revalidate`) that runs the existing audit, requires no files
  outside the inventory, and on success rebuilds receipt metadata without
  reconverting. Define "restore" in the contract as this revalidation.

### CR-6. Strict coverage has no operator override, so some sessions stay `partial` forever - Severity: Major
* **Reviewer Lens:** `egm-design` (M5)
* **Affected Scope:** 281-287, 343, 492-497, 541-546, 1239-1240.
* **Discrepancy Description:** Some MR Image Storage series cannot be
  converted by dcm2niix (unsupported transfer syntaxes, inconsistent
  dimensions or geometry). Reconversion gives the same result, and there is no
  override like `--accept-multi-study`. Eleven good series never reach
  `Niftis/Subjects/`, and every `convert` run exits 2 permanently, which wears
  down the meaning of exit code 2. The known-failure list covers only the
  opposite case (541-544).
* **Rationale & Recommendation:** Add a recorded override (for example
  `--accept-missing-series SUBJECT/SESSION`) stored with run ID and series
  keys, shown as "excused by operator", publishing the session as `converted`.
  Add the case to invariant 3's exceptions with a test.

### CR-7. Migration's false-`partial` impact and clearing cost undercut its alternatives analysis - Severity: Major
* **Reviewer Lens:** `egm-design` (M6). Cross-stage: same issue as ZO-6.
* **Affected Scope:** 58-62, 289-298, 481-482, 557-560, 574-576, 966-968,
  999-1008, 1266-1267.
* **Discrepancy Description:** `unknown` SOP class counts as required. Siemens
  exports commonly include CSA Non-Image objects, and other vendors include
  report or secondary-capture objects, so many converted sessions may flip to
  `partial` at upgrade. Every run exits 2, downstream `converted` filters drop
  these sessions, and clearing needs one `prepare --session KEY
  --force-reextract` per session (full hash, re-extraction, tree swap with
  double disk space), which is more I/O than the rejected header-read
  alternative (1266). Clearing fails outright for multi-study ZIPs and
  conflicting repeated instances (Q20 holds them) and for sessions whose
  canonical ZIP is missing (481-482).
* **Rationale & Recommendation:** Make `migrate --dry-run` report the affected
  count and re-extraction bytes, and decide with that number. Consider a
  distinct nonblocking status for migration-only coverage gaps. Provide batch
  clearing or a resumable header-only SOP-class backfill for affected
  sessions. Document which sessions cannot be cleared.

### CR-8. Migration rewrites `source_sha256` unconditionally, which can make stale output look current - Severity: Major
* **Reviewer Lens:** `egm-design` (M7)
* **Affected Scope:** 94, 589-592, 620-622, 972-977.
* **Discrepancy Description:** Step 2 updates the conversion's
  `source_sha256` whenever `content_sha256` is recomputed. A schema-2 session
  already stale through the P3 branch (status `needs_conversion`) that also
  has byte-identical repeated instances gets its old conversion relabelled as
  current: a false `converted`. The spec also does not say which rule wins for
  `needs_conversion` sessions with a complete folder: Q7 (explicit command
  only) or the current selection logic (automatic reconversion).
* **Rationale & Recommendation:** Rewrite `source_sha256` only when the old
  value equalled the old `content_sha256`; otherwise keep `needs_conversion`
  and list the session in the dry run. Decide explicitly between automatic
  reconversion of stale complete sessions (a Q7 exception) and a `stale
  output` hold, and test both.

### CR-9. Swap folders left by 0.1.0 crashes (P5) are never reconciled - Severity: Major
* **Reviewer Lens:** `egm-design` (M8)
* **Affected Scope:** 95, 128, 600-602, 662-673, 970-989.
* **Discrepancy Description:** Reconciliation acts only on intent files, and
  0.1.0 never wrote any, so the `.incoming_*`/`.previous_*` folders reproduced
  at line 128 stay unreported while Goal 4 counts P5 as fixed. If a 0.1.0
  crash left the destination missing, the only copy of a complete output is in
  `.previous_*`, and Q7 raises a blocking `output modified` (600-601) with no
  pointer to it.
* **Rationale & Recommendation:** Have `migrate` list each session's parent
  directory once for swap-named siblings, report them in the dry run, then
  apply the Q12 rules (with CR-1 checks) or raise issues that name them.

### CR-10. Schema 3 cannot support Q15 retention, so the single-migration claim fails - Severity: Major
* **Reviewer Lens:** `egm-design` (M9). Cross-stage: tension with ZO-2.
* **Affected Scope:** 356-357, 364, 825-850, 1201-1203.
* **Discrepancy Description:** Retention ages each attempt "from the creation
  time recorded in the attempt's state record" (837-840) and expects many
  attempts per unresolved session (845-846). Schema 3 keeps only one
  `last_attempt` (364), and every retry overwrites the failure record. Earlier
  `.pending/<attempt>/` folders and `.work/convert_*` stages (whose names omit
  the session) lose their state record, so phase 2 must either fall back to
  directory times (which Q15 rejects) or never delete them; either way a
  second schema change is needed.
* **Rationale & Recommendation:** Add a per-session `attempts` list (path,
  kind, creation time, run ID, status, diagnostics removed, stage path),
  appended on every failed, partial or interrupted attempt, with
  `last_attempt` pointing at the newest entry.

### CR-11. The spec contradicts itself on whether phase 1 is a production deployment - Severity: Major
* **Reviewer Lens:** `egm-design` (M10); `egm-style` (S21, phase tags) folded in.
* **Affected Scope:** 344, 403-404, 432-434, 1114, 1205-1208, 1280-1281,
  1289-1292, 1299-1301; phase tags at 255-257, 773-776, 1117-1137.
* **Discrepancy Description:** Release gates make phase 4 the first release,
  but Rollout speaks of "the phase 1 release" and the Q1 alternatives compare
  what "stays open in the first release" as if it followed phase 1. If phase 1
  runs on production data: archives are parsed in-process with no size,
  member or free-space limits (phase 2); Windows has no whole-process-tree
  termination; shared storage is unsupported until the site two-node lock
  check passes (344, 1114). If it does not, the Critical fixes (P1, P3, P10)
  wait through phases 2 and 3. Related: Q14 is written in the present tense as
  if it applies in phase 1 (but line 1290 says phase 1 still parses
  in-process); Q2's Windows list mixes phase 1 and phase 2 items; CUJ 2's 20 s
  shutdown comes from the phase 2 Q15 table while SIGTERM handling is phase 1.
* **Rationale & Recommendation:** State whether phase 1 is a production
  pre-release. If yes, define its gates (at least G02 site lock check, G03,
  G04 and G07 CI checks) and move a minimal byte, member and free-space cap
  into phase 1. If no, explain how studies are protected meanwhile. Add a
  phase tag to each Decision Log entry and each CUJ.

### CR-12. The rollback procedure leaves schema-3 journals for 0.1.0 to replay - Severity: Major
* **Reviewer Lens:** `egm-design` (M11)
* **Affected Scope:** 362, 1079-1083, 1302-1307.
* **Discrepancy Description:** Journals newer than `.bak` survive every flush
  (1081-1083), so after any phase-1 run `.state/pending/` holds schema-3
  journals. 0.1.0 replays every `*.json` there with no version check, applies
  unknown issue keys (`wait:`, `missing:`, `output:`, `state:recovered`), and
  saves the mixture as schema 2. Those issues stay unresolved forever (every
  run exits 2), sessions are reprocessed with the P10 behavior that deletes
  added files, and a later re-`migrate` meets an impure schema-2 index.
* **Rationale & Recommendation:** Make rollback move `pending/`, `intents/`
  and `index.json.bak` aside, or have `migrate` keep a full `.state.v2/` copy
  that rollback restores. List in the runbook the P3/P10 behaviors that return
  under 0.1.0, and add a rollback test.

### CR-13. Windows "files in use" handling covers publication only; an open report aborts the run - Severity: Major
* **Reviewer Lens:** `egm-design` (M12)
* **Affected Scope:** 398, 1055-1056, 1105-1107, 1134-1137.
* **Discrepancy Description:** Q2 says Windows limitations must be fixed or
  tested, but only publication renames are covered. Reports and state are
  replaced by `os.replace` every 60 s and at exit, and `run_history.csv` is
  appended to. Excel holds `session_status.csv` and `issues.csv` open in a way
  that blocks replacement on Windows, and those are the files researchers are
  told to open. Today a failed `reports()` call leaves the coordinator loop,
  fails again in `finally`, and ends with `RUN ABORTED`, exit 2 and no history
  row. Index and `.bak` renames are exposed the same way to antivirus and to
  lockless dry-run readers.
* **Rationale & Recommendation:** Make report-write failures non-fatal (log,
  retry next cadence, write a per-run side file at exit). Retry state renames
  with bounded backoff. Add Windows tests that hold each report and state file
  open during a run.

### CR-14. The design's assumptions about dcm2niix are untested and have no fallback - Severity: Major
* **Reviewer Lens:** `egm-design` (M13)
* **Affected Scope:** 77-78, 91, 134, 501-507, 550-552, 913-918.
* **Discrepancy Description:** Three untested hypotheses carry the design:
  Q6 depends on dcm2niix writing SeriesInstanceUID unchanged as the second
  underscore-separated part of each output name (`-f series_%j_...`);
  invariant 3 requires exit code 0; Q19 assumes non-image objects do not
  change the exit code. None has a fallback. dcm2niix documents a nonzero code
  for "converted some but not all of the input DICOMs"; if it fires on ignored
  CSA Non-Image objects, every affected Siemens session becomes `partial`.
* **Rationale & Recommendation:** Run the real-converter smoke tests first, as
  a spike, before Q6 and Q19 are built on these assumptions (the plan has them
  at step 10 of 11). Decide the fallback now: pass dcm2niix only
  required-class objects (non-image objects in a sibling folder), or run it
  once per required series, which makes the series-to-output mapping true by
  construction at about 12 process starts per session.

### CR-15. Q10 pass-through removes a privacy control and adds no detection - Severity: Major
* **Reviewer Lens:** `egm-design` (M14)
* **Affected Scope:** 348, 852-870, 1259-1260.
* **Discrepancy Description:** Publishing exact converter bytes lets any NIfTI
  extension or header text the converter writes reach published outputs;
  0.1.0 zeroed these. Invariant 8's channel list (348) names header text but
  not extensions. T14 tests only today's converter, so future converters are
  unbounded. Both alternatives considered change bytes; keeping bytes while
  detecting and reporting was not considered.
* **Rationale & Recommendation:** Keep pass-through, but record extension
  presence (count and codes) in provenance; the validator already reads that
  header area. Raise an issue for unknown codes, or fail closed on them. List
  extensions in invariant 8.

### CR-16. Missing Risks & Mitigations section - Severity: Major
* **Reviewer Lens:** `egm-structural` (F1); `egm-design` (N5) merged.
* **Affected Scope:** whole range; risk statements scattered at 541, 601,
  726, 819-823, 844-846, 911-912, 958-961, 999-1001, 1096-1099, 1157-1160,
  1204-1205, 1217-1219; security exposure only at 1289-1291.
* **Discrepancy Description:** The standard requires Risks & Mitigations
  (failure modes, security implications, scalability limits, and how each is
  mitigated). The content exists, mostly labelled "Known failure mode,
  accepted", but is spread over 12 Decision Log entries and one NFR sentence,
  with no risk paired to a mitigation or explicit acceptance. Several accepted
  items state no mitigation (844-846, 958-961, 1157-1160). The largest
  security risk, untrusted archives parsed in-process without caps during
  phase 1 (1289-1291; containment only in phase 2, 351, 396), is mentioned in
  passing, while Q14 (773-788) reads as if containment is in force. The design
  lens adds that newly introduced risks (reconciliation's automatic actions,
  rollback, mixed versions, the marker protocol) are also unregistered.
* **Rationale & Recommendation:** Add an H2 Risks & Mitigations table: risk,
  source (Q/P/R ID), phase exposed, mitigation or acceptance, owner, test,
  remaining risk, with a link to each Decision Log entry.

### CR-17. Alternatives Considered credits rejections to the author without a record or concrete trade-off - Severity: Major
* **Reviewer Lens:** `egm-structural` (F2); `egm-design` (N4) merged.
* **Affected Scope:** 1226-1230, 1262; same pattern at 454, 880-885; table
  rows 1234-1235, 1238, 1248, 1263, 1273; Q9 mitigation 909-910.
* **Discrepancy Description:** The Alternatives Rule forbids attributing a
  rejection to a maintainer without linking an issue, discussion or PR. Line
  1227 credits every row to the author and 1229-1230 say no further rationale
  exists. Line 1262 ("implementation time") is the deciding reason for the
  largest departure from the brief (priority 6), with no estimate. The
  repository has no commits, so there is nothing to link; the rationale must
  stand as a concrete trade-off. Secondary issues:
  * No "chosen option" column and no links to Decision Log entries.
  * "Document scope" rows (1234-1235) and the "Q9 Revert" row (1263) have no
    matching Decision Log entry.
  * Row order differs from the log (Q5 before Q6 at 1238).
  * The Q9 mitigation is overstated (909-910): in Enhanced MR multi-frame
    objects, position and orientation sit in per-frame functional groups, so
    capture stores nulls (Future Improvements at 1336 already excludes
    multi-frame).
  * The SQLite rejection (1273) cites unreliable network-filesystem locks,
    which Q13 relies on anyway.
  * The Q8 "one converter call per run" cost (1248) does not apply to
    comparing stored algorithm revisions, which phase 1 itself bumps.
* **Rationale & Recommendation:** Replace attribution with concrete
  trade-offs (effort or test burden against benefit). Add a chosen-option
  column and Decision Log links, add entries for the orphan rows, align the
  row order, and correct the three weak rationales.

### CR-18. "Open Questions" has no open questions, and stale "proposed"/"pending" labels contradict the approvals - Severity: Major
* **Reviewer Lens:** `egm-structural` (F3); `egm-style` (S3, S4); `egm-design`
  (N1, part). Cross-stage: same issue as ZO-12 and CO-1.
* **Affected Scope:** 211-239 against 160, 571-572, 603-606, 801, 803-806,
  1112, 1165, 1169.
* **Discrepancy Description:** The section says all 23 questions are answered
  (213), then lists approvals under a bold pseudo-heading (218). Goal 2's
  evidence (160) requires it to hold "only items that carry a phase and a
  reason"; only the bullet at 222 has a phase. Where the approved items are
  defined, they are still marked unsettled:

  | Item | Approved at | Defined at | Status there |
  | --- | --- | --- | --- |
  | Q15 | 222 | 801, 804, 806 | "pending author confirmation"; "proposed here for review at the section gate"; column "Proposed default" |
  | Q17 | 223 | 1165, 1169 | "other parts proposed"; "to confirm at the section gate" |
  | Q2 | 224 | 1112 | "proposed, confirm at section review" |
  | Q7 | 228 | 603-606 | "Proposed: a repeatable `--session`" |
  | Q20 | 233 | 571-572 | "(proposed: `--accept-multi-study`)" |

  Bullet labels are also inconsistent (222-223 no colon, 224-233 "Qn:", 236
  and 238 no ID, 239 ends with "(R16)").
* **Rationale & Recommendation:** Rename the section (for example "Decisions
  approved with the Technical Plan") or keep only genuinely open items with
  phase and reason, and state "No open questions remain". Change each stale
  label to "approved 2026-11-04".

### CR-19. The Decision Log is hard to navigate - Severity: Major
* **Reviewer Lens:** `egm-structural` (F4); `egm-style` (S16) merged.
  Cross-stage: CO-10 (ordering).
* **Affected Scope:** 463-1222 (about 760 lines, 57% of the range).
* **Discrepancy Description:** Entries are not in numeric order (Q3, Q6, Q19,
  Q20, Q7, Q8, Q12, Q13, Q4, Q5, Q14, Q15, Q15, Q10, Q9, Q11, Q22, Q23, Q18,
  Q21, Q2, Q16, Q17, Q1, Module layout), with no index or explanation. Q
  references are never linked (169, 176, 302, 335, 390-401, 1234-1283). Q
  numbers appear at 136, 169 and 176 before being introduced at 213. Q15 has
  two H4 entries (801, 825); the diffusion half of Q11 is decided inside Q9
  (919-923) with no pointer from Q11 (925); "Module layout" (1210) has no
  ID while 213 counts exactly 23 questions. Status suffixes in headings vary
  (465, 624, 763, 801, 878, 1165) and make anchors long and fragile; every
  heading repeats the same date.
* **Rationale & Recommendation:** Sort Q1-Q23 or add an index table (ID,
  topic, status, phase, link). Use one heading per ID (or Q15a/Q15b),
  cross-link the diffusion decision, give Module layout an ID, move status
  into a line inside each entry, keep dates only where they differ, and link
  Q references.

### CR-20. Subsections sit under parents whose scope they do not match - Severity: Major
* **Reviewer Lens:** `egm-structural` (F5)
* **Affected Scope:** 1287-1337, 300-304.
* **Discrepancy Description:** Additional Resources (1309) and Future
  Improvements (1320) are filed under Non-Functional Requirements but are
  neither; Future Improvements is the target of deferred-work citations (454,
  458, 909). The Technical Plan intro (302-304) names five contents, but the
  section also holds Release gates (429), Departures from the brief (447) and
  the Decision Log (463), which make up most of it.
* **Rationale & Recommendation:** Promote both subsections to H2. Extend the
  Technical Plan intro to name all eight subsections, or move the extra three
  out.

### CR-21. The eight review areas are not carried into the behavior contract - Severity: Major
* **Reviewer Lens:** `egm-structural` (F6). Cross-stage: same issue as CO-6.
* **Affected Scope:** 33-35, 43-52, 151-155, 339-352.
* **Discrepancy Description:** The Overview and Goal 1's title promise a
  contract for "the eight review areas"; Goal 1's body and evidence refer to
  "the ten invariants in baseline spec §19", and the table has no area column.
  Area 8 (compatibility, packaging, release) has no contract row (covered only
  by G07 at 444, Q2 and Q17); migrations (part of area 5) are covered only by
  Goal 6. A reader cannot check Goal 1 against the table.
* **Rationale & Recommendation:** Add an area column or an area-to-invariant
  map, and make Goal 1's title and body agree.

### CR-22. Claims of passing tests and working behavior rest on a run this document did not do - Severity: Major
* **Reviewer Lens:** `egm-style` (S1)
* **Affected Scope:** 26, 141-145, against 137 and 455.
* **Discrepancy Description:** Line 26 says "Version 0.1.0 passes its
  synthetic tests", and 141-144 state four "never" properties as current
  fact. Line 137 says the 30-case suite was "Not run" here; the only pass
  evidence is baseline spec §16.1 ("recorded results, not a new test run").
  Named tests exist but are not cited
  (`test_1000_cached_sessions_do_not_read_scan_contents`,
  `test_path_traversal_member_cannot_escape`,
  `test_partial_outputs_retained_and_retry`).
* **Rationale & Recommendation:** Write "recorded as passing in baseline
  §16.1; not re-run for this document", and give the test name or "code trace"
  for each property.

### CR-23. P8's severity contradicts the document's own scale - Severity: Major
* **Reviewer Lens:** `egm-style` (S2); `egm-design` (N2, part) merged.
* **Affected Scope:** 98, against the scale at 82-87; Departures row 3 (456).
* **Discrepancy Description:** The scale defines Critical as including
  "sensitive-data disclosure" (84) and Medium as "Cost, noise, or a gap in
  honesty". P8, unredacted converter output written to disk, is a disclosure
  channel. Departure row 3 (456) says the severity requirement is "Resolved",
  so that label is also off.
* **Rationale & Recommendation:** Relabel P8 Critical (or High if the crash
  trigger decides it), or state why Medium applies.

### CR-24. Windows containment is described three different ways - Severity: Major
* **Reviewer Lens:** `egm-style` (S5). Cross-stage: same issue as ZO-7.
* **Affected Scope:** 786-788 (Q14), 1119-1123 (Q2), 351 (contract).
* **Discrepancy Description:** Q14: "On Windows only the wall-clock limit
  applies. The contract states this weaker containment." Q2: Job Objects give
  "Windows the same caps as Linux". The contract row (351) says caps apply "on
  Linux and Windows" and does not state the weaker containment Q14 says it
  states.
* **Rationale & Recommendation:** Keep one statement and update the other two
  to match.

### CR-25. Q19 and Q8 disagree on whether the SOP class list is part of the cache identity - Severity: Major
* **Reviewer Lens:** `egm-style` (S6); `egm-design` (N1, part) merged.
  Cross-stage: same issue as ZO-8 and CO-7.
* **Affected Scope:** 545-546 (Q19) against 630-635 and 646-647 (Q8).
* **Discrepancy Description:** Q19 says the MR SOP class list is "part of the
  conversion cache identity (Q8)"; Q8 says the cache key "is unchanged" and
  the list revision is only recorded. These lead to opposite behavior after a
  list change.
* **Rationale & Recommendation:** In Q19, write "recorded in provenance (Q8)".

### CR-26. Q7 says converter changes are "reported", but Q8 says nothing reports them - Severity: Major
* **Reviewer Lens:** `egm-style` (S7)
* **Affected Scope:** 590-591 against 626-628 and 642-643.
* **Discrepancy Description:** Q7: "Converter or algorithm changes (Q8) are
  reported only." Q8: the pipeline "never reports a session as 'outdated'" and
  "nothing flags them".
* **Rationale & Recommendation:** Change "reported only" to "recorded only".

### CR-27. "Phase 1 closes P1 to P11" overstates the result - Severity: Major
* **Reviewer Lens:** `egm-style` (S8). Cross-stage: same issue as CO-7.
* **Affected Scope:** 1194, against 642-643, 458 and Goal 4 evidence (170-171).
* **Discrepancy Description:** Q8 and Departure 5 say P2 "stays open in
  practice, deliberately". Goal 4 requires a regression test that fails on the
  baseline for each phase 1 item, and P2 (and P9) by design has none.
* **Rationale & Recommendation:** Write "closes P1 and P3-P11; P2 is recorded
  but left open (Q8)", and adjust Goal 4's evidence for P2 and P9.

### CR-28. Two release-gate pass conditions are not accurate or objective - Severity: Major
* **Reviewer Lens:** `egm-style` (S9)
* **Affected Scope:** 431-432 (promise of "objective pass conditions"), 443
  (G06), 444 (G07).
* **Discrepancy Description:** G06 says "20,000 sessions take at most 5 min
  and use at most 2 GB", but the Q18 budgets (1048-1049) apply to a cached
  rerun and to coordinator peak memory; without the qualifiers G06 reads as a
  full-processing budget. G07's "install cleanly" is not checkable.
* **Rationale & Recommendation:** Restore the Q18 wording. Define the install
  condition (for example "installs into a fresh virtual environment without
  errors and `tests/check_distribution.py` passes").

### CR-29. References to drafting sessions and workflow steps that later readers cannot resolve - Severity: Major
* **Reviewer Lens:** `egm-style` (S10); `egm-structural` (F9, EGM part)
  merged. Cross-stage: same issue as CO-10.
* **Affected Scope:** 75 and 1226 ("this drafting session"), 933 ("this
  session"), 116 ("session scratchpad"), 137 and 455 ("the sandbox", "this
  environment"), 80, 432, 624, 733, 878 ("brief reconciliation"), 804 and 1169
  ("section gate"), 1112 ("section review"), 1226 ("decision gates"), 1317
  ("EGM", never spelled out).
* **Discrepancy Description:** A new team member cannot tell which events or
  places these refer to, and "session" collides with the MRI meaning.
* **Rationale & Recommendation:** Replace each with a dated, named event (for
  example "author review, 2026-11-04") or drop it. Spell out EGM, or remove
  the process-tool links from Additional Resources.

### CR-30. Two tables use layout workarounds - Severity: Nit
* **Reviewer Lens:** `egm-structural` (F7); `egm-style` (S17) merged.
* **Affected Scope:** 406-427, 664-673.
* **Discrepancy Description:** The R-register and T-group tables share one
  grid with a blank spacer column, suggesting R01 pairs with T01. Empty
  T-cells at 426-427 look like "unassigned rows" in Goal 3's evidence wording
  (163). No lead-in says this is the Goal 3 mapping, and the R14 cell (421)
  holds a full out-of-scope rationale. The Q12 table imitates merged cells
  with blank first cells, so rows 665-666, 668-670 and 672 lose their intent
  type when read alone.
* **Rationale & Recommendation:** Split the first table in two, each with a
  lead-in sentence. Repeat the intent type on every Q12 row.

### CR-31. Decision Log entries drift from their common template - Severity: Nit
* **Reviewer Lens:** `egm-structural` (F8)
* **Affected Scope:** 801-823, 880-885, 763-769, 1167, 245-297.
* **Discrepancy Description:** Q15 Resource limits has no "Decision:" or
  "Consequences:" labels; Q9 opens with an italic history paragraph; Q5 has
  no Consequences; Q17 uses "Decision (author):"; CUJs put the colon outside
  the bold ("**Given**:") while the rest put it inside.
* **Rationale & Recommendation:** Apply one template and one label style.

### CR-32. Cross-references are linked inconsistently - Severity: Nit
* **Reviewer Lens:** `egm-structural` (F9)
* **Affected Scope:** 76, 101, 455; "the Technical Plan" (105, 168, 176);
  "Detailed Implementation Plan" (118, 188, 337, 445, 1091, 1213); "Future
  Improvements" (454, 458, 909); invariant numbers (383, 1292); baseline §10.5,
  §14, §19, §18 (94, 101, 152, 336).
* **Discrepancy Description:** "Baseline verification" is linked at 76 and
  455 but not at 101; the other references are never linked.
* **Rationale & Recommendation:** Link them consistently.

### CR-33. Stray Markdown inside a plain-text code block - Severity: Nit
* **Reviewer Lens:** `egm-structural` (F10)
* **Affected Scope:** 321.
* **Discrepancy Description:** Backticks around `output modified` inside a
  `text` code block render literally.
* **Rationale & Recommendation:** Remove the backticks.

### CR-34. Framing claims go further than the evidence - Severity: Nit
* **Reviewer Lens:** `egm-style` (S11)
* **Affected Scope:** 26-27, 65-66, 99.
* **Discrepancy Description:** "It does not yet state what it guarantees" is
  too broad (README lines 85, 96, 187-188 make guarantees; P7 quotes one).
  "It is often the only copy" is an unsourced frequency. The P9 title "No
  support claim has evidence behind it" ignores the recorded Windows/Python
  3.12 run (baseline §16.1).
* **Rationale & Recommendation:** "does not state a complete contract with
  exceptions and evidence"; "can be the only copy"; "Platform and version
  support claims have no recorded CI evidence."

### CR-35. "Proves" overstates what a test does - Severity: Nit
* **Reviewer Lens:** `egm-style` (S12)
* **Affected Scope:** 34-35, 861-863.
* **Rationale & Recommendation:** Use "checks" or "demonstrates".

### CR-36. An unlabeled estimate drives the Q21 decision - Severity: Nit
* **Reviewer Lens:** `egm-style` (S13). Cross-stage: related to ZO-3.
* **Affected Scope:** 1053-1054.
* **Discrepancy Description:** "Rewriting a single JSON index of about 60 MB
  every 10 seconds does not fit these budgets on network storage" is stated
  as fact. The 10 s flush matches `pipeline.py:233`, but the 60 MB size and
  the network-storage conclusion are not measured; Q21 (1096) calls the size
  "estimated".
* **Rationale & Recommendation:** Mark the sentence as an estimate.

### CR-37. The Q9 reopen-and-revert history appears five times - Severity: Nit
* **Reviewer Lens:** `egm-style` (S14)
* **Affected Scope:** 454, 880-885, 899-903, 1262, 1332-1337.
* **Rationale & Recommendation:** Keep it once in the Q9 history note and
  refer to Q9 elsewhere.

### CR-38. Process narration inside decisions and alternatives - Severity: Nit
* **Reviewer Lens:** `egm-style` (S15)
* **Affected Scope:** 613-616, 769, 1225-1230.
* **Discrepancy Description:** "It replaces the original Q7 question...",
  "Reopening this would mean reversing part of Q3.", "The author gave no
  further rationale, so none is added here." describe drafting, not the
  system.
* **Rationale & Recommendation:** Drop them.

### CR-39. Vague words and filler - Severity: Nit
* **Reviewer Lens:** `egm-style` (S18)
* **Affected Scope:** 996 "can simply be run again"; 644 "say this plainly";
  1087 "a prominent log line" (name the level); 795-796 "roughly under a
  second" (double hedge); 793-794 "This bears on baseline R15" (say what
  changes); 1063-1064 "locally in CI" (contradictory); 777-778 "what the
  pipeline believes" (write "the state the pipeline acts on").
* **Rationale & Recommendation:** Replace each with the precise wording.

### CR-40. The scope of `--accept-multi-study` is unclear - Severity: Nit
* **Reviewer Lens:** `egm-style` (S19)
* **Affected Scope:** 572-575.
* **Discrepancy Description:** "It applies to that run only" is followed by a
  statement that the acceptance persists in state.
* **Rationale & Recommendation:** "The option is read only in the run that
  names it; the acceptance it records persists."

### CR-41. The same thing is described with different terms - Severity: Nit
* **Reviewer Lens:** `egm-style` (S20). Cross-stage: CO-10 ("owner"/"author").
* **Affected Scope:** 329 vs 1069 (flush cadence); 36 vs 80, 213 ("the
  owner's decision" vs "the author").
* **Rationale & Recommendation:** Pick one cadence wording and one name for
  the decision-maker.

### CR-42. The P11 row is too long for a table cell - Severity: Nit
* **Reviewer Lens:** `egm-style` (S22)
* **Affected Scope:** 101 (about 130 words).
* **Rationale & Recommendation:** Keep the defect statement in the row; move
  the NTFS and ext4 ordering explanation to Baseline verification or Q21.

### CR-43. Remaining internal inconsistencies - Severity: Nit
* **Reviewer Lens:** `egm-design` (N1, rest). Cross-stage: ZO-12, CO-7.
* **Affected Scope:** 315 vs 386, 401; 1146-1148.
* **Discrepancy Description:** The Q23 refusal appears in the
  post-hardening run sequence (315) with no phase, while `relocate` is phase 3
  (386, 401). The dcm2niix version range is "set during implementation"
  (1146-1148), which conflicts with Goal 8.
* **Rationale & Recommendation:** Mark the phase in the run sequence; set the
  version range now or list it as a deferred item with a phase.

### CR-44. Contract evidence is not mapped clause by clause - Severity: Nit
* **Reviewer Lens:** `egm-design` (N2, rest)
* **Affected Scope:** invariant 4 and 7 rows; Goal 1 (151-155).
* **Discrepancy Description:** Invariant 4 cites T10 (concurrency) for root
  containment and for keeping added files; invariant 7 cites T18.
* **Rationale & Recommendation:** Map each clause to a named test.

### CR-45. The metadata-call budget depends on the data - Severity: Nit
* **Reviewer Lens:** `egm-design` (N3)
* **Affected Scope:** 249-250, 346, 1047.
* **Discrepancy Description:** The receipt check stats every scan-type
  folder. With up to 17 labels plus root, receipt and extraction checks, a
  broad protocol can exceed the 20-call limit.
* **Rationale & Recommendation:** State the limit as a formula and test it
  with a session that has many labels.

### CR-46. Lock details (Q13) - Severity: Nit
* **Reviewer Lens:** `egm-design` (N6)
* **Affected Scope:** 705-724; 1013.
* **Discrepancy Description:**
  * `convert` locks the DICOM root, which it only reads; that fails on roots
    that are read-only for the converting account (a T09 case).
  * Q13 says two mount paths reach the same lock file (718-721), but state
    stores absolute paths (1013), so a different path alias causes refusals or
    relocate churn.
  * `migrate` should also take the legacy project lock.
  * Exit code 2 covers lock busy, held work and issues alike; a separate
    "busy" code would let schedulers retry instead of alerting a person.
* **Rationale & Recommendation:** Address each point in Q13.

### CR-47. Scope of recoverability - Severity: Nit
* **Reviewer Lens:** `egm-design` (N7). Cross-stage: same as ZO-10 (journal
  replay).
* **Affected Scope:** 345, 972.
* **Discrepancy Description:** Invariant 5 says "any persistence step", but
  no parent-directory fsync is added for intents, journals or the index/`.bak`
  renames. `migrate` replays schema-2 journals in name order (972), which
  inherits P11.
* **Rationale & Recommendation:** State whether power loss is in scope. Skip
  journals older than the index's modification time during migration (0.1.0
  journal names are time-ordered).
