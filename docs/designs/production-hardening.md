# mriDICOMpipeline Production Hardening: Behavior Contract and Phase 1 Plan

*   **Author:** Levi Davis
*   **Status:** Revised after review; awaiting the author's approval
*   **Date:** 2026-11-04; revised 2026-11-05
*   **Review:** EGM consolidated review of 2026-11-04
    ([`consolidated_review_report.md`](../reviews/2026-11-04-production-hardening/consolidated_review_report.md)).
    Its decisions D1–D8 were applied on 2026-11-05; see
    [Open Questions](#open-questions). The pre-review text is in this file's
    git history.
*   **Baseline:** `mri-dicom-pipeline` 0.1.0, state schema 2,
    `src/mri_dicom_pipeline/pipeline.py` SHA-256 `5667d1c1…38735a6`. All 19
    files listed in [`current-source-manifest.json`](./current-source-manifest.json)
    matched the working tree on 2026-11-04. The repository has no commits yet,
    so the manifest is the only fixed reference for the baseline.
*   **Inputs:** [`concepts_brief.md`](./concepts_brief.md) and
    [`mriDICOMpipeline-current-state-spec.md`](./mriDICOMpipeline-current-state-spec.md)
    (called "the baseline spec" below).

--------------------------------------------------------------------------------

## Overview

`mri-dicom-pipeline` takes session ZIP files of MRI DICOM images, checks each
file name against a study roster CSV, moves accepted ZIPs to a canonical
location, extracts the DICOM objects, and converts each session to NIfTI with
the external `dcm2niix` program. Operators run the same two commands, `prepare`
and `convert`, each time a new batch arrives. Sessions that are already finished
are skipped without re-reading their images.

Version 0.1.0's synthetic test suite is reported to pass; it was not re-run
for this document (see [Baseline verification](#baseline-verification)).
Version 0.1.0 does not yet state what it
guarantees, and some code paths can report success, reuse old results, or
replace data in ways an operator would not expect from the documentation (see
[The Problem](#the-problem)).

This document does three things:

1.  **Behavior contract.** It states each guarantee the package will make across
    the eight review areas in the brief, the exceptions to each guarantee, and
    the test or deployment check that proves it.
2.  **Decisions and phases.** It records the owner's decision on each open
    product question and assigns every item in the baseline spec's risk
    register (R01–R20) and stress-test matrix (T01–T18) to a delivery phase.
3.  **Phase 1 plan.** It gives a file-level implementation plan for phase 1
    only. Phase 1 is the lab's production version: production data is
    processed as soon as it is done. Each later phase starts only when a named
    trigger occurs (see the [Phase map](#phase-map)) and then gets its own
    design document, which must follow this contract.

No project has been processed by 0.1.0, and no 0.1.0 output has to be kept
(author, 2026-11-05). This document therefore plans no migration.

The eight review areas are:

1.  Data loss and crash recovery.
2.  Concurrent and shared-filesystem safety.
3.  Scientific correctness of images and diffusion companions.
4.  Metadata, privacy, and untrusted-input boundaries.
5.  Cache, provenance, and migrations.
6.  Scaling and resource budgets.
7.  Operator reporting.
8.  Compatibility, packaging, and release maintenance.

## The Problem

### Who is affected and why it matters

Researchers treat a session marked `converted` in `Reports/session_status.csv`
as ready for analysis. If that status is wrong (a series is missing, the images
came from an outdated converter, or they were built from a superseded upload),
the error enters study results. It is also expensive to find later, because the
pipeline's own reruns skip that session.

Inputs carry the same risk. Once a ZIP is moved to
`DicomUploads/SubjectsSorted/`, it is often the only copy the study holds, so a
silent replacement cannot be undone.

The intended deployment is one compute node, often a Slurm allocation with a
time limit, on shared storage. In that setting, abrupt termination, two
operators running commands at once, and slow network filesystems are routine.

### What is wrong today

Each claim below was first checked by reading `pipeline.py` on 2026-11-04
("code trace"). Most were then reproduced against the
unchanged baseline; see [Baseline verification](#baseline-verification). An
item marked "hypothesis" depends on behavior outside this code, such as
`dcm2niix`, and needs a test to confirm it.

The severity scale was approved by the author at brief reconciliation:

| Severity | Meaning |
|---|---|
| Critical | Falls in one of the brief's priority categories: false success, image corruption, identity reassignment, input loss, unrecoverable state, or sensitive-data disclosure |
| High | Data or state at risk, but only with a specific trigger or platform |
| Medium | Cost, noise, or a gap in honesty |
| Low | Documentation mismatch only |

| # | Severity | Problem | Where | Evidence |
|---|---|---|---|---|
| P1 | Critical | **`converted` does not mean every series was converted.** A session counts as complete when `dcm2niix` exits 0, at least one valid volume exists, and no unexpected files remain. The code does not compare the outputs with the series listed in the preparation inventory. If `dcm2niix` skips a series and still exits 0, the session is reported complete with that series missing. | `convert_one`, `organize_conversion` | Code trace for the missing comparison; hypothesis for `dcm2niix` exit behavior. Not in the baseline spec. |
| P2 | Medium | **Old outputs survive converter or algorithm changes.** The conversion cache matches on prepared DICOM content, output path, and receipt metadata. It does not record the `dcm2niix` version or options, or the classifier or sanitizer revision. After an upgrade of any of these, every old session stays `converted`. | `plan_convert` | Code trace; same as baseline R04. |
| P3 | Critical | **A changed ZIP at its canonical path replaces the session without a hold.** A changed ZIP that arrives in `Uploads/` for an existing session is held as a conflict. A changed ZIP at the canonical `SubjectsSorted/` path takes a separate "replacement" branch that re-extracts and republishes. Overwriting a canonical ZIP with a different export (for example, a re-export missing some series) therefore replaces the prepared session. A truncated copy normally fails to open and is reported as a failure instead. | `prepare_one` (`replacement` branch) | Code trace; same as baseline R05. |
| P4 | High | **State forgets the last good conversion, and stale folders look current.** A failed or partial reconversion overwrites the session's conversion record. The older complete folder stays on disk, but state no longer points to it. After a session is re-prepared, the old folder at `Niftis/Subjects/<id>/<session>/` stays in place with a "complete" receipt, although it was built from superseded input. Only the status CSV shows `needs_conversion`. | `convert_one`; `prepare_one` (copies the prior `conversion` record) | Code trace. The overwrite is in baseline §10.5; the stale-folder case is not. |
| P5 | High | **Abrupt termination leaves undefined states.** Archive move, directory swap, and state checkpoint are separate filesystem steps. Only a caught exception during the final rename is tested. There is no SIGTERM handler; a Slurm time limit sends SIGTERM and then SIGKILL, which skips the orderly shutdown path. Leftover `.incoming_*` and `.previous_*` directories are never cleaned up or reported. | `publish_tree`, `prepare_one`, `run` | Code trace; same as baseline R01 and R11. |
| P6 | High | **The lock does not cover shared storage roots.** The lock file sits under `--project-root`. Two commands with different project roots but the same `--report-root` or `--output-root` both get a lock and can write the same index and outputs. | `main`, `ProjectLock` | Code trace; same as baseline R02. |
| P7 | Low | **`--dry-run` can hash files.** The help text says a dry run "performs no hashes". With `--audit`, planning hashes every inventoried file in cached sessions. | `config_parser`, `plan_prepare`, `plan_convert`, `fast_valid` | Code trace; same as baseline R10. |
| P8 | Critical | **Unredacted converter output reaches disk.** This is a sensitive-data disclosure channel, so it is Critical on the scale above (relabeled from Medium after review). The converter log is written raw while `dcm2niix` runs and is redacted only afterwards. A crash in between leaves the raw log in the staging directory. The redaction itself matches line patterns and can miss identifiers. | `convert_one`, `redact_diagnostic` | Code trace; same as baseline R08. |
| P9 | Medium | **No support claim has evidence behind it.** CI is configured, but the repository has no commits and no recorded CI run. No test uses the real `dcm2niix`. No license is declared. Python 3.11 is listed in the classifiers but is not tested. | `.github/workflows/ci.yml`, `pyproject.toml` | Direct inspection; same as baseline R19 and R20. |
| P10 | Critical | **A modified output folder is reconverted and replaced automatically.** The conversion cache check compares stored directory mtimes. Adding, removing, or renaming any file under `Niftis/Subjects/<id>/<session>/` fails the check; unzipping a `.nii.gz` in place is one example. The next `convert` run then reconverts the session. If the reconversion succeeds, `publish_tree` replaces the folder and deletes any files people added along with the old tree. | `plan_convert`, `fast_valid`, `publish_tree` | Code trace. The baseline tests detection of deleted outputs (`test_deleted_dicom_and_nifti_detected`), not the loss of added files. |
| P11 | High | **A crash while journals are being deleted can roll sessions back.** `State.flush` writes the index, then deletes the pending journals one by one in whatever order `Path.glob` returns, which is not sorted. If the process is killed partway, the next start replays the journals that remain on top of an index that already includes newer events. A session whose latest journal was deleted but whose earlier journal survived reverts to the earlier state, for example losing its conversion record; deleted issues can also come back. On NTFS the listing is alphabetical, so the oldest journals are deleted first and the replay is harmless. On filesystems whose listing order is not sorted (ext4, network filesystems), the rollback can happen. | `State.flush`, `State.__init__` | Reproduced with a forced newest-first order; not reproduced with the natural NTFS order (see Baseline verification). The baseline (§14) expects replay to "tolerate repetition"; that holds only when the surviving journals are the newest ones. |

The baseline spec's register also lists concerns that are not repeated above:
archive resource limits, diffusion checks, classification rules, path and case
collisions, scale, retention, and trust in ownership receipts. The Technical
Plan assigns every register item to a phase.

### Baseline verification

Run on 2026-11-04 against the unchanged baseline. All 19 manifest files
matched their SHA-256 values.

*   **Environment:** Windows 11, Python 3.12.1, `pydicom` 3.0.2.
*   **Data:** synthetic DICOM objects and a simulated converter, created in
    temporary folders outside the repository.
*   **Scripts:** throwaway scripts in the session scratchpad, not part of the
    repository. The phase 1 regression tests (see the traceability table in
    the Detailed Implementation Plan) replace them as the lasting evidence.
*   **Project files:** none were modified.

| Item | Result | Observed |
|---|---|---|
| P1 | Reproduced | Two prepared series; the converter exited 0 with one volume; status `converted` |
| P2 | Reproduced | A different converter path was given; the session stayed cached and conversion was not called |
| P3 | Reproduced | The canonical ZIP was overwritten with different content; `prepare` exited 0, the content digest changed, and there was no blocking issue |
| P4 (stale folder) | Reproduced | After P3, the old NIfTI folder was still present with a `complete` receipt; the status CSV showed `needs_conversion` |
| P4 (record overwritten) | Reproduced | A forced reconversion failed (exit 2); state then pointed to `.pending/`, and the old complete folder was still on disk |
| P5 | Reproduced | A hard kill during the folder swap left `.incoming_*` and `.previous_*` folders, which remained after the next successful `prepare` |
| P6 | Reproduced | While project A held its lock, project B with the same `--report-root` ran and exited 0 |
| P7 | Reproduced | `--dry-run --audit` called `sha256` and left the project files unchanged |
| P8 | Reproduced | The unredacted sentinel line was readable in the staging `converter.log` while the converter ran, and absent from the final report log |
| P10 | Reproduced | A file added to a complete output folder was deleted by the next `convert`, which exited 0 |
| P11 | Reproduced with forced order | Natural NTFS order: no rollback. Newest-first deletion followed by a kill: the index held `v2`, and after reload the session was `v1` |
| P1 (`dcm2niix` exit behavior) | Reproduced (2026-11-05, step 1) | `dcm2niix` v1.0.20261029 on synthetic data skipped a 16-bit JPEG Baseline series and still exited 0. The pipeline (0.1.0 logic after step 0) then reported the session `converted` with 1 of its 2 series. A series with undecodable RLE pixel data gave exit code 8 ("converted some but not all"). Evidence: `test_real_converter.py` |
| Q19 (non-image objects) | Hypothesis holds (2026-11-05, step 1) | `dcm2niix` v1.0.20261029 ignored a synthetic Siemens CSA Non-Image object, converted the image series, and exited 0 |
| P9 | Inspection only | No commits, no CI run, no license file (not reproducible as a test) |
| Classifier (Q11) | Reproduced | The mislabels in the Q11 table came from running `classify()` |
| Existing suite (30 cases) | **Not run** | The global `pytest` cannot be imported (missing `pygments`), and the sandbox blocked package downloads. Re-run after `python -m pip install pygments` or in a fresh environment |

### What already works and must be kept

Incremental reruns skip finished work without reading image contents. ZIP member
names never become output paths, so they cannot escape the staging directory.
Output folders without a pipeline receipt are never replaced. A nonzero
converter exit is never reported as success. Hardening must keep these
properties rather than trade them away for new guarantees.

## Goals

A goal counts as met only when the listed evidence exists.

1.  **One written contract for all eight areas.** For each of the ten invariants
    in baseline spec §19, the contract states the guarantee, its exceptions, and
    the acceptance test or deployment check that establishes it.
    *Evidence:* every guarantee in the contract table cites at least one test
    ID or deployment check.
2.  **Every open product decision is resolved or deferred on purpose.** Each
    item in [Open Questions](#open-questions) is answered with the chosen option
    and its rationale, or deferred to a named phase with the reason it cannot be
    decided now.
    *Evidence:* Open Questions holds only items that carry a phase and a reason.
3.  **The whole register is traced.** Every R01–R20 item and every T01–T18
    group is assigned to a phase or marked out of scope with a reason.
    *Evidence:* a mapping table with no unassigned rows.
4.  **Phase 1 makes the pipeline safe for production data.** As the brief
    requires, phase 1 addresses problems that can cause false success, image
    corruption, identity reassignment, input loss, unrecoverable state, or
    sensitive-data disclosure. It also carries every change to the output
    layout or the state format (classifier revision 2, state schema 3), so
    production data is converted under one layout from its first run. Other
    performance, packaging, and reporting work waits for a later phase. The
    exceptions are the CI matrix and the license files, which the P9 evidence
    needs, and the report and history changes the new statuses need. Which
    P-items and register items fall in phase 1 is decided in the Technical Plan
    (Q1).
    *Evidence:* each phase 1 item has a regression test that fails on the
    baseline and passes after phase 1.
5.  **Incremental cost does not regress.** An ordinary rerun over unchanged,
    completed sessions performs no archive hashing, no DICOM header reads, and
    no converter calls, as today.
    *Evidence:* `test_1000_cached_sessions_do_not_read_scan_contents` still
    passes after every phase. Numeric scale budgets wait for phase 3 (Q18).
6.  **No migration is needed.** No project has been processed by 0.1.0
    (author, 2026-11-05). Phase 1 refuses an older state index, without
    writing anything, instead of upgrading it. A later phase that adds state
    fields adds optional ones, and readers treat a missing optional field as
    its default, so no migration is needed then either.
    *Evidence:* `test_old_state_rejected_without_writes`, run for state
    versions 1 and 2.
7.  **Documentation matches tested behavior.** The README and CLI help make no
    claim that lacks a test or a documented deployment check.
    *Evidence:* a checklist that maps each README guarantee to a test ID.
8.  **The phase 1 plan needs no further design decisions.**
    *Evidence:* the Detailed Implementation Plan names every file it touches and
    leaves no decision that would change behavior, data layout, or interfaces.

## Non-Goals

*   Producing BIDS datasets, defacing images, removing burned-in identifiers,
    or certifying de-identification. The contract will say which outputs keep
    identifiers; it will not claim anonymization.
*   Clinical or diagnostic use.
*   PACS or DICOMweb integration, an upload service, network transfer, a GUI,
    or a database server.
*   Spreading work across several nodes. Refusing an unsafe concurrent run
    started from a second node on shared storage is in scope (area 2).
*   Recognizing duplicates across different anonymizations, matching subjects
    from DICOM patient fields, or numbering same-day sessions automatically.
    Identity stays based on the file name.
*   Choosing the best acquisition, registration, motion or distortion
    correction, or segmentation.
*   File-level implementation plans for phases after phase 1.
*   Upgrading 0.1.0 projects. None exist (author, 2026-11-05).
*   Containing hostile archives, such as decompression bombs or parser
    exploits. Archives are assumed to come from the lab's own scanner exports
    (Q14). Phase 2 takes this up only if archives start coming from outside
    the lab.
*   Using real participant data during design or review. Fixtures are synthetic
    or explicitly approved de-identified data. The private `sessions.csv`,
    `DicomUploads/`, and `DicomExtracted/` in the repository root are not read.

## Open Questions

No product question is open. The author answered all 23 product questions
(Q1–Q23) on 2026-11-04; see the [Decision Log](#decision-log). References
below point to the baseline spec (R = review register, T = stress-test group,
G = release gate) or to the problems above.

**Revised after review on 2026-11-05.** The author asked for the simplest
design that is safe for production data once phase 1 is done. The author also
handed the phase placement of each item to the reviewer. Decision D2 is the
author's own; the reviewer made the others under that direction, and the
author can override any of them:

*   **D1:** phase 1 runs on production data. Of the Q15 limits, only a
    free-space check before extraction moves into phase 1.
*   **D2:** crash recovery never rolls a copied folder forward. It rolls back,
    redoes the work, or adopts a folder whose files all check out (Q12).
*   **D3:** upload marker files are dropped. The existing size, mtime, and ZIP
    integrity checks cover partial uploads (Q4).
*   **D4:** optional fields that a later phase adds need no migration. Phase
    1 drops geometry-tag capture and the fields that nothing in phase 1 reads
    (Q1, Q9).
*   **D5:** no 0.1.0 project exists, so `migrate` is dropped (Q22).
*   **D6:** nothing guards against empty state with full roots. The adoption
    rule rebuilds state from the receipts at the cost of one hash per ZIP
    (Q21).
*   **D7:** there is no override for series that `dcm2niix` cannot convert
    until the real-converter tests have run (Q6).
*   **D8:** phases 2–4 keep only their guarantees, triggers, and open
    questions. Their mechanisms leave this document (Q2, Q14, Q15, Q18, Q23).

**Approved with the Technical Plan on 2026-11-04.** These items were proposed
while recording the author's decisions, and the author approved them with the
section:

*   Q17 versioning, deprecation, security-report, and support proposals
    (they apply from the first public release, phase 4).
*   Q2: Python 3.11 and later.
*   Q3: an open canonical-ZIP conflict blocks conversion of that session.
*   Q7: a repeatable `--session SUBJECT/SESSION` filter; explicit
    reconversion refuses folders that hold files not in their inventory.
*   Q11: T2* sequences map to the existing `GRE` label.
*   Q12: a missing canonical ZIP raises a nonblocking issue; SIGTERM ends
    with exit code 143.
*   Q20: the `--accept-multi-study SUBJECT/SESSION` option.
*   P7: `--dry-run --audit` keeps hashing, and the help text and README are
    corrected to say so. This keeps a dry integrity check possible.
*   `prepare --audit` also re-hashes canonical ZIPs, so that a same-size,
    same-mtime edit is caught (supports Q3).
*   Every invocation that takes its locks writes a `run_history.csv` row,
    including early aborts (outcome `aborted`) (R16).

## Critical User Journeys (CUJs)

### CUJ 1: New batch on a large, finished project

*   **Given**: 1,000 converted sessions, and three new ZIPs in `Uploads/`
    whose copies have finished.
*   **When**: The operator runs `prepare`, then `convert`.
*   **Then**: Only the three new sessions are hashed, extracted, and
    converted. The other 1,000 cause no content reads. Both commands exit 0.

### CUJ 2: Slurm time limit during conversion

*   **Given**: `convert` running with four workers inside a Slurm job.
*   **When**: Slurm sends SIGTERM, then SIGKILL 30 s later.
*   **Then**: The run sets its stop flag and stops every converter. On Linux
    it stops each converter's whole process group. Sessions still running are
    recorded as `interrupted` and keep their stages; nothing more is
    organized or published. Completed events are already checkpointed. The
    final flush and the report rewrite are attempted, and the exit code is
    143. The next run reconciles open intents: it adopts a folder that was
    fully published but not yet recorded, rolls back a half-finished swap,
    and redoes interrupted sessions. No session reverts to an older state.

### CUJ 3: Corrected re-export for an existing session

*   **Given**: `S001/20260725` is converted.
*   **When**: An operator copies a corrected export over
    `SubjectsSorted/S001/S001_20260725.zip`, or drops it into `Uploads/`.
*   **Then**: `prepare` holds the session as a conflict. The extraction and
    the NIfTI folder stay unchanged. The operator uploads the export as
    `S001_20260725_2.zip`, and it becomes a separate session.

### CUJ 4: A researcher modifies an output folder

*   **Given**: A complete folder `Niftis/Subjects/S001/20260725/`.
*   **When**: A researcher gunzips one `.nii.gz` in place, and `convert` runs
    later.
*   **Then**: The session gets an `output modified` issue. Nothing is
    deleted or reconverted. A `convert --session S001/20260725
    --force-reconvert` refuses while the extra `.nii` file is present and
    names it. Once the file is moved away, the reconversion proceeds.

### CUJ 5: A converter run skips a series

*   **Given**: A prepared session with 12 required (MR image) series.
*   **When**: `dcm2niix` exits 0 but produces volumes for only 11.
*   **Then**: The session is `partial`, its output goes to `.pending/`, and
    the issue names the missing series key. Any earlier complete folder
    stays in place and stays recorded as last-good.

### CUJ 6: First production run

*   **Given**: A new project with no state. Session ZIPs sit in
    `DicomUploads/Subjects/<id>/` (the legacy location) or in `Uploads/`.
*   **When**: The operator runs `prepare --dry-run` (adding
    `--import-legacy` for the legacy location), reviews the plan, then runs
    `prepare` and `convert`.
*   **Then**: The dry run lists the planned work and writes nothing.
    `prepare` moves each accepted ZIP to
    `DicomUploads/SubjectsSorted/<id>/` and extracts it. It never modifies or
    deletes a ZIP. A ZIP it cannot accept stays where it was, with an issue.
    `convert` converts every prepared session.

## Technical Plan (Architecture)

The decisions below (Q1–Q23) define the architecture. This section turns
them into a run sequence, a behavior contract, a state schema, interface
changes, and a phase map.

### Run sequence after hardening

```text
command start
  │
  ├─ parse options; resolve roots (pairwise disjoint, as today)
  ├─ load roster ── reject IDs that differ only by case, and reserved names (Q2)
  ├─ install the stop handler for SIGTERM and Ctrl+C (Q12)
  ├─ take locks on every root the command touches (Q13)                       [non-dry]
  ├─ load index.json (decision table in Q21) ── replay journals with seq > loaded seq
  ├─ refuse if the schema is not 3 (Q22) or the project appears moved (Q23)
  ├─ reconcile open intents on the locked roots (Q12)                         [non-dry]
  ├─ plan
  │    prepare: discover ZIPs ─ signature / cache ─ canonical changed? → hash;
  │             same digest → accept; different → hold (Q3)
  │    convert: ready sessions ─ complete folder? never auto (Q7) ─ receipt
  │             invalid → `output modified` issue
  ├─ subject workers (one thread per subject, as today)
  │    prepare: stage ─ free-space check (Q15) ─ hash / extract (dedupe,
  │    multi-study hold, Q20) ─ move ZIP ─ write intent ─ publish ─ result event
  │    convert: converter in its own process group; output piped through
  │    redaction before it reaches disk (P8); validate + gzip, no byte
  │    changes (Q10); series coverage check (Q6/Q19) ─ write intent ─ publish
  └─ coordinator (single state writer)
       journal(seq) ─ apply ─ clear intent ─ flush every 10 s, as today (Q21)
       ─ reports ─ run_history row on every exit path ─ exit 0 / 2 / 130 / 143
```

### Behavior contract

Each guarantee holds on supported environments (Q2) from the phase listed.
Test IDs refer to the T-groups in baseline spec §18; the phase 1 test
functions are named in the Detailed Implementation Plan.

| # | Invariant | Guarantee | Stated exceptions | Evidence | Phase |
|---|---|---|---|---|---|
| 1 | Identity | Subject and session come only from authorized filenames. IDs that differ only by case, or that equal a Windows reserved name, are rejected. A ZIP with more than one study or conflicting repeated instances is held unless accepted. | Operators assign `_N` suffixes. `--allow-unlisted-dates` admits known IDs. `--accept-multi-study` overrides the study hold. | T01, T02, T05 | 1 |
| 2 | Input preservation | ZIP bytes are never modified, and ZIPs are never deleted. An accepted canonical ZIP is never replaced, and changed bytes are never extracted. An upload moves only after a hash check, with its size and mtime unchanged through hashing and extraction. | The pipeline cannot stop others from overwriting files. Detection relies on size and mtime unless `--audit` is used. Skipped non-DICOM members exist only in the ZIP. Partial-upload detection depends on the copy tool writing the file in order (Q4). | T03, T05, T06 | 1 |
| 3 | No false completion | `converted` means the converter exited 0, every required series has at least one valid volume, there are no organization errors, and there are no unexpected files. | Series outside the MR image SOP classes are excused and listed. Whether the conversion itself is correct is up to `dcm2niix` (Q9). A series `dcm2niix` cannot convert leaves its session `partial`, with no override yet (Q6). | T08, T11, T18 | 1 |
| 4 | Isolation | Deletion and publication stay inside the configured roots. Two commands that share a root cannot run at the same time on supported storage. Files people add are never deleted. | Storage whose locks only work within one node is unsupported. Processes that do not cooperate with the lock are out of scope. | T10 + site check | 1 |
| 5 | Recoverability | After an abrupt stop at any persistence step, the next run either returns to the last valid state, redoes the interrupted work, or adopts a published result whose inventory files are all present at their recorded sizes. No session rolls back. An unreadable index is recovered from `.bak` plus journals. | If neither index file can be read, the run stops and names them. A stage on a lost node is replanned. Power loss (unflushed disk caches) is out of scope. | T07, T09 | 1 |
| 6 | Incrementality | A cached rerun makes no content reads. A complete session is never reconverted automatically. | `--audit` and `--force-*`. | T06 | 1 |
| 7 | Provenance | Each conversion records package version, run ID, converter path, argument list, and converter version or versions. The package version identifies the classifier, sanitizer, and SOP-class list in use. Each preparation records archive hash and run ID. | No comparison and no "outdated" flag (Q8). The converter binary is not hashed. | T18 | 1 |
| 8 | Privacy honesty | The README lists every channel that carries identifiers or dates: paths, sidecar fields, NIfTI header text (time of day in `descrip`) and NIfTI header extensions, original DICOM bytes in `DicomExtracted/` and the canonical ZIPs, hashes, and heuristically redacted logs. Unredacted converter output is never written to disk. | No de-identification claim. Redaction matches line patterns. | T14 | 1 (full README contract in 4) |
| 9 | Pipeline integrity (narrowed, Q9) | Each published NIfTI decompresses to exactly the converter's bytes. Companions are byte-identical. Required series are accounted for. | Orientation, slice order, spacing, dimensions, scaling, and voxel values are `dcm2niix`'s responsibility. Diffusion companions get the finite-number check only. | T11, T12 | 1 |
| 10 | Operational clarity | Distinct statuses for awaiting upload, held, ready, converted, converted with a failed last attempt, partial (with reason), failed, interrupted, and output modified. Every locked invocation writes a history row. | Local time without offset is ambiguous for one hour per year (Q16). | T18 | 1 |

Containment of hostile archives and automatic retention cleanup are not
guaranteed. They are phase 2 items that start only on their triggers (Phase
map).

### State schema 3

Phase 1 writes schema 3 and carries only the fields phase 1 reads. A later
phase may add optional fields. Readers treat a missing optional field as its
default, so adding one needs no migration (Q1).

| Element | Change |
|---|---|
| Index top level | `version: 3`; `last_seq` (the highest journal sequence number included) |
| Journal file | Name `<seq:016x>_<random>.json`; body carries `seq` |
| `index.json.bak` | Previous index, kept at every flush |
| Session entry | Adds `accepted_multi_study` (bool) and `last_attempt` (`status`, `output`, `run_id`, `reason`). `conversion` becomes the last-good record and gains `provenance` and `coverage` (`required`, `converted`, `excused` series keys) |
| Receipt descriptors | `version: 3`; adds `receipt_id`, copied from the receipt, so reconciliation can tell which receipt state records. Paths and directory-mtime keys stay absolute |
| Source records | `version: 3`; otherwise unchanged |
| Preparation inventory | Series rows add `sop_classes` and `required`. The top level adds `repeated_identical`. `content_sha256` counts byte-identical repeats once |
| Receipt files | Preparation: adds `run_id`. Conversion: adds `run_id`, `provenance`, and `coverage` |
| `Reports/.state/intents/` | New. One file per in-flight folder publication |

### Interface changes

| Change | Phase |
|---|---|
| New repeatable option `--session SUBJECT/SESSION` (both commands) | 1 |
| New repeatable option `--accept-multi-study SUBJECT/SESSION` (`prepare`) | 1 |
| `--force-reconvert` refuses folders with files not in their inventory | 1 |
| `convert --audit --session KEY` revalidates a restored complete folder (Q7) | 1 |
| `--dry-run` help text: "Plan only: no project writes or converter calls; hashes files only when combined with --audit" | 1 |
| `prepare --audit` also re-hashes canonical ZIPs | 1 |
| Exit code 143 after SIGTERM | 1 |
| `session_status.csv` adds `required_series`, `converted_series`, `excused_series`, `last_attempt_status`, `last_attempt_folder`; new status values listed in invariant 10 | 1 |
| `run_history.csv` gains outcomes `terminated` and `aborted` and a `reconciled` count | 1 |

### Phase map

**The rule (decided 2026-11-05):** phase 1 holds the safety fixes plus every
change to the output layout or the state format, so production data is
converted under one layout from its first run. Every other item waits for a
named trigger. A later phase starts, with its own short design document, only
when its trigger occurs. If the trigger never occurs, the phase is never
built.

**Phase 1, the lab's production version** (installed from the repository;
not a public release):

*   Q3, Q6, Q7, Q8 (record only), Q9 (integrity checks), Q10, Q11, Q12
    (including SIGTERM and Linux process groups), Q13, Q19, Q20, and the Q2
    ID rules.
*   Q4 (the existing size, mtime, and ZIP integrity checks; no markers).
*   Q14 and Q15, reduced to the free-space check before extraction.
*   Q21, reduced to sequence numbers, `.bak`, and the load decision table.
*   Q22, reduced to refusing other state schemas. Q23, reduced to refusing a
    moved project.
*   P7 and P8.
*   Report files held open on Windows do not stop a run (Q2).
*   Real-converter smoke tests right after the move-only step.
*   A CI matrix on Python 3.11 and later, and `LICENSE`/`NOTICE`.
*   A README and runbook that describe phase 1 behavior.

**Phase 2, operations.** Each item starts only on its trigger:

| Item | Trigger | Until then |
|---|---|---|
| Windows whole-tree converter stop | Leftover converter processes are seen on Windows | Stopping a run on Windows stops the converter, not processes it started |
| Cleanup of retained `.pending/` attempts and failed stages (Q15) | They use too much disk | Manual cleanup, as the runbook describes |
| Windows path-length holds | A path longer than Windows allows causes a failure | That session fails with a reported error and publishes nothing |
| Archive caps and per-archive process isolation (Q14) | Archives start coming from outside the lab | Archives are parsed in-process, as today, after the free-space check |

**Phase 3, scale.** Starts only when runs are measured as too slow: Q18 (the
20,000-session targets, subject index, report cadence, index size, flush
interval, and T15 benchmarks). Q23 `relocate` starts when a project has to
move.

**Phase 4, public release.** Starts only when the package is published: the
README contract rewrite (Goal 7), `SECURITY.md`, the support table, the
distribution checks, the Q17 versioning policy, and the first release.

| Register item | Phase | | Test group | Phase |
|---|---|---|---|---|
| R01 Crash consistency | 1 | | T01 Identity grammar | 1 |
| R02 Locks on shared roots | 1 (+ site check if runs start from more than one machine) | | T02 CSV robustness | 1 (very large rosters in 3) |
| R03 NIfTI correctness, sanitizer | 1 (narrowed to integrity by Q9/Q10) | | T03 Archive eligibility | 1 (encrypted members in 2) |
| R04 Cache provenance | 1 (record only, Q8) | | T04 Archive integrity, resources | 1 (free-space check); 2 if archives come from outside the lab |
| R05 Canonical replacement | 1 | | T05 Duplicate/conflict matrix | 1 |
| R06 Partial transfer | 1 (existing size, mtime, and ZIP integrity checks, Q4) | | T06 Fast cache and audit | 1 |
| R07 Archive resource abuse | 1 (free-space check); 2 if archives come from outside the lab | | T07 Crash injection | 1 |
| R08 Privacy channels | 1 (README contract in 4) | | T08 Process control | 1 (Windows whole-tree stop in 2, on its trigger) |
| R09 Corruption, migration, relocation | 1 (`.bak`; no migration needed, Q22); 3 (`relocate`, on its trigger) | | T09 Filesystem faults | 1 (including the free-space check) |
| R10 Audit and dry-run | 1 | | T10 Concurrent operations | 1 (+ site check if runs start from more than one machine) |
| R11 Termination, descendants | 1 (Linux); 2 (Windows, on its trigger) | | T11 Scientific geometry | 1 (narrowed to integrity) |
| R12 Non-image, multi-study, duplicates | 1 | | T12 Diffusion | 1 (narrowed to integrity) |
| R13 Diffusion and labels | 1 | | T13 Classification | 1 |
| R14 Paths, case, reserved names | 1 (case, reserved); 2 (path length, on its trigger). Symlink races inside project storage: out of scope, because storage is trusted (Q14) | | T14 Privacy | 1 |
| R15 Scale | 3, when runs are measured as too slow | | T15 Scale | 3, on its trigger |
| R16 Reporting completeness, retention | 1 (history row); 2 (retention, on its trigger) | | T16 Packaging/release | 1 (CI, license); 4 (release) |
| R17 Receipt trust | Accepted and documented (Q14) | | T17 Migration | Out of scope: no 0.1.0 project exists (Q22); 3 (`relocate`, on its trigger) |
| R18 Missing canonical archives | 1 | | T18 Observability | 1 |
| R19 Claimed versus observed support | 1 (CI, smoke tests); 4 (support table) | | | |
| R20 Release, license, security | 1 (license); 4 (security, release) | | | |

### Release gates

These restate the baseline spec's gates G01–G08 with objective pass
conditions; the author approved them at brief reconciliation. Before phase 1
processes production data, every condition marked phase 1 must pass. The
first public release (phase 4) requires every gate to pass. A gate's evidence
is produced in the phase listed and must stay green afterwards.

| Gate | Pass condition | Evidence phase |
|---|---|---|
| G01 Product contract | Every contract guarantee cites a test or a check (phase 1). The README support table matches the CI matrix plus the site checks that have passed (phase 4). | 1, 4 |
| G02 Safety and recovery | The T07 crash matrix (`test_recovery.py`) is green. The runbook covers every reconciliation outcome. If runs will start from more than one machine on shared storage, the two-node lock check has passed for that storage. | 1 (+ site, if needed) |
| G03 Scientific integrity (narrowed, Q9) | The integrity tests (byte-identical output, series coverage, companions) and the real-converter smoke tests are green on the oldest and newest supported `dcm2niix`. The README states that conversion correctness is `dcm2niix`'s responsibility. | 1 |
| G04 Cache and provenance | The provenance tests are green. The absence of an "outdated" flag is documented (Q8). | 1 |
| G05 Privacy and security | The T14 channel tests are green (phase 1). `SECURITY.md` is published (phase 4). | 1, 4 |
| G06 Performance | `test_1000_cached_sessions_do_not_read_scan_contents` is green (phase 1). Scale budgets are measured only if phase 3 is triggered. | 1 (3 if triggered) |
| G07 Compatibility and release | Every CI job is green and `LICENSE` and `NOTICE` exist (phase 1). The sdist and wheel pass `twine check` and `tests/check_distribution.py`, both contain `LICENSE` and `NOTICE`, and the versioning and deprecation policy is published (phase 4). | 1, 4 |
| G08 Operations and documentation | The runbook covers the procedures listed in the Detailed Implementation Plan. The README checklist (Goal 7) is complete. | 1, 4 |

### Departures from the brief

The draft was compared with [`concepts_brief.md`](./concepts_brief.md) on
2026-11-04, and the author decided each difference.

| # | Brief | Resolution |
|---|---|---|
| 1 | Priority 6, "Scientific correctness of converted images" | **Departure, decided after reopening Q9:** the author briefly chose a runtime geometry cross-check, then reverted to pipeline integrity only because of implementation time. Conversion correctness is `dcm2niix`'s responsibility. Geometry tags are not captured (D4, 2026-11-05); a later check would read headers again (Future Improvements). |
| 2 | "First verify the descriptive baseline against the source"; a discrepancy report with reproducible tests | Resolved: the [Baseline verification](#baseline-verification) section reproduces P1–P8 and P10, and P11 under forced listing order. **Remaining departure:** the existing 30-case suite was not re-run, because `pytest` is broken in this environment, and the reproduction scripts are throwaway. The phase 1 regression tests become the lasting evidence. |
| 3 | "Impact/severity" for every finding | Resolved: severity column and scale added to the problem table. |
| 4 | "Objective release gates" | Resolved: [Release gates](#release-gates) added. |
| 5 | Converter, tool, and configuration changes missing from cache identity (a limitation to challenge) | **Departure, reconfirmed:** Q8 records provenance but reports nothing as outdated, so P2 stays open in practice. An "outdated" report is listed under Future Improvements. |
| A1 | Not raised: code structure | **Addition, approved:** `pipeline.py` is split into modules in phase 1, with a move-only step 0. |
| A2 | CI on "Python 3.10/3.12" | **Addition, approved:** Python 3.11 or later (3.10 is end of life) and `setuptools>=77`. |
| A3 | Not raised: new CLI surface | **Additions, approved:** `--session`, `--accept-multi-study`, exit code 143, and new report columns. `migrate` and required upload markers were dropped after review (D5, D3). |
| 6 | Area 4, "untrusted-input boundaries" | **Departure, decided after review (D1):** archives are assumed to come from the lab's own scanner exports and are parsed in-process, after a free-space check. Containment waits for phase 2 and starts only if archives come from outside the lab. |

### Decision Log

#### Q3 Canonical inputs: immutable (decided 2026-11-04)

**Decision:** An accepted canonical ZIP in `DicomUploads/SubjectsSorted/` is
immutable. When `prepare` detects a change (size or mtime differs from the
recorded signature), it holds the session as a conflict and leaves the
prepared extraction and NIfTI outputs unchanged.

**Consequences:**

*   The `replacement` branch in `prepare_one` is removed. This fixes P3.
*   A changed signature triggers a hash of the ZIP. If the hash equals the
    recorded `archive_sha256` (for example, a file restored from backup with a
    new mtime), the ZIP is accepted without re-extraction and its new signature
    is recorded. Any other hash is held.
*   While the conflict is open, conversion of that session stays blocked, the
    same as for today's conflicts from `Uploads/`.
*   `--force-reextract` re-extracts only when the canonical ZIP hashes to the
    recorded value. It never accepts changed bytes.
*   A corrected export cannot reuse the session label; it arrives as a new
    `_N` session. The extracted tree keeps byte copies of every accepted DICOM
    object, so the original session stays convertible even if its canonical
    ZIP was overwritten. Skipped non-DICOM members are not kept anywhere.
*   Not settled by this decision: what an operator does when the original
    bytes are gone and the changed ZIP has to leave `SubjectsSorted/`. That
    leaves a prepared session with no canonical archive (R18). It is handled
    under Q12 (recovery and repair).

#### Q6 Meaning of `converted`: strict, every series (decided 2026-11-04)

**Decision:** A session is `converted` only if every series in its preparation
inventory yields at least one valid NIfTI volume. The existing conditions also
stay: `dcm2niix` exits 0, no organization errors, and no unexpected files. A
session that misses any series is `partial`.

**Consequences:**

*   This fixes P1 whether or not `dcm2niix` exits 0 when it skips a series.
*   The check compares inventory `series_key` values with each output's
    `source_series_key`. Both are the first 20 hex characters of SHA-256 of
    the SeriesInstanceUID, so no image is read again. The fallback in
    `organize_conversion`, which uses a stem token when the output name does
    not start with `series_`, cannot match an inventory key. Such an output
    therefore counts toward volumes but not toward series coverage.
*   A series that `dcm2niix` splits into several outputs (echoes, magnitude
    and phase) counts once it yields at least one valid volume.
*   Today preparation accepts any object with a SOP class and a series UID,
    except DICOMDIR. Under this rule, every non-image series that reaches the
    inventory would leave its session `partial` permanently. Which objects
    reach the inventory is therefore decided separately in Q19.
*   Known failure mode, accepted until the real-converter smoke tests have
    run (D7): a required series that `dcm2niix` cannot convert leaves its
    session `partial`, and there is no operator override. Whether one is
    needed is decided from the smoke-test results.

#### Q19 DICOM eligibility: extract all, require MR images (decided 2026-11-04)

**Decision:** Preparation keeps extracting every eligible object, as today.
The inventory records the SOP class of each series. The Q6 strict rule applies
only to *required* series: those containing at least one object of an MR image
SOP class. Every other series stays in `DicomExtracted/`, and reports list it
as "not converted by design".

**MR image SOP classes (required):**

| SOP Class UID | Name |
|---|---|
| `1.2.840.10008.5.1.4.1.1.4` | MR Image Storage |
| `1.2.840.10008.5.1.4.1.1.4.1` | Enhanced MR Image Storage |
| `1.2.840.10008.5.1.4.1.1.4.3` | Enhanced MR Color Image Storage |
| `1.2.840.10008.5.1.4.1.1.4.4` | Legacy Converted Enhanced MR Image Storage |

MR Spectroscopy Storage (`1.2.840.10008.5.1.4.1.1.4.2`) is not on the list.

**Consequences:**

*   Each inventory series row gains its set of SOP class UIDs and a `required`
    flag. A series that mixes classes is required if any of its objects is on
    the list.
*   Known failure mode: an image series stored under a class not on the list
    (a vendor private image class or secondary-capture maps) is excused from
    the strict rule. It can be missing from the outputs without the session
    becoming `partial`. Listing excused series per session keeps this visible.
    The list is a constant in the code. Changing it changes no cache key
    (Q8); the package version in the provenance record identifies the list in
    use.
*   Non-image objects stay in the `dcm2niix` input folder, as today. The
    real-converter smoke tests (T03, T11) must confirm that their presence
    does not make `dcm2niix` exit nonzero. This is a hypothesis to test. The
    tests run right after the move-only step. *Result (2026-11-05):* the
    hypothesis held for `dcm2niix` v1.0.20261029 with a synthetic CSA
    Non-Image object (exit code 0). If `dcm2niix` exits nonzero,
    this decision is reopened before preparation changes are built (for
    example, running `dcm2niix` per series, or moving non-image objects to a
    sibling folder).

#### Q20 Repeated instances and several studies: hold for review (decided 2026-11-04)

**Decision:** Preparation holds a ZIP as a conflict, and publishes nothing,
when either of these is true:

*   two objects share a SOP Instance UID but have different bytes;
*   the objects carry more than one StudyInstanceUID.

Byte-identical repeats are extracted once and counted. The operator resolves a
hold in one of two ways: split the ZIP into separate `_N` sessions, or accept
that one session explicitly.

**Consequences:**

*   `extract_archive` tracks SOP Instance UID → (SHA-256, size) for each
    accepted object. An object without a SOP Instance UID fails the archive,
    the same way a missing SeriesInstanceUID does today.
*   The explicit accept is a repeatable `prepare` option naming one session key,
    `--accept-multi-study SUBJECT/SESSION`. It applies to that run only. The
    acceptance is recorded in the session's state entry, so later runs do not
    hold the session again. A conflict between repeated instances with
    different bytes cannot be accepted; the ZIP must be split or replaced by
    a new `_N` upload.
*   Extraction hashes each object while writing it. A byte-identical repeat is
    therefore written, recognized, and deleted again, and it is left out of
    the inventory. File names come from the inventory length, so numbering
    stays continuous.

#### Q7 Reconversion of complete sessions: only on explicit command (decided 2026-11-04)

**Decision:** A session with a complete NIfTI folder is never reconverted
automatically. When its folder fails the cache check, the session is flagged
in `issues.csv` and left as it is. Converter or algorithm changes are
recorded, not reported (Q8). A complete session is reconverted only when the
operator runs `--force-reconvert`.

A session is *complete* when its state `conversion` record has status
`converted` and its folder lies under the configured NIfTI root.

**Consequences:**

*   This fixes P10. `plan_convert` no longer selects a `converted` session
    whose receipt check fails. It records an `output modified` issue instead.
    The issue counts as unresolved, so the run exits with code 2 until the
    operator revalidates the folder or reconverts it.
*   Revalidation: restoring a folder from a backup changes its directory
    mtimes, so it fails the metadata check even when every file is intact.
    `convert --audit --session KEY` revalidates such a folder. If every
    inventory file hashes to its recorded value and the folder holds no
    files outside its inventory, it records the new mtimes and clears the
    issue. No new option is needed.
*   The same rule covers a complete folder that has disappeared or that
    `--audit` finds changed. Known failure mode: a damaged folder stays damaged
    until someone acts on `issues.csv`.
*   A conversion record whose folder lies under a different NIfTI root is
    not complete at the configured root. `convert --output-root OTHER`
    therefore converts the session into `OTHER`, as today
    (`test_output_root_override_does_not_reuse_other_root`).
*   `--force-reconvert` must be able to target single sessions. Without that,
    the only route is reconverting the whole study. A repeatable
    `--session SUBJECT/SESSION` filter for both commands, applied before
    `--limit`, provides this.
*   An explicit reconversion refuses to replace a folder that contains files
    not listed in its inventory. The issue names those files. This extends the
    current refusal to replace folders without a receipt, so files people
    added are never deleted silently.
*   If an explicit reconversion fails or comes out partial, the complete
    folder and its state record stay untouched. The failed attempt is recorded
    separately (status and `.pending/` path) and reported next to the
    `converted` status. This follows user priority 3 in the brief, "last-good
    output". It replaces the original Q7 question about a last-good versus a
    latest-attempt state model.
*   An interrupted explicit reconversion is not retried automatically. Failed,
    partial, and interrupted sessions *without* a complete folder keep today's
    retry behavior. `--retry-failed` never replaces a complete folder or its
    record.

#### Q8 Conversion provenance: record only, no comparison (decided 2026-11-04; reconfirmed at brief reconciliation)

**Decision:** Each conversion records its provenance in its receipt and in
the session's state entry. The pipeline never compares that record with the
current environment and never reports a session as "outdated".

**Recorded fields:** package version; run ID; resolved converter path; the
converter argument list; and the converter version string or strings, taken
from the outputs' `ConversionSoftwareVersion` sidecar field. The package
version identifies the classifier, the NIfTI handling, and the MR SOP class
list (Q19) in use, so no separate revision numbers are kept. No field needs
an extra file read or a converter call. The converter executable's hash is
not recorded.

**Consequences:**

*   This meets invariant 7 in the baseline spec ("outputs can be traced to
    input contents, tool/version/configuration, and processing attempt"),
    including the run ID that the baseline lacks.
*   P2 stays open in practice, deliberately. After a converter or rule
    change, existing outputs remain `converted` and nothing flags them. The
    README and the contract must say this plainly and describe the explicit
    reconversion procedure (Q7).
*   The conversion cache key is unchanged: prepared content digest, output
    root, and receipt check, plus the Q7 rule.

#### Q12 Crash recovery: automatic reconciliation at startup (decided 2026-11-04; roll-forward removed after review 2026-11-05, D2)

**Decision:** Before publishing a folder, the worker writes an intent file.
The coordinator removes the file once the publication's result is
checkpointed in state. Every non-dry run, once it holds its locks and before
planning, resolves each open intent on the roots it has locked. It rolls the
publication back, redoes the work, or adopts a folder that passes the
adoption rule below. Recovery never rolls a copied `.incoming_*` folder
forward. A dry run only reports what it would do.

The ZIP move needs no intent. It is one rename inside `DicomUploads/`, where
cross-device moves are refused. After a crash, the ZIP sits at exactly one of
the two paths, and the planner handles either case as ordinary work.

**Reconciliation rules for a publish intent** (stage → `.incoming_*` →
destination, old destination → `.previous_*`). The intent records the
`receipt_id` of the receipt it is publishing, and the digest the folder must
match (`archive_sha256` for an extraction, `content_sha256` for a
conversion), so reconciliation never has to hash a ZIP.

| Destination | Destination's receipt | Action |
|---|---|---|
| Present | The receipt that state records | The swap either finished and was recorded, or never started. Remove `.incoming_*` and `.previous_*`, and clear the intent |
| Present | The intent's own receipt, not recorded in state | Apply the adoption rule. If it passes, record the folder, remove `.previous_*` and `.incoming_*`, and clear the intent. If it fails, touch nothing and raise an issue |
| Missing | — (`.previous_*` present) | Roll back: rename `.previous_*` to the destination, remove `.incoming_*`, and clear the intent |
| Missing | — (no `.previous_*`) | Remove `.incoming_*` and clear the intent. The planner redoes the work |
| Present | Any other receipt, or none | Touch nothing; raise an issue |

A stage directory named by an open intent, and not referenced by any issue, is
removed.

An attempt publication (a partial or failed conversion published under
`Niftis/.pending/`) is never adopted. If its destination exists, it stays as
a retained attempt; `.incoming_*` is removed and the intent is cleared.

While a "touch nothing" issue (`recover:<intent id>`) is open, both planners
hold that session, so nothing replaces the folder before a person has looked
at it.

**Adoption rule.** Reconciliation, `prepare`, and `convert` all use this one
rule whenever a folder holds a receipt that state does not record. A folder
is adopted only when both conditions hold:

1.  Its receipt matches the prepared content. For an extraction, the
    receipt's `archive_sha256` equals the canonical ZIP's hash. For a
    conversion, the receipt says `complete` and its `source_sha256` equals
    the session's `content_sha256`.
2.  Every file listed in its inventory exists at its recorded size. This
    costs one `stat` call per file and runs only in these recovery cases,
    never on a cached rerun.

The state record is rebuilt from the small JSON files, without reading
images. The descriptor comes from `receipts.receipt_info`, and coverage is
recomputed with `receipts.output_series_keys`. `n_volumes` is the number of
NIfTI files in the inventory. Provenance and `run_id` are copied from the
receipt.

**Consequences:**

*   This removes the defect the review rated Blocker. A cross-device publish
    fills `.incoming_*` with `copytree`, which can copy the receipt before the
    volumes. A kill partway through the copy used to leave an incomplete
    folder that the roll-forward rule would publish as `converted`. Now
    `.incoming_*` is always discarded, and the destination is only ever
    replaced by renaming a fully copied `.incoming_*`.
*   Known cost, accepted: a crash during a first publication redoes that
    session's work, up to one converter run (14,400 s timeout). This is
    acceptable while crashes are rare.
*   A command reconciles only its own intents, and only on roots it has
    locked: `prepare` resolves extraction publications and `convert`
    resolves conversion publications.
*   Intent files live on shared storage under `Reports/.state/intents/`, never
    on node-local staging, so they survive the loss of a node. Each worker
    writes only its own files, with unique names, through `atomic_json`.
    State itself still has a single writer, the coordinator.
*   Detection costs one directory listing of open intents per run, not a scan
    of the study, so Goal 5 holds.
*   Safety rules: reconciliation never deletes a folder that is the only copy
    of a complete extraction or output, never touches a path outside the
    configured roots, and is idempotent, so a crash during reconciliation is
    itself recoverable. Every row of the table above needs a crash-injection
    test (T07), including a crash in the middle of reconciliation, a kill
    during a cross-device copy, and a refused adoption of a folder with a
    deleted volume.
*   Retained failure stages and `.pending/` attempts are not removed here.
    Cleanup stays manual until phase 2 (Q15).
*   A stage on node-local staging from a lost node no longer exists. Its
    intent is cleared and the session is replanned.
*   **Stopping (P5):** SIGTERM and Ctrl+C go to one handler, installed at the
    start of the run, that only sets a stop flag; later signals are ignored
    until the run ends. Workers stop their converters: on Linux the whole
    process group, on Windows the converter process only (Q2). They keep the
    stage and record `interrupted`; nothing is organized, gzipped, hashed, or
    published after a stop. A session counts as `interrupted` only when the
    stop flag is set or the converter died from SIGTERM or SIGKILL. Slurm
    signals `dcm2niix` directly, and any other cause, such as SIGSEGV, stays
    `failed`. The final flush and the report rewrite are attempted but may
    fail. No shutdown time is promised. The runbook shows how to have Slurm
    signal the pipeline ahead of the time limit.
*   Missing canonical ZIP (R18, left open by Q3): `prepare` already lists every
    ZIP under `SubjectsSorted/`. A prepared session whose canonical ZIP is
    missing from that listing gets a nonblocking `canonical archive missing`
    issue. Conversion continues because the extracted DICOM objects are intact
    byte copies.

#### Q13 Lock scope: lock every storage root (decided 2026-11-04)

**Decision:** Each non-dry command takes a nonblocking OS lock on a
`.mri_dicom_pipeline.lock` file in every root it touches, and records the
holder (host, PID, command, start time) for diagnosis. If any lock is already
held, the command releases the locks it took and exits with code 2, naming
the holder.

**Roots locked:**

| Command | Roots |
|---|---|
| `prepare` | upload root, DICOM root, reports root, staging root |
| `convert` | DICOM root, NIfTI root, reports root, staging root |

**Consequences:**

*   This fixes P6. Two commands that share any root exclude each other,
    whatever their `--project-root`. Locks are taken on files inside the
    roots, so two different mount paths to the same storage meet at the same
    file.
*   The old project-root lock is removed. It protected nothing the root locks
    miss, and no 0.1.0 process will run against these projects.
*   Dry runs still take no lock, as today.
*   Known failure mode, accepted: on mounts where OS locks only work within
    one node (for example Lustre mounted with `localflock`) or do not work at
    all, two runs on different nodes both get every lock and nothing is
    protected. If runs will start from more than one machine on the same
    storage, the storage counts as supported only after a two-node lock check
    has passed at the site (T10). The runbook gives that check as a
    procedure. Runs from a single machine need no site check.

#### Q4 Upload readiness: existing checks, no marker files (decided 2026-11-04; markers dropped after review 2026-11-05, D3)

**Decision:** No marker files are used. A ZIP is prepared once it opens as a
valid ZIP and its size and mtime stay unchanged through hashing and
extraction, as in 0.1.0.

**Consequences:**

*   These existing checks catch a partly copied ZIP:
    *   A ZIP's directory is stored at its end and written last, so a partial
        copy has none and fails to open.
    *   Reading each member checks its CRC-32 (`pipeline.py:420-421`).
    *   The size and mtime signature is compared before and after hashing
        and extraction (`pipeline.py:522-531`).
    *   A failed ZIP is retried once its size or mtime changes, which
        happens when the copy finishes (`pipeline.py:596-606`).
*   The review found that markers added work for every uploader, stalled
    any ZIP whose marker was forgotten, and added a crash window when the
    marker moved with the ZIP. They gave little over the checks above.
*   Known failure mode, accepted: on Linux, a copy tool that writes the end
    of the file before the middle could produce a ZIP that opens before the
    copy finishes. Common copy tools write in order. The README advises not
    to run `prepare` while copies into `Uploads/` are in progress, or to copy
    under a temporary name and rename it when the copy is done.

#### Q5 Partial overlap: settled by Q3 (recorded 2026-11-04)

**Decision:** Q3 already settles this. A new upload whose DICOM contents
differ from the prepared session in any way (subset, superset, or partial
overlap) is held as a conflict, as today. The prepared session is never
extended or merged. A re-export that adds missing series arrives as a new
`_N` session. Reopening this would mean reversing part of Q3.

#### Q14 Trust model: archives from the lab's scanners, trusted storage (decided 2026-11-04; revised after review 2026-11-05, D1)

**Decision:** Archives are assumed to come from the lab's own scanner
exports. Phase 1 parses them in-process, as 0.1.0 does. It has two
protections: ZIP member names never become output paths (existing), and
extraction does not start when the archive would not fit in free space
(Q15). The roster CSV, state, receipts, and inventories are trusted. Anyone
with write access to project storage can change what the pipeline believes,
and the contract says so.

**Consequences:**

*   Known failure mode, accepted: a hostile or malformed archive, such as a
    decompression bomb or a file that triggers a parser bug, can exhaust
    memory or crash a run. It cannot write outside the staging directory.
*   Trigger for phase 2: archives start coming from outside the lab. Phase 2
    then designs containment in its own document: caps on members and sizes,
    and parsing in a separate process. The 2026-11-04 design for this
    (Alternatives Considered) is a starting point, not a commitment.
*   Ownership receipts are still trusted when present (R17), and this is
    documented, not changed.

#### Q15 Resource limits: free-space check only (proposed 2026-11-04; reduced after review 2026-11-05, D1)

**Decision:** Before extracting, `prepare` adds up the declared uncompressed
sizes of the ZIP's members. It refuses to extract when that total plus a
reserve exceeds the free space on the staging root, or on the DICOM root
when that root is on a different filesystem. The reserve is the smaller of 5%
of the filesystem or 50 GiB. A refusal is an ordinary preparation failure:
the ZIP stays where it is, and an issue names the shortfall. `convert` keeps
its existing free-space check (`pipeline.py:773-775`) and its 14,400 s
converter timeout.

**Consequences:**

*   No other limits exist in phase 1. Member-count and member-size caps,
    converter memory caps, a cap on converter log size, a shutdown time
    budget, and `run_history.csv` rotation are not built. Phase 2 decides
    them only if its trigger occurs.
*   Known gap: `shutil.disk_usage` reports filesystem free space, not user or
    group quotas. On quota-managed cluster storage the free-space check can
    pass while the quota is exhausted. Quota exhaustion is therefore handled
    as a write failure that publishes nothing and leaves inputs in place
    (T09), and the runbook tells operators to check quotas.

#### Q15 Retention: manual cleanup until phase 2 (decided 2026-11-04; deferred after review 2026-11-05, D8)

**Decision:** Phase 1 deletes no failure material automatically. Failed
stages (`.work/convert_*`) and `Niftis/.pending/` attempts stay until an
operator removes them. The runbook says which ones are safe to delete: those
for sessions with no open `convert:` issue.

**Consequences:**

*   Trigger for phase 2: retained failure material uses too much disk. The
    2026-11-04 decision is the starting point for that phase's design. That
    decision deleted material older than N days, for resolved sessions only,
    with a `--retention-days N` option.
*   Node-local staging: a run sees only its own node's staging root. The
    runbook says so.

#### Q10 NIfTI sanitization: none (decided 2026-11-04)

**Decision:** The pipeline publishes the converter's NIfTI header and voxel
bytes unchanged and only gzips them. The structural checks (header size,
magic, dimensions, bit depth, voxel offset, payload length, the 16 MiB
extension-area cap) still run, but they never change bytes.

**Consequences:**

*   Guarantee: every published `.nii.gz` decompresses to exactly the bytes
    `dcm2niix` wrote. One test (T11) proves this by comparing the
    decompressed output with the raw converter file.
*   Privacy: the header text fields (`descrip`, `aux_file`, `intent_name`)
    and any extensions now reach the published NIfTI. `dcm2niix` typically
    writes `TE=…;Time=…` into `descrip`, so acquisition time of day appears
    in the header, even though the JSON sidecar filter still removes
    acquisition time. The privacy contract lists these channels. The session
    date is already in every path and sidecar, so the added exposure is the
    time of day. Invariant 8 names NIfTI header extensions as a channel too.
    The real-converter byte-identity smoke test asserts that the tested
    `dcm2niix` versions write no extensions. No detection code is added.
*   `sanitized_nifti` becomes a validate-and-gzip function. The gzip settings
    (empty file name, `mtime=0`, level 6) stay, so output bytes remain
    repeatable for the same converter output.

#### Q9 Scientific validation scope: pipeline integrity only (decided 2026-11-04; reopened and reverted at brief reconciliation)

*History: first decided as "pipeline integrity only". While the draft was
being compared with the brief's priority 6, the author reopened Q9 and chose a
runtime geometry cross-check. The author then reverted to integrity only,
because implementing and testing the geometry comparison would take too
long. Geometry-tag capture at preparation was kept at first, then dropped
after review (D4, 2026-11-05) because nothing in phase 1 reads it.*

**Decision:** The package verifies its own handling:

*   published NIfTI bytes equal the converter output (Q10);
*   every required series is accounted for (Q6, Q19);
*   companions are copied intact.

Whether `dcm2niix` converts correctly (orientation, slice order, spacing,
dimensions, scaling, voxel values) is documented as `dcm2niix`'s
responsibility. Supported converter versions are listed by version only.

**Consequences:**

*   The baseline spec's candidate invariant 9 is narrowed to pipeline
    integrity. Release gate G03 becomes: the integrity tests are green on
    the oldest and newest supported `dcm2niix`, and the responsibility
    boundary is documented. This is a recorded departure from the brief's
    priority 6.
*   **No geometry tags are captured** (D4). A later geometry check (Future
    Improvements) would read DICOM headers again.
*   Known failure mode, accepted: a `dcm2niix` regression or a
    scanner-specific conversion bug goes unnoticed by the package.
*   Smoke tests with the real converter on *synthetic* DICOM objects
    generated with `pydicom` are still needed. They check the pipeline's
    assumptions about `dcm2niix`: the `series_%j_…` output naming used by
    the Q6 series check, exit codes when a series is skipped (P1), and
    behavior with non-image objects in the input (Q19). They need no
    approved data. They run right after the move-only step, before Q6 and
    Q19 are built on these assumptions.
*   **Diffusion (former half of Q11), settled by this decision:** `.bval` and
    `.bvec` files are copied byte for byte, and the existing finite-number
    check stays. No checks on shape, count, or orientation are added. A
    `DWI` label does not require companions. (R13 and T12 are narrowed to
    integrity.)

#### Q11 Classification: fix the rules, keep the layout (decided 2026-11-04)

**Decision:** The classifier is rewritten to match whole tokens, with
specific patterns ordered ahead of general ones. Preparation also passes
`ImageType`, so both stages assign the same label. The label set and the
`<scan_type>/` folder layout stay the same. The label is still documented as
an unverified hint. The package version in each provenance record identifies
the classifier used (Q8).

**Phase placement:** classifier revision 2 stays in phase 1 because it
changes which folder a series lands in. Landing it before production data is
converted means every production session uses one layout (Goal 4).

**Behavior observed on 2026-11-04** (`classify()` run on synthetic
description strings; no study data):

| Description | Revision 1 (today) | Revision 2 (required) |
|---|---|---|
| `AX T2* GRE` | `T2` | `GRE` |
| `AX T2 STAR GRE` | `T2` | `GRE` |
| `SWI mIP`, `SWI_MinIP` | `SWI` | `SWI_MINIP` |
| `HEADCOIL T1` | `EADC` | `T1` |
| `PROGRESSIVE` | `GRE` | `OTHER` |
| `T1 FLAIR` | `FLAIR` | `FLAIR` (unchanged) |

**Consequences:**

*   No new labels. T2* sequences map to the existing `GRE` label, so
    revision 2 adds no folders.
*   Tokenizing: uppercase description and protocol text, split on any
    character other than a letter, a digit, or `*`. Multi-word patterns
    (`MIN IP`) match consecutive tokens.
*   `ImageType` is added to the header tags read during preparation. This is
    a tag on the existing header-only read, not an extra read.
*   T13 becomes a test table of synthetic description strings in common
    vendor styles, with expected labels and a human review of the table. A
    confusion matrix built from real scanner descriptions would need study
    data and is not required (Q9).
*   Known failure mode, accepted: naming conventions not in the table can
    still be mislabeled.

#### Q22 Migration: none (decided 2026-11-04; dropped after review 2026-11-05, D5)

**Decision:** No project has been processed by 0.1.0, and no 0.1.0 output has
to be kept (author, 2026-11-05). Phase 1 has no `migrate` command. `prepare`
and `convert` refuse to run on an index whose schema is not 3. They write
nothing and name the file, and the operator starts with a new report root.

**Consequences:**

*   This removes the upgrade journey, `migrate.py`, its tests and test
    helper, the rollback copy of the old index, and every rule for migrated
    sessions.
*   The 2026-11-04 design (a metadata-only migration) applies only if a
    0.1.0 project turns up later. That project would then need its own
    design.

#### Q23 Project relocation: refuse in phase 1, `relocate` on its trigger (decided 2026-11-04; trimmed after review 2026-11-05, D8)

**Decision:** State keeps absolute paths. In phase 1, `prepare` and `convert`
refuse to run when the index records canonical ZIPs but none of them lies
under the configured uploads root. They print "Project appears moved …
Moving a project is not supported yet". Only the uploads root is checked
(simplified 2026-11-05, step 6): the DICOM and NIfTI roots can be overridden
on purpose (`--output-root`), so a mismatch there is not evidence of a move.

**Consequences:**

*   The check compares strings in memory and adds no filesystem calls. It is
    skipped for an empty index.
*   Without the check, a moved project would re-extract every session, and
    conversion would flag every session as `output modified` (Q7).
*   Trigger for phase 3: a project has to move. That phase designs a
    `relocate` command. The 2026-11-04 design is its starting point: rewrite
    the stored path prefixes, then check every receipt at the new location
    using metadata only.

#### Q18 Scale: measure before tuning (decided 2026-11-04; deferred after review 2026-11-05, D8)

**Decision:** Phase 1 keeps today's scale behavior. Its only performance
promise is Goal 5: a cached rerun makes no content reads.

**Consequences:**

*   Trigger for phase 3: runs are measured as too slow. The starting point
    for that phase is the 2026-11-04 work:
    *   targets of up to about 20,000 sessions, a cached rerun of at most
        5 minutes, and coordinator memory of at most 2 GB;
    *   defects found by code trace: building each subject's state copy
        scans every session key, the 15-second report rewrite, and an index
        estimated at 30–60 MB at 20,000 sessions.

#### Q21 Index storage: single JSON index with sequence numbers and a backup (decided 2026-11-04; trimmed after review 2026-11-05)

**Decision:** State stays in one `index.json` with one backup copy,
`index.json.bak`. The flush cadence stays as today: at most every 10 seconds
while events arrive, after reconciliation, and at the end of each run.
Per-event journals cover the time between flushes.

**Consequences:**

*   Journal sequence numbers (P11 fix): every event gets a number that only
    increases. The index and the backup each record the last number they
    include. Startup replays only journals with higher numbers than the file
    it loaded, so a partial deletion cannot roll a session back.
*   Flush order: write a temporary file and `fsync` it; replace
    `index.json.bak` with the current `index.json` (`os.replace`); replace
    `index.json` with the temporary file (`os.replace`). `os.replace`
    overwrites on Windows, where a plain rename fails when the target
    exists. Journals are deleted only up to the backup's sequence number, so
    the backup plus the remaining journals can always rebuild the current
    state.
*   **Load decision table (R09):**

    | `index.json` | `index.json.bak` | Action |
    |---|---|---|
    | Parses | Any | Use `index.json` |
    | Missing | Parses | Use `.bak` silently. A crash between the two replaces leaves this state |
    | Does not parse | Parses | Use `.bak`, log at error level, and note it in the history row. At the first save, move the bad file aside to `index.json.corrupt-<run_id>` (a dry run never saves, so it writes nothing). No persistent issue is raised |
    | Missing or does not parse | Missing or does not parse, with at least one of the two present | Stop with exit 2 and name both files |
    | Missing | Missing | New project. The adoption rule (Q12) rebuilds state from receipts as `prepare` and `convert` meet existing folders |

    After loading, journals with a higher sequence number than the loaded
    file's `last_seq` are replayed. The next flush uses the in-memory
    `last_seq`, so it never writes the corrupt file over a good `.bak`.
*   Empty state with full roots (D6): if the report root is mistyped or its
    state is lost, the next runs hash each canonical ZIP once and adopt the
    existing folders. No data is lost and nothing is republished. The cost is
    time, which is accepted. The runbook says so.
*   Known limit, accepted: every run parses the whole index. Scale work waits
    for phase 3 (Q18).

#### Q2 Supported environments: Linux and Windows for production (decided 2026-11-04)

**Decision:** Linux and Windows are both supported for production data.
Windows limitations must be fixed or covered by explicit tests; documenting
them is not enough. `dcm2niix` is supported over a tested range of versions.

| Dimension | Supported | Evidence |
|---|---|---|
| Operating system | Linux, Windows; macOS not supported | Full CI suite on `ubuntu-latest` and `windows-latest` |
| Python | 3.11 and later, limited to versions still supported upstream (3.10 reached end of life in October 2026); approved 2026-11-04 | CI on 3.11, 3.12, 3.13, 3.14 on both operating systems |
| `dcm2niix` | The oldest and newest release versions that pass the real-converter smoke tests (Q9), listed in the README | CI downloads both release binaries for Linux and Windows and runs the smoke tests |
| Storage | Local disk. A shared filesystem used from more than one machine is supported once it passes the site's two-node lock check (Q13) | Site deployment check (T10), only when needed |
| Scanners | No scanner-specific claims (Q9) | Not applicable |

**Windows behavior in phase 1:**

*   **Process-tree termination (R11).** On Windows, stopping a run stops the
    converter but not processes the converter started. This is the only
    statement of Windows containment in this document. Trigger for phase 2:
    leftover converter processes are seen on Windows.
*   **Path length.** A path longer than Windows allows fails that session
    with a reported error, and nothing is published. Trigger for phase 2 (a
    hold at planning time): such failures are seen.
*   **Case-insensitive collisions (R14).** Roster IDs that differ only by
    letter case (`S001` and `s001`) are rejected while the roster is loaded,
    on every platform, so that behavior does not depend on the filesystem.
*   **Reserved device names (R14).** IDs equal to `CON`, `PRN`, `AUX`, `NUL`,
    `COM0`–`COM9`, or `LPT0`–`LPT9`, in any letter case, are rejected on
    every platform. Current Windows documentation reserves `COM0` and `LPT0`
    as well.
*   **Files in use during publication.** If a rename during publication
    fails because a file in the destination is open (Windows refuses the
    rename), the existing rollback in `publish_tree` keeps the old folder,
    and the session fails with an issue. The existing
    `test_publish_rolls_back_if_swap_fails` covers it.
*   **Report files held open.** If a program such as Excel holds
    `session_status.csv`, `issues.csv`, or `run_history.csv` open, the failed
    write is logged and retried at the next report write, and the run
    continues. Today such a failure ends the run with no history row.
    Explicit test.

**Consequences:**

*   `requires-python` becomes `>=3.11`, and the classifiers and CI matrix
    follow. CI runs 8 test jobs (2 operating systems × 4 Python versions),
    the existing package job, and up to 4 real-converter jobs (2 operating
    systems × 2 `dcm2niix` versions).
*   On Linux the converter runs in its own session (`start_new_session`) and
    is stopped with a signal to the whole process group.
*   The initial `dcm2niix` range is set during implementation: the newest
    release at that time and the version the target cluster's module system
    provides, if both pass.

#### Q16 Time zones: keep local time without offset (decided 2026-11-04)

**Decision:** Log lines and run IDs keep the host's local time without an
offset, as today.

**Consequences:**

*   Known failure modes, accepted: during the repeated hour at the
    daylight-saving fall-back change, log lines and run IDs are ambiguous.
    Nodes set to different time zones produce timestamps that cannot be
    compared. The README says so.
*   No phase 1 behavior depends on a timestamp. A later phase that uses
    timestamps to decide what to delete (Q15 retention) stores them as UTC
    epoch seconds, so clock-display choices never change what is deleted.

#### Q17 Release policy: Apache-2.0 (decided 2026-11-04)

**Decision (author):** The package is licensed under Apache-2.0.

**Approved with it on 2026-11-04; these apply from the first public release
(phase 4):**

| Topic | Policy |
|---|---|
| Versioning | Semantic versioning. A state schema change that is not additive (Q1) ships with a migration and at least a minor-version bump. |
| Deprecation | A deprecated CLI option keeps working, with a warning, for at least one minor release before removal. |
| Security reports | `SECURITY.md` pointing to GitHub private vulnerability reporting. |
| Support | Best effort by the maintainer, with no response-time commitment. Only the latest minor release is supported. |

**Consequences:**

*   New files `LICENSE` (Apache-2.0 text) and `NOTICE`. `pyproject.toml`
    uses the SPDX expression `license = "Apache-2.0"` with
    `license-files = ["LICENSE", "NOTICE"]`, which needs `setuptools>=77`.
    No license classifier is added, because the SPDX field replaces it.
    setuptools puts `license-files` in both the wheel and the sdist, so
    `MANIFEST.in` needs no change. Checking that both files are present in
    both artifacts (`tests/check_distribution.py`) waits for phase 4.
*   Dependency compatibility: `pydicom` (MIT) can be used from Apache-2.0
    code. `dcm2niix` is called as an external program and not
    redistributed.
*   The `Operating System :: OS Independent` classifier is replaced with
    Linux and Windows classifiers (Q2).

#### Q1 Phases: production-ready phase 1, later phases on triggers (decided 2026-11-04; revised after review 2026-11-05, D1, D4, D8)

**Decision:** Phase 1 is the lab's production version. It closes P1 and P3
to P11, leaves P2 open on purpose (Q8), and introduces state schema 3. It
also carries every change to the output layout or the state format, so
production data is converted under one layout from its first run. Phases 2–4
start only when their triggers occur. See the [Phase map](#phase-map).

**Consequences:**

*   Additive state changes need no migration. A later phase adds optional
    fields, and readers treat a missing optional field as its default.
    Phase 1 therefore carries only the fields it reads.
*   Known cost, accepted: phase 1 changes most of `pipeline.py`. It is the
    largest phase and the slowest to review.
*   Windows gets the same stop handling (Ctrl+C) in phase 1. Stopping a run
    on Windows stops the converter, not processes the converter started
    (Q2).

#### Module layout: full restructure by responsibility (decided 2026-11-04)

**Decision:** In phase 1, `pipeline.py` is replaced by modules split by
responsibility. The module list is in the Detailed Implementation Plan.

**Consequences:**

*   Recorded risk: refactoring and behavior changes mixed in one diff. This
    is handled by ordering: step 0 moves code with no logic change and must
    pass the existing tests before any behavior change lands.
*   Tests patch functions where they are defined, and modules import their
    sibling modules rather than individual names, so that patching works.

## Alternatives Considered

The concepts brief has no "Alternatives Weighed" section. Every alternative
below was weighed at the author's decision gates on 2026-11-04 or in the
review decisions of 2026-11-05. Rows marked "chosen 2026-11-04, … after
review" are options that were first chosen and then replaced by the review
decisions. The "Cons" column gives the failure condition stated when the
options were presented.

| Decision | Rejected alternative | Pros | Cons (recorded trade-off) |
|---|---|---|---|
| Document scope | One document covering every area in full file-level detail | A single complete plan | Needs every decision settled at once; stalls on decisions that need site evidence first |
| Document scope | One area only (data safety) | Smallest document | A deferred area (such as provenance) could change the schema again and force a second migration |
| Q3 Canonical inputs | Replace only on explicit command, keeping superseded copies | Corrections can keep the same label; old data is kept | More code and disk; superseded copies need retention; risk of routine use without review |
| Q3 Canonical inputs | Allow replacement, but record and flag it | Smallest change | A wrong export becomes the session's data before anyone reviews it |
| Q5 Partial overlap | Merge or extend a session with a superset upload | One visit keeps one label | Excluded by Q3: accepted session content never changes |
| Q6 Meaning of `converted` | Output or a configurable exclusion rule per series | No permanent `partial` for legitimate non-image series | An overly broad rule hides a missing series; needs SOP class plus migration. Q19 later brought back a narrower form: a fixed SOP class list instead of configurable rules |
| Q6 Meaning of `converted` | Report-only warning | No migration | `converted` still does not mean complete; P1 stays open |
| Q19 DICOM eligibility | Extract MR image SOP classes only | Smaller extraction; clean converter input | Image series outside the list are never extracted or converted |
| Q19 DICOM eligibility | Keep current acceptance | No list to maintain | Sessions with non-image objects stay `partial` permanently |
| Q20 Repeats and multi-study | Accept with warnings | Less operator work | An export that merges two people's exams converts under one ID with only a warning |
| Q20 Repeats and multi-study | Keep current behavior | No change | Repeated slices reach the converter; merged exports give no signal |
| Q7 Reconversion | Automatically repair damage only | Damaged folders fix themselves | Edits that keep the file size go undetected; needs a rule for preserving added files |
| Q7 Reconversion | Automatically reconvert on damage and on upgrades | Outputs track the current converter | One upgrade reconverts the whole study; replaces folders that are in use |
| Q8 Provenance | Full fingerprint including the converter binary hash | Detects any converter change | A rebuild of the same version marks everything outdated; existing outputs start as unknown |
| Q8 Provenance | Version-level fingerprint with an "outdated" report | Flags converter upgrades | One converter call per run; different builds with the same version string look identical |
| Q12 Crash recovery | Roll forward a copied `.incoming_*` (chosen 2026-11-04, removed after review, D2) | Saves redoing a session's work after a crash during the swap | A cross-device copy can leave a receipt in an incomplete `.incoming_*`; telling complete from partial needs an extra copy step or intent phase |
| Q12 Crash recovery | Explicit `repair` command | A person sees every action | Sessions stay stuck when nobody runs repair |
| Q12 Crash recovery | Detect and document manual steps | No repair code to get wrong | Manual moves on shared storage are error-prone |
| Q13 Lock scope | Root locks plus a heartbeat lease | Protects mounts whose locks only work within one node | A crashed run's lease blocks new runs; clock skew between nodes |
| Q13 Lock scope | Project lock plus root claim markers | Smallest change | No protection on node-local-lock mounts; claims go stale after a move |
| Q4 Upload readiness | Required marker files `.zip.done` or `.zip.sha256` (chosen 2026-11-04, dropped after review, D3) | Explicit readiness signal; optional checksum | Every uploader must write a marker; a forgotten marker stalls the ZIP; moving the marker with the ZIP added a crash window |
| Q4 Upload readiness | Quiet period of N minutes | No noise; no new protocol | Stalls longer than N; every upload is delayed; depends on the server clock; Windows copies keep the source mtime |
| Q14 Trust model | Untrusted archives parsed in a child process per archive, with memory, CPU, and size caps (chosen 2026-11-04, deferred after review, D1) | Contains parser bugs and decompression bombs | A process start per archive; Windows needs Job Objects; little benefit while archives come from the lab's own scanners |
| Q14 Trust model | Untrusted archives and storage (HMAC receipts) | Detects forged receipts | Key management; a lost key invalidates every receipt |
| Q15 Retention | Explicit cleanup command | Nothing disappears unless a person chooses it | Grows until the disk reserve stops new work |
| Q15 Retention | Keep the last K attempts per session | Bounded per session | Discards the first failure's diagnostics; grows with the number of failing sessions |
| Q10 Sanitization | Keep current clearing and verify the rest | Header text and time of day stay out of outputs | Data a future converter writes into extensions is dropped silently |
| Q10 Sanitization | Clear text fields; refuse extensions | Nothing is dropped silently | Sessions go `partial` if the converter starts writing extensions |
| Q9 Validation scope | Reference fixtures in CI | Catches converter regressions | Approved fixtures are hard to get; never complete |
| Q9 Validation scope | Runtime cross-check against DICOM geometry (chosen when Q9 was reopened, then reverted) | Checks real study data; addresses brief priority 6 | Re-implements converter geometry logic; false alarms on edge cases. The author gave implementation time as the reason for reverting |
| Q9 Revert | Capture geometry tags at preparation (chosen 2026-11-04, dropped after review, D4) | A later check could run without reading headers again | Seven fields per object that nothing in phase 1 reads |
| Q11 Classification | Keep the rules and call the label a hint | No layout change | Known mislabels stay (T2* as T2, SWI mIP as SWI) |
| Q11 Classification | Remove labels from output paths | Folder names make no claims | Breaks downstream scripts; mixed layouts |
| Q22 Migration | A metadata-only `migrate` command (chosen 2026-11-04, dropped after review, D5) | Upgrades 0.1.0 projects in place | No 0.1.0 project exists; the command carried most of the open review findings |
| Q23 Relocation | Root-relative paths with identity files | Moves need no extra step | Copies fool the root guard; moves that lose directory mtimes invalidate every receipt |
| Q23 Relocation | Keep absolute paths and document the cost | No code | Full re-extraction and manual reconversion after any move |
| Q18 Scale | Up to about 2,000 sessions | The JSON index works unchanged | Rework when a project grows |
| Q18 Scale | Up to about 200,000 sessions | Supports a data-coordinating center | Redesign well beyond hardening |
| Q21 Index storage | 256 bucketed JSON files | Rewrites only what changed | 257 file opens per run; crash consistency across files |
| Q21 Index storage | SQLite | Incremental, atomic writes | Locking on network filesystems is unreliable; harder to repair by hand |
| Q2 Environments | Production on Linux, development only on Windows | Smaller test burden | Windows production users are unsupported |
| Q2 Environments | Linux with one pinned `dcm2niix` version | Smallest matrix | Warnings after every site upgrade; one version call per run |
| Q16 Time zones | UTC everywhere | Unambiguous; sorts correctly | Harder to line up with local scheduler logs |
| Q16 Time zones | Local time with offset | Readable and unambiguous | Two clocks in use; sorting across a daylight-saving change is wrong |
| Q17 License | MIT | Short; matches `pydicom` | No explicit patent grant |
| Q17 License | BSD-3-Clause | Bars use of names for endorsement | No explicit patent grant |
| Q1 Phases | No-migration fixes first | First release ships soonest | P1 and P11 stay open in the first release; reports change twice |
| Q1 Phases | Correctness core first | Smaller phase 1 | P5 and P6 stay open in the first release |
| Q1 Phases | Phase 1 as a non-production step until phase 4 (review option D1 (b)) | Phase 1 needs no production-readiness evidence | The Critical bugs (P1, P3, P8, P10) stay in production use until phase 4 |
| Module layout | Keep a single `pipeline.py` | Smallest structural change; existing test patch targets keep working | The module roughly doubles to about 2,000 lines |
| Module layout | New modules only for new subsystems | New code stays separate | Risk of import cycles between `pipeline.py` and the new modules |

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Security and privacy:** Archives are assumed to come from the lab's own
    scanner exports and are parsed in-process, as today (Q14). The
    free-space check (Q15) and the existing member-name rule are the only
    archive protections in phase 1. State, receipts, and the roster are
    trusted. Privacy channels are listed under invariant 8. Phase 1 changes
    two of them: NIfTI header text now passes through (Q10), and unredacted
    converter output never reaches disk (P8).
*   **Performance:** Goal 5 applies: a cached rerun makes no content reads,
    checked by `test_1000_cached_sessions_do_not_read_scan_contents`. Scale
    budgets wait for phase 3 (Q18).
*   **Rollout:** Install phase 1 from the repository. Keep a copy of every
    ZIP outside the project before the first run. Run `prepare --dry-run`
    (with `--import-legacy` when ZIPs sit in `DicomUploads/Subjects/`),
    review the plan, then run `prepare` and `convert` (CUJ 6). No migration
    is needed (Q22).
*   **Rollback:** Reinstall 0.1.0 and give it a new, empty report root,
    because 0.1.0 refuses a schema-3 index. With empty state, 0.1.0 re-prepares and
    reconverts every session it finds, and it replaces folders that hold
    receipts. ZIPs are not modified. The runbook documents this.

## Additional Resources

*   [`concepts_brief.md`](./concepts_brief.md): input brief.
*   [`mriDICOMpipeline-current-state-spec.md`](./mriDICOMpipeline-current-state-spec.md):
    the baseline behavior, review register R01–R20, and stress-test matrix
    T01–T18. It stays unchanged as the baseline.
*   [`current-source-manifest.json`](./current-source-manifest.json):
    baseline file hashes.
*   EGM [README](https://github.com/google/elephant-goldfish-model/blob/main/README.md)
    and [workflow guide](https://github.com/google/elephant-goldfish-model/blob/main/docs/getting_started.md).

## Future Improvements

*   **Phases 2–4:** see the [Phase map](#phase-map). Each starts only when
    its trigger occurs and then gets its own design document that follows
    this contract.
*   **Override for series `dcm2niix` cannot convert (D7):** decided after
    the real-converter smoke tests show how often it happens.
*   **Overlap detail in conflict issues:** a held conflict could report how
    many DICOM objects the two uploads share, using both inventories, to
    speed up operator decisions (Q5).
*   **"Outdated" reporting:** Q8 rejected comparing provenance with the
    current environment. It can be added later without a schema change,
    because the provenance record already exists.
*   **Runtime geometry cross-check (Q9):** compare each output's dimensions
    and corner coordinates with geometry tags read from the DICOM headers
    (phase 1 does not capture them, D4). It addresses brief priority 6 and
    was deferred because of implementation time. The design worked out when Q9 was reopened (sorted
    dimensions; 4D volume count; eight corner voxel centers within the larger
    of 0.1 mm or 1% of the smallest voxel; sform, else qform; mosaic,
    multi-frame, and multi-orientation series unchecked) is a starting point,
    not an approved design.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

This plan covers **phase 1 only**. It restructures the package by
responsibility, as the author decided, and implements every phase 1 item in
the [Phase map](#phase-map). It contains no implementation code; signatures,
schemas, and rule tables define the behavior.

### Implementation order

Restructuring and behavior changes land as separate commits, so a reviewer
can see that behavior changed only where this plan says it does. That was
the recorded risk of the restructure option.

| Step | Content | Verified by |
|---|---|---|
| 0 | Move code from `pipeline.py` into the new modules **with no logic change**. Retarget imports in tests and launchers. Move the shared test helpers into `tests/support.py` and `tests/conftest.py`. Delete `pipeline.py`. Merged on its own. | All existing test cases pass with only import or patch-target edits |
| 1 | Real-converter smoke tests: `test_real_converter.py` (`dcm2niix`'s own behavior only), the synthetic DICOM helpers it needs, and the CI `real-converter` job | The four smoke tests run against real `dcm2niix`, and their results are recorded. If `dcm2niix` exits nonzero with a non-image object present, Q19 is reopened before step 6 |
| 2 | Packaging and CI: Python 3.11+, `setuptools>=77`, Apache-2.0 `LICENSE`/`NOTICE`, the CI test matrix | `python -m build`, CI green |
| 3 | State schema 3: journal sequence numbers, `.bak`, `os.replace` flush, the load decision table, refusal of other schemas | `test_recovery.py` state tests, `test_project_configuration.py::test_old_state_rejected_without_writes` |
| 4 | Identity rules (case-only collisions, reserved names); per-root locks | `test_identity.py`, `test_locks.py` |
| 5 | Intents, reconciliation, the adoption rule, stop handling (SIGTERM, Ctrl+C) | `test_recovery.py` |
| 6 | Preparation: immutable canonical ZIPs, SOP classes, repeats and multi-study holds, `--session`, `--accept-multi-study`, canonical rehash under `--audit`, missing-archive issue, free-space check, moved-project refusal | `test_inputs.py` |
| 7 | Conversion: strict coverage, Q7 rules (including `--audit --session` revalidation), last-good and last attempt, pass-through NIfTI, provenance, streaming redaction, process groups, classifier revision 2 | `test_conversion.py`; session-level assertions added to `test_real_converter.py` |
| 8 | Reports and run history: new columns and outcomes, report files held open, the history-row note for an index recovered from `.bak` | `test_conversion.py`, `test_recovery.py::test_history_row_written_on_early_abort` |
| 9 | README phase 1 updates; runbook | Review against the checklist in Verification Plan |

From step 3 onward, each P-item's regression test (see the traceability
table) is written first and fails on the step 0 code. That is the evidence
Goal 4 asks for. Tests of behavior that phase 1 keeps unchanged pass on both.

Each step adds the issue keys it raises to the issue table in `reports.py`
(file 13), with their blocking flag, so later steps never depend on step 8.
Each change to an existing test (files 23–24) lands in the step that causes
it.

**Import convention (applies to every module):** modules import sibling
modules, not names (`from . import fsutil`, then `fsutil.sha256(...)`), so
tests can patch a function where it is defined. One exception: modules
import `State` directly (`from .state import State`). Many functions already
have a local variable named `state`, and the test helpers have a `state()`
function, so importing the module itself would clash with both.

### Target Files to Create/Modify

#### 1. `src/mri_dicom_pipeline/pipeline.py` (Delete)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: Its contents move into the modules below in step 0.
    Nothing re-exports it: the CLI is the supported interface (baseline §4),
    and the launchers and tests are updated. Items without a home elsewhere:
    `VERSION` becomes `config.SCHEMA_VERSION`; `problem()` moves to
    `reports.py`; `run()` moves to `runner.py`; `sanitized_nifti` moves to
    `nifti.py`; and `redact_diagnostic` moves to `convert.py`. In step 0, the
    import in `cli.py` is retargeted to `runner`.

#### 2. `src/mri_dicom_pipeline/config.py` (Create)

*   **Type / Scope**: Python module, CLI parsing and root configuration.
*   **Proposed Changes**:
    *   Constant `SCHEMA_VERSION = 3` (it was `VERSION = 2` in
        `pipeline.py`).
    *   `config_parser(phase)`, moved. Adds a repeatable `--session KEY` to
        both commands and a repeatable `--accept-multi-study KEY` to
        `prepare`. New `--dry-run` help text: "Plan only: no project writes
        or converter calls; hashes files only when combined with --audit".
        New `--audit` help text adds "prepare also re-hashes canonical ZIPs;
        convert --session revalidates restored folders".
    *   `configure(args, phase)`, moved. Also validates every `--session` and
        `--accept-multi-study` value with `identity.parse_key`, and sets
        `cfg.lock_roots` per the Q13 table (`prepare`: uploads, DICOM,
        reports, staging; `convert`: DICOM, NIfTI, reports, staging) and
        `cfg.run_id` (format unchanged: `YYYYMMDD_HHMMSS_<8 hex>`, local
        time, Q16).

#### 3. `src/mri_dicom_pipeline/identity.py` (Create)

*   **Type / Scope**: Python module, identity grammar and roster (T01, T02).
*   **Proposed Changes**:
    *   `NAME` and `SAFE_ID` regexes, moved. `RESERVED_NAMES` = `CON`,
        `PRN`, `AUX`, `NUL`, `COM0`–`COM9`, `LPT0`–`LPT9`, compared in upper
        case.
    *   `valid_id(text) -> bool`: `SAFE_ID` full match and not reserved.
    *   `parse_name(name, legacy=False) -> (sid, date, label)`, moved; an ID
        that is not valid raises `ValueError`, so the ZIP is held.
    *   `parse_key(text) -> (sid, label)` for `SUBJECT/SESSION` CLI values.
        It lands in step 6, with `--session`, its first caller.
    *   `load_roster(...)`, moved. Invalid IDs (now including reserved names)
        keep today's "row ignored" warning. After reading, IDs are grouped
        by `casefold()`. Every group with more than one spelling has all its
        spellings removed from `ids` and `pairs`, with one warning naming
        the first row number of each spelling. A roster left with no valid
        pairs still fails, as today.

#### 4. `src/mri_dicom_pipeline/fsutil.py` (Create)

*   **Type / Scope**: Python module, filesystem primitives.
*   **Proposed Changes**: `atomic_json`, `read_json`, `signature`, `sha256`,
    `owned`, and `remove_tree`, moved unchanged. Added:
    *   `swap_names(destination) -> (incoming, previous)`: returns the
        `.incoming_<12 hex>` and `.previous_<12 hex>` sibling paths.
    *   `publish_tree(stage, destination, ownership_root, incoming,
        previous)`: today's algorithm, using the names the caller already
        recorded in its intent.
    *   `utc_now() -> float` (epoch seconds), used in intent and lock-owner
        files.
    *   `write_text_or_log(path, writer, logger) -> bool`: runs an atomic
        report write. On `PermissionError` or another `OSError` it logs a
        warning and returns `False` instead of raising (Q2, report files held
        open).

#### 5. `src/mri_dicom_pipeline/receipts.py` (Create)

*   **Type / Scope**: Python module, receipts, entries, and coverage.
*   **Proposed Changes**:
    *   `receipt_info(root, receipt, inventory)`, moved. Adds `receipt_id`
        from the receipt; keys stay absolute, as today.
    *   `fast_valid(entry, kind, audit=False)`, moved unchanged apart from
        the version constant. The `kind` argument stays, as today.
    *   `RECEIPT` and `INVENTORY` name the two control files.
    *   `prepared_entry(sid, date, label, canonical, digest, inventory,
        destination, conversion)` builds the schema-3 session entry from the
        published extraction (step 5). It is used by `prepare.prepare_one`
        and by adoption. Step 6 adds `accepted_multi_study`; step 7 adds
        `last_attempt`.
    *   `conversion_record(destination, status, n_volumes, source_sha256)`
        adds `receipt_info` (step 5). Step 7 adds `provenance` and
        `coverage`.
    *   `receipt_id(root)`: the receipt ID in a folder, or `None`.
    *   `required_series(inventory) -> (required_keys, excused_keys)`.
    *   `output_series_keys(output_root, inventory_files) -> set`: reads the
        `.json` sidecars listed in a conversion inventory and returns their
        `source_series_key` values. Used only by adoption.
    *   `coverage(required, excused, converted) -> dict` with `required`,
        `converted`, `excused`, and `missing` (sorted lists).
    *   `adoptable(root, kind, expected_digest) -> bool`: the Q12 adoption
        rule. It is true when the receipt matches `expected_digest`
        (`archive_sha256` for `extract`; `complete` and `source_sha256` for
        `convert`) and every inventory file exists at its recorded size (one
        `stat` per file). The state event for an adopted folder is built from
        `prepared_entry` or `conversion_record` (`recovery.py`).

#### 6. `src/mri_dicom_pipeline/state.py` (Create)

*   **Type / Scope**: Python module, persistent index (Q21, P11).
*   **Proposed Changes**: `class StateError(ValueError)`, so existing callers
    and tests that expect `ValueError` keep working. `class State(report_root,
    run_id=None)`; the optional `run_id` keeps today's one-argument calls in
    tests working:
    *   **Load:** the Q21 load decision table. A corrupt `index.json` sets
        `recovered_from_backup = True`, and the runner logs it at error
        level. The file is renamed to `index.json.corrupt-<run_id>` (or
        `-<epoch seconds>` without a run ID) at the first save, not at
        load, so a dry run writes nothing. The history-row note arrives with
        the history changes in step 8. If neither file can be read but one
        exists, raise `StateError` naming both paths. If neither exists,
        start empty (`version 3`, `last_seq 0`).
    *   **Version:** any version other than 3 raises
        `StateError("Unsupported state version N in <path>; this version
        starts new projects only. Use a new --report-root.")` and writes
        nothing. The message keeps today's "Unsupported state version" prefix.
    *   **Replay:** apply journals whose `seq` exceeds the loaded
        `last_seq`, in sequence order.
    *   `checkpoint(event)`: assign `seq = last + 1`, write the journal
        `<seq:016x>_<4 hex>.json` with `atomic_json`, apply the event, and
        flush if more than 10 s have passed since the last flush (as today).
    *   `flush()`: set `last_seq` from memory, write a temporary file with
        `fsync`, `os.replace` `index.json` onto `index.json.bak` (or move a
        corrupt `index.json` aside instead, keeping the good `.bak`), then
        `os.replace` the temporary file onto `index.json`, then delete
        journals with `seq <= (backup's last_seq)`.
    *   `apply(event)`: as today (session replacement, source upserts, issue
        clears and upserts).

#### 7. `src/mri_dicom_pipeline/locks.py` (Create)

*   **Type / Scope**: Python module, locking (Q13).
*   **Proposed Changes**:
    *   `ProjectLock`, moved, and kept as the single-file lock primitive that
        `RootLocks` uses. Nothing takes the project-root lock any more (Q13).
    *   `class LockBusy(RuntimeError)`. It is the error `ProjectLock` raises
        today (`test_lock_and_pending_journal_recovery` expects
        `RuntimeError`). Message: "<root> is locked by <host> pid <pid>
        (<command>, run <run_id>, since <time>)".
    *   `RootLocks(roots, command, run_id)`, a context manager composed of
        one `ProjectLock` per root. It creates each root if missing, then, in
        sorted path order, locks `<root>/.mri_dicom_pipeline.lock`. After each
        lock it writes `<root>/.mri_dicom_pipeline.lock.owner`
        (`{host, pid, command, run_id, started_utc}`). On the first failure
        it releases the locks already taken, reads that root's owner file
        (if it is missing or unreadable, the holder is "unknown"), and
        raises `LockBusy`. On exit it releases everything and removes its own
        owner files.

#### 8. `src/mri_dicom_pipeline/recovery.py` (Create)

*   **Type / Scope**: Python module, intents and reconciliation (Q12).
*   **Proposed Changes**:
    *   Intent schema, in `Reports/.state/intents/<id>.json`:
        `{version: 3, id, kind: "publish", stage: "prepare" | "convert", key,
        run_id, created_utc, stage_dir, destination, incoming, previous,
        ownership_root, receipt_id, expected_digest, attempt}`. A `prepare`
        intent also carries `subject_id`, `date`, `label`, and `archive` (the
        canonical ZIP), which adoption needs. There is no ZIP-move intent
        (Q12). A worker puts the intent ID in its event only once the result
        is complete; an intent left by a failure is resolved at the next run.
    *   `write_intent(reports_root, **fields) -> id` (workers) and
        `clear_intents(reports_root, ids)` (coordinator, after the matching
        event is checkpointed).
    *   `blocked_keys(state) -> set`: sessions with an open `recover:` issue,
        which both planners hold.
    *   `reconcile(cfg, phase, state, logger, dry_run) -> int` implements
        the Q12 table in the Decision Log and returns how many intents it
        resolved. Rules:
        *   It skips intents of the other command and intents whose
            `ownership_root` is not in `cfg.lock_roots`.
        *   Every path it touches must pass `fsutil.owned` against the
            intent's root.
        *   Adoption uses `receipts.adoptable`, builds the entry with
            `receipts.prepared_entry` or `receipts.conversion_record`, and
            records it through `state.checkpoint`.
        *   It clears a `recover:` issue whose intent file a person has
            removed.
        *   A case where it touches nothing raises the blocking issue
            `recover:<intent id>` (issue table, file 13).
        *   Every action is logged.
        *   It is idempotent: each rule checks the current filesystem state
            before acting.
        *   A dry run only logs the planned actions.

#### 9. `src/mri_dicom_pipeline/classify.py` (Create)

*   **Type / Scope**: Python module, classifier revision 2 (Q11).
*   **Proposed Changes**: Signature unchanged:
    `classify(description, protocol="", image_type=None)`.
    *   **Tokens:** the uppercase description and protocol text, split on
        every character other than `A–Z`, `0–9`, or `*`. The raw uppercase
        text is kept for the `+C` check.
    *   **Match kinds:** *word* (the token equals the pattern), *prefix*
        (the token matches `^T1[A-Z]*$` or `^T2[A-Z]*$`), *contains* (the
        pattern is a substring of a token), and *sequence* (consecutive
        tokens).
    *   **Rules, first match wins:**

        | # | Label | Matches |
        |---|---|---|
        | 1 | `LOCALIZER` | contains `LOCALIZER`, `SCOUT`, `SURVEY` |
        | 2 | `EADC` | word `EADC` |
        | 3 | `ADC` | word `ADC` |
        | 4 | `QSM` | word `QSM` |
        | 5 | `SWI_MINIP` | word `MINIP`; sequence `MIN IP`; word `MIP` together with word `SWI` or `SWAN` |
        | 6 | `SWI` / `SWI_PHASE` | word `SWI` or `SWAN`; `SWI_PHASE` if word `PHA` or `PHASE`, or an `ImageType` value equals `P` or `PHASE` (so `PRIMARY` does not match) |
        | 7 | `FLAIR` | contains `FLAIR` |
        | 8 | `GRE` (T2*) | word `T2*` or `T2STAR`; sequence `T2 STAR` |
        | 9 | `T1` / `T1_POST` | prefix `T1`; contains `MPRAGE`; word `BRAVO`, `SPGR`, `THRIVE`. `T1_POST` if word `POST` or `GAD`, contains `GADO` or `CONTRAST`, or the raw text contains `+C` |
        | 10 | `T2` | prefix `T2` |
        | 11 | `DWI` | word `DWI`, `DTI`, `TRACE`; contains `DIFFUSION` |
        | 12 | `MRA` | word `MRA`, `TOF` |
        | 13 | `MRV` | word `MRV` |
        | 14 | `GRE` | word `GRE` |
        | 15 | `PERFUSION` | contains `PERFUSION`; word `DSC`, `DCE`, `ASL`, `PCASL`, `PASL`, `CASL` |
        | — | `OTHER` | no match |

#### 10. `src/mri_dicom_pipeline/nifti.py` (Create)

*   **Type / Scope**: Python module, NIfTI pass-through (Q10) and sidecar
    filtering.
*   **Proposed Changes**:
    *   `validate_and_gzip(source, destination)` replaces
        `sanitized_nifti`. It keeps every structural check (header size,
        magic, dimensions, bit depth, voxel offset, the 16 MiB extension
        cap, minimum payload). It writes header, extension area, and payload
        **unchanged** into gzip (empty file name, `mtime=0`, level 6), and
        deletes `destination` on any failure.
    *   `TECH_FIELDS` and `clean_metadata`, moved unchanged.
    *   `check_companion(path)`: the existing finite-number check, moved
        from `organize_conversion`.

#### 11. `src/mri_dicom_pipeline/prepare.py` (Create)

*   **Type / Scope**: Python module, preparation (Q3, Q15, Q19, Q20, R18).
*   **Proposed Changes**:
    *   **Constants.** `DICOM_TAGS` adds `ImageType`, read in the same
        header-only read. `SKIP_EXT`, moved. `MR_IMAGE_SOP_CLASSES` is the
        four UIDs in Q19. `FREE_SPACE_RESERVE` is the smaller of 5% of the
        filesystem or 50 GiB (Q15).
    *   **`HoldError(ValueError)`.** Produces outcome `held` with a blocking
        `prepare:<path>` issue. The ZIP stays where it is and no session
        entry changes.
    *   **Candidates** stay today's tuple `(source, sid, date, label,
        fingerprint)`. The `--audit` re-hash runs in the planner (below), so
        there is no separate audit candidate.
    *   **`discover(cfg)`**, moved.
    *   **`check_free_space(archive, stage_root, dicom_root)`.** Sums the
        `file_size` of every member that is not a directory. Raises
        `ValueError` ("not enough free space: need N bytes plus reserve,
        have M") when the total plus `FREE_SPACE_RESERVE` exceeds
        `shutil.disk_usage(stage_root).free`. If `dicom_root` is on a
        different device, it checks that root too.
    *   **`extract_archive(source, stage, stop, accept_multi_study)`.**
        Keeps today's behavior, plus:
        *   `prepare_one` calls `check_free_space` just before it, so nothing
            is written when the ZIP would not fit.
        *   It keeps a `sop_uid -> (sha256, size)` map. Each object is still
            hashed while it is written. A byte-identical repeat is then
            deleted again, left out of the inventory, and counted in
            `repeated_identical`; because names come from the inventory
            length, numbering stays continuous. A repeat with different bytes
            raises `HoldError`. A missing SOP Instance UID raises
            `ValueError`.
        *   Series rows gain `sop_classes` (sorted list) and `required`
            (any class in `MR_IMAGE_SOP_CLASSES`). Classification passes
            `ImageType`.
        *   More than one StudyInstanceUID without `accept_multi_study`
            raises `HoldError`.
        *   `content_sha256` is computed over unique objects.
    *   **`move_archive(source, destination, cfg, fingerprint)`**, moved
        unchanged.
    *   **`prepare_one(candidate, previous, cfg, stop)`.** Order of
        operations:
        1.  Hash the ZIP.
        2.  Canonical-change rule (Q3): if the source is the previous
            canonical archive and the digest differs, raise `HoldError`
            ("accepted canonical ZIP changed"). If the digest matches but
            the signature changed, emit a "refresh" event. It updates the
            session's `archive_signature` and the source record's
            `signature` together, so the ZIP is not hashed again on the next
            run.
        3.  Adoption: if the destination holds a receipt that state does not
            record, apply `receipts.adoptable` with the digest. On a pass,
            emit an `adopted` event without extracting. On a mismatch, raise
            `HoldError` ("extraction on disk does not match this ZIP").
        4.  Duplicate and conflict logic as today, with the `replacement`
            branch removed. `--force-reextract` re-extracts only when the
            digest matches.
        5.  Move the ZIP (no intent; Q12). Write the `publish` intent, then
            publish. Receipts carry `run_id`. The event carries its intent
            ID.
    *   **Outcomes.** Each outcome sets the event fields, the counter, the
        source record, and the caching rule:

        | Outcome | Counted as | Source record | Session entry | Issue |
        |---|---|---|---|---|
        | `prepared` | completed | `outcome: prepared`, signature | New entry | Clears `prepare:<path>` |
        | `adopted` | completed | `outcome: prepared`, signature | Rebuilt by `receipts.prepared_entry` from the published inventory | Clears `prepare:<path>` |
        | `duplicate` | duplicate | `outcome: duplicate`, signature | Unchanged | Clears `prepare:<path>` |
        | `refresh` | cached | Signature updated | `archive_signature` updated | None |
        | `held` | held | `outcome: held`, signature; not hashed again until the signature changes | Unchanged; no entry is created for a new key | Blocking `prepare:<path>` |
        | `failed` | failed | `outcome: failed`, signature; retried when the signature changes or with `--retry-failed`, as today | Unchanged for an existing session; a new key gets a `failed` entry, as today | Blocking `prepare:<path>` |

    *   **`plan_prepare(cfg, state, ids, pairs, logger)`.** Today's rules,
        plus:
        *   The `--session` filter (`convert` has the same filter).
        *   The moved-project refusal (Q23) runs in `runner.run` for both
            commands, before reconciliation and planning.
        *   A ready session whose canonical archive is missing from the
            discovered paths gets a nonblocking `missing:<key>` issue. The
            issue clears when the archive is listed again.
        *   With `--audit`, each cached canonical ZIP is hashed during
            planning, so a dry run hashes it too and writes nothing (P7). A
            mismatch holds the ZIP; a match clears an earlier audit hold.
        *   An unchanged ZIP whose source record says `held` is not hashed
            again (its issue stays open), unless `--accept-multi-study`
            names its session.
        *   `accepted_multi_study` in the entry is true when the published
            inventory holds more than one study, which only an explicit
            acceptance allows.

#### 12. `src/mri_dicom_pipeline/convert.py` (Create)

*   **Type / Scope**: Python module, conversion (Q6, Q7, Q8, P8, P10).
*   **Proposed Changes**:
    *   **`redact_line(text) -> str`.** Today's patterns and the 2,000-char
        truncation, applied per line.
    *   **`run_converter(command, log_path, env, stop, timeout) ->
        (returncode, reason)`.** `Popen` with `stdout=PIPE`,
        `stderr=STDOUT`, and on POSIX `start_new_session=True`. A reader
        thread decodes each line (`errors="replace"`), redacts it, and
        writes it to `converter.log`, so raw output never reaches disk.
        Stopping on stop or timeout: on POSIX, `SIGTERM` to the process
        group, wait 10 s (today's grace period), then `SIGKILL` to the group;
        on Windows, terminate, wait 10 s, kill. On Windows, processes the
        converter started are not stopped (Q2). `reason` is `stopped` when
        the stop flag was set or the return code is `-SIGTERM` or
        `-SIGKILL`, and `timeout` or `exit` otherwise.
    *   **`organize_conversion(...)`.** As today, but using
        `nifti.validate_and_gzip` and `nifti.check_companion`. It returns
        `(volumes, files, errors, converted_keys, converter_versions)`:
        today's three values, the `source_series_key` of each valid volume,
        and the set of sidecar `ConversionSoftwareVersion` values.
    *   The redacted diagnostic in `event["detail"]` and its copy into
        `latest_convert.log` stay as today. The text is already redacted line
        by line before it reaches disk.
    *   **`convert_one(entry, cfg, stop)`:**
        1.  Read the extraction inventory and call
            `receipts.required_series`.
        2.  Run the converter. If `reason` is `stopped`, keep the stage,
            record `interrupted`, and return; nothing is organized, gzipped,
            hashed, or published (Q12). Otherwise organize the output and
            compute `receipts.coverage`. Each missing required key adds the
            error "Missing required series: …".
        3.  Build `provenance`: package version, `run_id`, the resolved
            converter path, the argument list after the executable, and the
            sorted set of sidecar `ConversionSoftwareVersion` values. It is
            written into the receipt together with `coverage`, `n_volumes`,
            and `run_id`.
        4.  **Explicit reconversion over last-good:** when the destination
            is the session's recorded complete folder, list its tree. If any
            file is outside its inventory and the control files, fail with
            "files not in inventory: …" before the swap.
        5.  Write the `publish` intent, then publish.
        6.  **State:** success sets `conversion` to the new record and
            clears `last_attempt`. A failure, partial, or interrupt with an
            existing complete `conversion` keeps that record and sets
            `last_attempt = {status, output, run_id, reason}`. Without a
            complete `conversion`, it behaves as today.
    *   **`plan_convert(cfg, state, ids, pairs, logger)`.** "Complete" is
        defined in Q7.
        *   **Complete session**, not forced: a valid receipt means cached;
            an invalid one adds the blocking issue `output:<key>` ("output
            modified; revalidate with --audit --session or reconvert
            explicitly") and no task.
        *   **Complete session, `--audit` with `--session`:** if the folder
            fails only the mtime check, every inventory file hashes to its
            recorded value, and the folder holds no files outside its
            inventory and control files, record fresh `receipt_info` and
            clear `output:<key>` (Q7 revalidation). Otherwise the issue
            stays.
        *   **Complete session, `--force-reconvert`:** selected, subject to
            `--session`.
        *   **Conversion record under a different NIfTI root:** treated as no
            record at this root (Q7).
        *   **No conversion record but a folder with a receipt exists** at
            the canonical path: read its receipt (one small read, only in this
            case) and apply `receipts.adoptable`. Adopt on a pass; otherwise
            add the blocking issue `output:<key>` ("unreferenced output
            folder"). It is never replaced automatically.
        *   **Blocked set:** prepare issues for the session that the issue
            table marks blocking.
        *   Failed, partial, and interrupted rules without a complete record
            stay as today. `--retry-failed` never selects a complete
            session.

#### 13. `src/mri_dicom_pipeline/reports.py` (Create)

*   **Type / Scope**: Python module, logs, CSV reports, history.
*   **Proposed Changes**:
    *   `setup_log`, `write_csv`, and `problem()`, moved.
    *   **Issue table.** An issue is *blocking* when it counts toward the
        unresolved total that makes a run exit with code 2. A nonblocking
        issue is reported but does not change the exit code.
        `NONBLOCKING_PREFIXES = ("date:", "missing:")` and
        `unresolved(state) -> int`.

        | Key | Raised by | Blocking | Cleared by |
        |---|---|---|---|
        | `prepare:<ZIP path>` | Preparation failures and holds (Q3, Q15, Q20) | Yes; it also blocks conversion of that session | The next accepted preparation of that path, or the path disappearing (today's stale-issue rule) |
        | `date:<key>` | Header dates that differ from the file name (today) | No | As today |
        | `missing:<key>` | A ready session whose canonical ZIP is not listed (R18) | No | The ZIP being listed again |
        | `convert:<key>` | Conversion failures, partials, and interrupts (today's key) | Yes | The next successful conversion of that session |
        | `output:<key>` | A complete folder that fails its receipt check, or an unreferenced folder that fails adoption (Q7) | Yes | Revalidation with `convert --audit --session KEY`, an explicit reconversion, or the folder passing its check on a later run |
        | `recover:<intent id>` | A reconciliation case that touches nothing (Q12) | Yes | The intent file being gone when the next run reconciles (the runbook says how to resolve it) |

    *   `reports(state, cfg, pairs)`: `session_status.csv` adds
        `required_series`, `converted_series`, and `excused_series`
        (semicolon-separated series keys), plus `last_attempt_status` and
        `last_attempt_folder`.
        *   `prepare_status` can also be `held`, taken from a blocking
            `prepare:` issue on a ZIP whose name maps to the session key.
        *   `conversion_status` can also be `output_modified`, taken from an
            open `output:<key>` issue.
        *   "Converted with a failed last attempt" shows as `converted`
            plus a non-empty `last_attempt_status`.
        *   Every report and history write goes through
            `fsutil.write_text_or_log`. A file held open (for example by
            Excel) is logged and retried at the next report write, and the
            run continues (Q2).
    *   `append_history(cfg, row)`: columns are today's plus `reconciled`.
        Outcomes: `ok`, `issues`, `interrupted` (Ctrl+C, exit 130),
        `terminated` (SIGTERM, exit 143), and `aborted` (normal
        finalization not reached).

#### 14. `src/mri_dicom_pipeline/runner.py` (Create)

*   **Type / Scope**: Python module, command orchestration.
*   **Proposed Changes**:
    *   `main(phase, argv=None)` (the default stays, as today): install the
        stop handler, configure, load the roster, then (except in a dry run)
        enter `RootLocks`. It loads `State`, reconciles (report-only in a
        dry run), plans, applies `--limit`, and runs workers. `run()` from
        `pipeline.py` moves here.
        *   The `try`/`finally` around everything after locking writes a
            history row on every exit path, with outcome `aborted` when
            normal finalization was not reached.
        *   `LockBusy` and `StateError` return 2.
    *   **Signals (Q12):** one handler for SIGTERM and SIGINT, installed at
        the start of `main` for the whole run. The first signal sets the stop
        event and records which signal arrived; later signals are ignored
        until the run ends. Workers see the stop event; the coordinator
        drains finished events, then attempts the final flush and the report
        rewrite. A failure in either is logged and does not change the exit
        code. The run returns 143 after SIGTERM and 130 after Ctrl+C,
        including a signal that arrives during planning.
    *   `accept(event)`: `state.checkpoint(event)`, then
        `recovery.clear_intents(cfg.reports, event.get("intents", []))`, then
        today's counting and logging.
    *   `process_subject`, moved. It receives `cfg` (which carries `run_id`
        and the accepted multi-study set).

#### 15. `src/mri_dicom_pipeline/cli.py` (Modify)

*   **Type / Scope**: Installed CLI entry point.
*   **Proposed Changes**: Commands stay `prepare` and `convert`. Both
    dispatch to `runner.main` (retargeted from `pipeline` in step 0).

#### 16. `src/mri_dicom_pipeline/__init__.py` (Modify)

*   **Type / Scope**: Package metadata.
*   **Proposed Changes**: `__version__ = "0.2.0"`, the minor bump for the
    schema change (Q17 versioning). It lands in step 3, with the schema
    change, together with `pyproject.toml`'s `version`.

#### 17. `Scripts/unzip_and_inventory_dicoms.py` and `Scripts/convert_dicoms_to_nifti.py` (Modify)

*   **Type / Scope**: Source-checkout launchers.
*   **Proposed Changes**: Import `main` from `mri_dicom_pipeline.runner`
    instead of `.pipeline`. No other change.

#### 18. `pyproject.toml` (Modify)

*   **Type / Scope**: Build configuration.
*   **Proposed Changes**:
    *   `requires-python = ">=3.11"` (step 2). `version = "0.2.0"` lands in
        step 3, with the schema change and `__init__.__version__`.
    *   `build-system.requires = ["setuptools>=77", "wheel"]`.
    *   `license = "Apache-2.0"` and
        `license-files = ["LICENSE", "NOTICE"]`.
    *   Classifiers: remove Python 3.10 and `OS Independent`; add Python
        3.13 and 3.14, `Operating System :: POSIX :: Linux`, and
        `Operating System :: Microsoft :: Windows`.
    *   The `dev` extra raises its floor to `twine>=6.1,<7`. setuptools 77
        writes Metadata-Version 2.4, which twine 5.1.1 rejects (checked
        2026-11-05).
    *   Dependencies unchanged.

#### 19. `LICENSE` and `NOTICE` (Create)

*   **Type / Scope**: Legal files.
*   **Proposed Changes**: `LICENSE` is the unmodified Apache License 2.0
    text. `NOTICE` reads "mriDICOMpipeline / Copyright 2026 Levi Davis". The
    copyright holder is taken from `git config user.name`; if an employer
    or institution owns the code, it replaces the name here before the
    first release.

#### 20. `.github/workflows/ci.yml` (Modify)

*   **Type / Scope**: CI.
*   **Proposed Changes**:
    *   **`test` job:** OS `[ubuntu-latest, windows-latest]` × Python
        `["3.11", "3.12", "3.13", "3.14"]` (today: 3.10 and 3.12). The
        install step stays `python -m pip install -e ".[test]"`.
    *   **New `real-converter` job:** OS `[ubuntu-latest, windows-latest]`
        × `dcm2niix` `[OLDEST, NEWEST]`, Python 3.12. It downloads the
        release archive for the OS from
        `github.com/rordenlab/dcm2niix/releases`. Each matrix entry pins the
        tag, the asset name, and the archive's SHA-256. The job checks the
        checksum and the reported version, sets `MRI_PIPELINE_DCM2NIIX`, and
        runs `pytest tests/test_real_converter.py`.
        *   `NEWEST` is the newest release on the day this step is
            implemented: v1.0.20261029 (implemented 2026-11-05).
        *   `OLDEST` is the version the target cluster's module provides
            (Q2); if that cannot be determined, `OLDEST = NEWEST`, and the
            README lists a single version. It is not known yet, so the job
            runs v1.0.20261029 only.
    *   **`package` job:** unchanged.

#### 21. `tests/support.py` (Create)

*   **Type / Scope**: Shared test helpers (moved out of `test_pipeline.py`,
    which `test_project_configuration.py` imports today).
*   **Proposed Changes**:
    *   Step 0 moves `dicom_bytes`, `archive`, `call`, and `state` here
        unchanged.
    *   `dicom_bytes(...)` gains `sop_class`, `sop_uid`, `series_uid`,
        `study_uid`, `description`, and slice geometry (`ImagePositionPatient`,
        `ImageOrientationPatient`, `PixelSpacing`, `SliceThickness`), so the
        real converter accepts the output (step 1).
    *   `fake_converter_script(...)`: the fake converter reads its input
        DICOMs with `pydicom` and names outputs `series_<SeriesInstanceUID>_…`.
        This replaces today's hard-coded series key, which would make every
        existing conversion test `partial` under strict coverage. Switches:
        skip one series, set the exit code, print a sentinel line then sleep,
        spawn a child process, write a header sentinel.
    *   `run_with_crash(root, phase, point, *args)` (step 5): runs the
        command in a child Python process that calls `os._exit(137)` at a
        named crash point and returns its exit code. The points
        (`CRASH_POINTS`) match a `Path.rename` by its source or target name
        (`zip-moved`, `staged`, `moved-aside`, `swapped`, `rolling-back`), an
        `os.replace` onto `index.json.bak` (`index-rotated`), the first
        `recovery.clear_intents` call (`journaled`), or a forced cross-device
        copy killed after two files (`copying`). When a point is not reached,
        it prints the child's output.

#### 22. `tests/conftest.py` (Create)

*   **Type / Scope**: pytest fixtures.
*   **Proposed Changes**:
    *   `project` (today's fixture).
    *   `converter` (a fake converter built with `fake_converter_script`).
    *   `real_dcm2niix`: skipped unless `MRI_PIPELINE_DCM2NIIX` is set.

#### 23. `tests/test_pipeline.py` (Modify)

*   **Type / Scope**: Existing integration tests.
*   **Proposed Changes**:
    *   Helpers move to `support.py`, and patch targets are retargeted
        (`fsutil.sha256`, `prepare.extract_archive`, `prepare.prepare_one`,
        `convert.convert_one`).
    *   `test_conversion_privacy_companions_and_fast_rerun` becomes
        `test_conversion_companions_and_fast_rerun`. The NIfTI-header
        sentinel assertion is dropped (moved to `test_conversion.py`); the
        JSON and log assertions stay.
    *   `test_deleted_dicom_and_nifti_detected` becomes
        `test_deleted_dicom_reextracted_and_deleted_nifti_flagged`: deleting
        a NIfTI now gives exit 2 and an `output:` issue, with no
        reconversion.
    *   `test_lock_and_pending_journal_recovery` targets `locks` and
        `state`.
    *   `test_sequence_categories_and_truncated_nifti` targets `classify`
        and `nifti.validate_and_gzip`.
    *   `test_path_traversal_member_cannot_escape`: the extra member is a
        byte-identical repeat of the first object, so it now expects
        `n_dicom == 1` and `repeated_identical == 1` (Q20). The escape
        assertion stays.
    *   `test_output_root_override_does_not_reuse_other_root` stays as it is
        and must keep passing (Q7).
    *   `test_1000_cached_sessions_do_not_read_scan_contents` stays here,
        rebuilt on schema 3. It remains Goal 5's evidence.
    *   `test_dry_run_writes_nothing_and_bad_names_held` (step 4): after the
        non-dry run, `DicomExtracted/` may hold its lock file (Q13), and
        nothing else.

#### 24. `tests/test_project_configuration.py` (Modify)

*   **Type / Scope**: Existing configuration tests.
*   **Proposed Changes**:
    *   Import helpers from `support`.
    *   `test_old_state_rejected_without_writes` is parametrized over
        versions 1 and 2. Both expect the "Unsupported state version"
        message and an unchanged file.
    *   `test_publish_rolls_back_if_swap_fails` passes the swap names to
        `fsutil.publish_tree`.

#### 25. `tests/test_identity.py` (Create)

*   **Type / Scope**: Unit tests (T01, T02).
*   **Proposed Changes**:
    *   `test_case_only_id_collision_ignored_with_warning`
    *   `test_reserved_device_names_rejected` (parametrized: `CON`, `nul`,
        `Com1`, `LPT9`; both roster rows and ZIP names)
    *   `test_session_key_grammar` (`_1` and `_02` rejected; `_2` and `_10`
        accepted)

#### 26. `tests/test_inputs.py` (Create)

*   **Type / Scope**: Integration tests (T03, T05, T06, T09; R05, R06, R07,
    R12, R18; P3, P7).
*   **Proposed Changes**:
    *   **Uploads and space:**
        `test_truncated_upload_fails_then_prepares_when_complete` (Q4),
        `test_free_space_check_refuses_extraction_and_keeps_zip` (Q15;
        patches `shutil.disk_usage`),
        `test_legacy_import_moves_zip_and_never_deletes_it` (CUJ 6),
        `test_empty_state_adopts_matching_extraction` (D6),
        `test_moved_project_refused`,
        `test_session_filter_limits_prepare`.
    *   **Immutable canonical ZIPs:**
        `test_changed_canonical_zip_is_held_and_outputs_unchanged` (P3),
        `test_restored_canonical_zip_accepted_without_reextract`,
        `test_force_reextract_refuses_changed_bytes`,
        `test_canonical_conflict_blocks_conversion`,
        `test_audit_rehashes_canonical_zip`,
        `test_missing_canonical_zip_nonblocking_issue`.
    *   **SOP classes, repeats, studies:**
        `test_non_image_series_extracted_and_excused`,
        `test_repeated_identical_instances_written_once`,
        `test_conflicting_repeated_instance_held`,
        `test_multi_study_held_until_accepted`.
    *   **Dry run:** `test_dry_run_audit_hashes_but_writes_nothing` (P7: the
        documented behavior and the help text).

#### 27. `tests/test_conversion.py` (Create)

*   **Type / Scope**: Integration tests (T08, T11–T14, T18; P1, P4, P8,
    P10).
*   **Proposed Changes**:
    *   **Coverage and reconversion:**
        `test_missing_required_series_is_partial` (P1),
        `test_complete_session_not_reconverted_when_folder_modified` (P10:
        an added file stays, exit 2),
        `test_force_reconvert_refuses_folder_with_extra_files`,
        `test_failed_reconversion_keeps_last_good` (P4),
        `test_unreferenced_complete_folder_adopted_not_replaced`,
        `test_restored_folder_revalidated_by_audit_session`.
        (`test_adoption_refused_when_volume_deleted` landed in
        `test_recovery.py` in step 5, with the adoption rule.)
    *   **Output bytes and privacy:**
        `test_published_nifti_is_byte_identical_to_converter_output` (Q10),
        `test_header_text_passes_through_and_sidecar_stays_filtered` (T14
        channels),
        `test_raw_converter_output_never_written` (P8: the log is read
        while the fake converter sleeps after printing a sentinel).
    *   **Provenance and labels:**
        `test_provenance_recorded_in_receipt_and_state` (Q8),
        `test_classifier_revision_2_table` (parametrized with the Q11 table
        and the existing cases),
        `test_prepare_and_convert_labels_agree`.
    *   **Selection and reports:** `test_session_filter_limits_work`,
        `test_status_columns_and_last_attempt_reported` (T18),
        `test_report_file_held_open_does_not_stop_run` (Q2; patches
        `os.replace` to raise `PermissionError` for `session_status.csv`).

#### 28. `tests/test_recovery.py` (Create)

*   **Type / Scope**: Crash-injection and process-control tests (T07–T09;
    P5, P11).
*   **Proposed Changes**:
    *   **`test_crash_at_boundary_reconciles`**, parametrized over the crash
        matrix below. Each case runs the command through
        `support.run_with_crash`, reruns it normally, and asserts: no
        `.incoming_*` or `.previous_*` left; every session at its prior valid
        state or its new result; no input lost.

        | Crash point | Command | Expected after the rerun |
        |---|---|---|
        | After the ZIP rename, before the publish intent | `prepare` | Canonical ZIP prepared as new work |
        | After stage→incoming, before destination→previous | both | `.incoming_*` removed; work redone (first publish) or old folder kept |
        | During a cross-device copy into `.incoming_*` (crash point `copying`) | both | `.incoming_*` removed; work redone; never `converted` with missing volumes |
        | After destination→previous, before incoming→destination | both | Rolled back to the old folder |
        | After incoming→destination, before the event | both | Adopted after the stat check |
        | After the event's journal write, before the intent is cleared | both | Intent cleared; swap leftovers removed |
        | After `index.json`→`.bak`, before temporary→`index.json` | both | `.bak` loaded silently; no rollback |
    *   **State and reconciliation:**
        `test_crash_during_reconciliation_is_recoverable`,
        `test_partial_journal_deletion_does_not_roll_back` (P11; it forces
        newest-first listing order, because NTFS's alphabetical order hides
        the defect),
        `test_corrupt_index_recovers_from_backup` (moved aside, history
        note, no persistent issue),
        `test_both_index_files_corrupt_stops_run`,
        `test_dry_run_with_corrupt_index_writes_nothing`,
        `test_crash_during_reconciliation_is_recoverable`,
        `test_adoption_refused_when_volume_deleted`,
        `test_dry_run_reports_reconciliation_without_acting`,
        `test_reconcile_skips_other_commands_intents`.
    *   **Signals:** `test_stop_signal_during_conversion` (step 5, all
        platforms; SIGTERM and Ctrl+C raised in-process with
        `_thread.interrupt_main`): exit 143 or 130, the session is
        `interrupted`, the stage is kept, nothing is published, and the
        history outcome is `terminated` or `interrupted`. Step 7 adds
        `test_sigterm_stops_converter_group_and_exits_143` (skipped on
        Windows): a real SIGTERM, and the fake converter's child process is
        gone.
    *   **Other exits and faults:**
        `test_history_row_written_on_early_abort`,
        `test_enospc_during_publish_keeps_old_output` (a simulated `OSError`
        with `ENOSPC` on rename or copy).

#### 29. `tests/test_locks.py` (Create)

*   **Type / Scope**: Multi-process lock tests (T10, local part).
*   **Proposed Changes**:
    *   `test_shared_report_root_excludes_different_projects` (a child
        process holds the locks)
    *   `test_lock_failure_names_holder` (and "unknown" when the owner file
        is missing)
    *   `test_dry_run_takes_no_lock`

#### 30. `tests/test_real_converter.py` (Create)

*   **Type / Scope**: Real `dcm2niix` smoke tests (Q9; P1 and Q19
    hypotheses). Runs only when `MRI_PIPELINE_DCM2NIIX` is set.
*   **Proposed Changes**: Step 1 writes each test against `dcm2niix`'s own
    behavior, and step 7 adds the session-level assertions in parentheses.
    *   `test_real_converter_names_outputs_by_series_uid` (output keys
        equal the inventory keys)
    *   `test_real_converter_with_non_image_object_converts`: a CSA
        Non-Image object is present, and the exit code is recorded. Step 7
        adds: the session is `converted` with that series excused. A
        nonzero exit reopens Q19.
    *   `test_real_converter_unconvertible_series_makes_session_partial`:
        the observed exit code is logged as evidence for P1. Step 7 adds:
        the session is `partial`.
    *   `test_real_converter_output_byte_identical`: also asserts that the
        output NIfTI files carry no header extensions (invariant 8).

#### 31. `README.md` (Modify)

*   **Type / Scope**: Operator documentation (Goal 7, phase 1 part; the
    full contract rewrite is phase 4).
*   **Proposed Changes**: Update the following and remove the claims they
    contradict:
    *   Python 3.11+.
    *   Uploads: do not run `prepare` while copies into `Uploads/` are in
        progress, or copy under a temporary name and rename it; partial copies
        fail and are retried (Q4). ZIPs are moved, never modified or deleted.
    *   Archives are assumed to come from the lab's own scanner exports; the
        free-space check (Q14, Q15).
    *   Moving a project is not supported yet (Q23).
    *   Immutable canonical ZIPs and the conflict, multi-study, and repeat
        holds (with `--accept-multi-study`).
    *   Strict `converted` and excused non-image series.
    *   The integrity-only validation boundary: conversion correctness is
        `dcm2niix`'s responsibility (Q9).
    *   No automatic reconversion, `output modified`, `--session`, and the
        explicit reconversion refusal.
    *   Last-good versus last attempt.
    *   Header text pass-through, with the time of day in `descrip` (this
        replaces "header text/extensions are cleared").
    *   Dry run with `--audit` hashing; `prepare --audit` re-hashing ZIPs.
    *   Per-root locks with a pointer to the runbook's two-node check; the
        "State schema version 2 uses … `.mri_dicom_pipeline.lock`" sentence
        becomes schema 3 with per-root lock files.
    *   Exit codes 0, 2, 130, 143.
    *   Older state is refused, not migrated (Q22); rollback (NFR).
    *   Classifier revision 2 rules.
    *   License: Apache-2.0 (replacing "no license has been selected").
    *   The CI matrix and the real-converter jobs.
    *   `run_history.csv`: one row per locked invocation, including
        `terminated` and `aborted`, with a `reconciled` count (replacing "one
        compact row per completed/interrupted invocation").
    *   The `session_status.csv` field list, with the new columns.
    *   The `.state` contents: `index.json.bak` and `intents/`.
    *   Nonblocking issues: date mismatches and missing canonical ZIPs.
    *   Interrupted work is retried automatically, except an interrupted
        explicit reconversion.

#### 32. `docs/runbook.md` (Create)

*   **Type / Scope**: Operator procedures. Phase 1 contents; phase 4 extends
    it.
*   **Proposed Changes**: One section per procedure:
    *   First production run (CUJ 6). Keep a copy of every ZIP outside the
        project before the first run; `prepare` moves accepted ZIPs into
        `SubjectsSorted/` and never modifies or deletes them.
    *   The two-node lock check (T10 site evidence), only when runs start
        from more than one machine on shared storage, and how to record it.
    *   Resolving a canonical-ZIP conflict, a multi-study hold, an
        `output modified` issue (revalidate or reconvert), and a `recover:`
        issue.
    *   Explicit reconversion.
    *   What reconciliation does after a crash and what it reports.
    *   Recovery from a corrupt index; what happens with empty state and
        full roots (D6).
    *   Rolling back to 0.1.0.
    *   Manual cleanup of failed stages and `.pending/` attempts (Q15).
    *   Slurm: sending SIGTERM to the pipeline ahead of the time limit
        (`#SBATCH --signal=B:TERM@<seconds>`, with `exec` in the batch
        script so the signal reaches the pipeline).
    *   Quota exhaustion.
    *   Node-local staging limits.

#### Files not changed in phase 1

`src/mri_dicom_pipeline/__main__.py`, `Scripts/prepare_dicoms.sh`,
`Scripts/convert_dicoms.sh`, `Scripts/requirements.txt`,
`examples/sessions.csv`, `.gitignore` (lock, owner, and backup files all sit
under roots that are already ignored), `.gitattributes`, `MANIFEST.in`
(setuptools adds the license files to the sdist), `tests/check_distribution.py`
(its license checks wait for phase 4), and
the baseline files `docs/designs/mriDICOMpipeline-current-state-spec.md`,
`docs/designs/concepts_brief.md`, and
`docs/designs/current-source-manifest.json`. The baseline stays unchanged,
as baseline §21 requires.

### Phase 1 regression traceability (Goal 4)

| Problem | Regression test (fails at step 0, passes after phase 1) |
|---|---|
| P1 | `test_conversion.py::test_missing_required_series_is_partial` |
| P2 | Deliberately open (Q8); `test_provenance_recorded_in_receipt_and_state` covers the record |
| P3 | `test_inputs.py::test_changed_canonical_zip_is_held_and_outputs_unchanged` |
| P4 | `test_conversion.py::test_failed_reconversion_keeps_last_good` |
| P5 | `test_recovery.py::test_crash_at_boundary_reconciles`, `test_stop_signal_during_conversion` (step 5), `test_sigterm_stops_converter_group_and_exits_143` (step 7) |
| P6 | `test_locks.py::test_shared_report_root_excludes_different_projects` |
| P7 | `test_inputs.py::test_dry_run_audit_hashes_but_writes_nothing` (help-text assertion) |
| P8 | `test_conversion.py::test_raw_converter_output_never_written` |
| P9 | CI matrix and `real-converter` jobs green; `LICENSE` and `NOTICE` present (artifact checks in phase 4) |
| P10 | `test_conversion.py::test_complete_session_not_reconverted_when_folder_modified` |
| P11 | `test_recovery.py::test_partial_journal_deletion_does_not_roll_back` |
| Q9 integrity (brief priority 6, narrowed) | `test_conversion.py::test_published_nifti_is_byte_identical_to_converter_output`, `test_real_converter.py::test_real_converter_output_byte_identical` |

### Verification Plan

*   **Compilation/Build**: `python -m pip install -e ".[dev]"`, then
    `python -m build`. The existing CI `package` job keeps running
    `twine check` and `tests/check_distribution.py` unchanged.
*   **Unit and integration tests**: `python -m pytest` on Linux and Windows,
    Python 3.11–3.14 (CI `test` job). The SIGTERM test runs on Linux only.
*   **Real converter**: `MRI_PIPELINE_DCM2NIIX=<path> python -m pytest
    tests/test_real_converter.py` (CI `real-converter` job, both OSes, both
    versions).
*   **Static analysis & linting**: none is configured today, and phase 1
    adds none. Step 0's review checks for no logic change by comparing
    function bodies before and after the move.
*   **Test coverage requirement**: traceability rather than a percentage.
    Every phase 1 row in the behavior contract and every P-item in the
    traceability table has at least one named, passing test.
*   **Incrementality**: `test_1000_cached_sessions_do_not_read_scan_contents`
    asserts that a cached rerun reads no scan contents (Goal 5).
*   **Site checks (only when runs start from more than one machine on shared
    storage)**: the runbook's two-node lock check on that filesystem.
*   **Documentation**: a checklist that maps each changed README statement
    to its test, reviewed before the phase 1 merge (Goal 7).

--------------------------------------------------------------------------------

*Drafted with EGM*
