# mriDICOMpipeline Production Hardening: Behavior Contract and Phase 1 Plan

*   **Author:** Levi Davis
*   **Status:** Proposed
*   **Date:** 2026-11-04
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

Version 0.1.0 passes its synthetic tests. It does not yet state what it
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
    only. Each later phase gets its own design document, which must follow this
    contract.

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

Each claim below was first checked by reading `pipeline.py` during this
drafting session ("code trace"). Most were then reproduced against the
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
| P8 | Medium | **Unredacted converter output reaches disk.** The converter log is written raw while `dcm2niix` runs and is redacted only afterwards. A crash in between leaves the raw log in the staging directory. The redaction itself matches line patterns and can miss identifiers. | `convert_one`, `redact_diagnostic` | Code trace; same as baseline R08. |
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
| P1 (`dcm2niix` exit behavior) | Not run | `dcm2niix` is not installed here; covered by the phase 1 real-converter tests |
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
4.  **Phase 1 removes the most severe risks first.** As the brief requires,
    phase 1 addresses problems that can cause false success, image corruption,
    identity reassignment, input loss, unrecoverable state, or sensitive-data
    disclosure before any work on performance, packaging, or reporting. Which
    P-items and register items fall in phase 1 is decided in the Technical Plan
    (Q1).
    *Evidence:* each phase 1 item has a regression test that fails on the
    baseline and passes after phase 1.
5.  **Incremental cost does not regress.** An ordinary rerun over unchanged,
    completed sessions performs no archive hashing, no DICOM header reads, and
    no converter calls, as today.
    *Evidence:* `test_1000_cached_sessions_do_not_read_scan_contents` still
    passes after every phase. Numeric budgets (Q18) are set in the Technical
    Plan, not promised here.
6.  **Existing studies upgrade without loss.** A phase that changes the state
    schema ships a migration that first runs as a preview, keeps the schema-2
    index and all inputs and outputs, and either completes or refuses with the
    project unchanged.
    *Evidence:* a migration test on a synthetic schema-2 project, including an
    interrupted migration (baseline T17).
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
*   Using real participant data during design or review. Fixtures are synthetic
    or explicitly approved de-identified data. The private `sessions.csv`,
    `DicomUploads/`, and `DicomExtracted/` in the repository root are not read.

## Open Questions

The author answered all 23 product questions (Q1–Q23) on 2026-11-04; see the
[Decision Log](#decision-log). References below point to the baseline spec
(R = review register, T = stress-test group, G = release gate) or to the
problems above.

**Approved with the Technical Plan on 2026-11-04.** These items were proposed
while recording the author's decisions, and the author approved them with the
section:

*   Q15 numeric limit defaults (phase 2).
*   Q17 versioning, deprecation, security-report, and support proposals.
*   Q2: Python 3.11 and later.
*   Q3: an open canonical-ZIP conflict blocks conversion of that session.
*   Q4: `waiting for marker` is nonblocking; `--import-legacy` is exempt from
    markers.
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

*   **Given**: 1,000 converted sessions, and three new ZIPs in `Uploads/`,
    each with a `.zip.done` or `.zip.sha256` marker.
*   **When**: The operator runs `prepare`, then `convert`.
*   **Then**: Only the three new sessions are hashed, extracted, and
    converted. The other 1,000 cause no content reads and at most 20 metadata
    calls each. Both commands exit 0.

### CUJ 2: Slurm time limit during conversion

*   **Given**: `convert` running with four workers inside a Slurm job.
*   **When**: Slurm sends SIGTERM, then SIGKILL 30 s later.
*   **Then**: Within 20 s of SIGTERM, every converter process group is
    stopped, completed events are checkpointed, and reports are written; the
    exit code is 143. The next run reconciles open intents: it adopts any
    folder that was published but not yet recorded and retries interrupted
    sessions. No session reverts to an older state.

### CUJ 3: Corrected re-export for an existing session

*   **Given**: `S001/20260725` is converted.
*   **When**: An operator copies a corrected export over
    `SubjectsSorted/S001/S001_20260725.zip`, or drops it into `Uploads/`.
*   **Then**: `prepare` holds the session as a conflict. The extraction and
    the NIfTI folder stay unchanged. The operator uploads the export as
    `S001_20260725_2.zip` with a marker, and it becomes a separate session.

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

### CUJ 6: Upgrading a 0.1.0 project

*   **Given**: A schema-2 project.
*   **When**: The operator runs `migrate --dry-run`, then `migrate`.
*   **Then**: The preview lists every planned change. The schema-3 index is
    written, and `index.v2.json` is kept. Sessions that contain non-image
    series may show as `partial`, a known false result. They clear without
    reconversion through `prepare --session KEY --force-reextract`. Every
    other command refuses to run on a schema-2 project and points to
    `migrate`.

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
  ├─ take locks on every root the command touches + legacy project lock (Q13)   [non-dry]
  ├─ load index.json (fall back to .bak) ── replay journals with seq > loaded seq (Q21)
  ├─ refuse if schema 2 (→ migrate) or stored paths are outside the roots (→ relocate, Q23)
  ├─ reconcile open intents (Q12)                                              [non-dry]
  ├─ plan
  │    prepare: discover ZIPs ─ marker present? (Q4) ─ signature / cache ─ canonical
  │             changed? → hash; same digest → accept; different → hold (Q3)
  │    convert: ready sessions ─ complete folder? never auto (Q7) ─ receipt
  │             invalid → `output modified` issue
  ├─ subject workers (one thread per subject, as today)
  │    write intent ─ stage ─ hash / extract (dedupe, multi-study hold, Q20)
  │    ─ move ZIP + marker ─ publish ─ result event
  │    convert: converter in its own process group; output piped through
  │    redaction before it reaches disk (P8); validate + gzip, no byte
  │    changes (Q10); series coverage check (Q6/Q19)
  └─ coordinator (single state writer)
       journal(seq) ─ apply ─ clear intent ─ flush at most every 5 min (Q21)
       ─ reports ─ run_history row on every exit path ─ exit 0 / 2 / 130 / 143
```

### Behavior contract

Each guarantee holds on supported environments (Q2) from the phase listed.
Test IDs refer to the T-groups in baseline spec §18; the phase 1 test
functions are named in the Detailed Implementation Plan.

| # | Invariant | Guarantee | Stated exceptions | Evidence | Phase |
|---|---|---|---|---|---|
| 1 | Identity | Subject and session come only from authorized filenames. IDs that differ only by case, or that equal a Windows reserved name, are rejected. A ZIP with more than one study or conflicting repeated instances is held unless accepted. | Operators assign `_N` suffixes. `--allow-unlisted-dates` admits known IDs. `--accept-multi-study` overrides the study hold. | T01, T02, T05 | 1 |
| 2 | Input preservation | ZIP bytes are never modified. An accepted canonical ZIP is never replaced, and changed bytes are never extracted. Uploads move only after a marker and a hash check. | The pipeline cannot stop others from overwriting files. Detection relies on size and mtime unless `--audit` is used. Skipped non-DICOM members exist only in the ZIP. | T03, T05, T06 | 1 |
| 3 | No false completion | `converted` means the converter exited 0, every required series has at least one valid volume, there are no organization errors, and there are no unexpected files. | Series outside the MR image SOP classes are excused and listed. Whether the conversion itself is correct is up to `dcm2niix` (Q9). Migrated sessions can be false `partial`, never false `converted`. | T08, T11, T18 | 1 |
| 4 | Isolation | Deletion and publication stay inside the configured roots. Two commands that share a root cannot run at the same time on supported storage. Files people add are never deleted. | Storage whose locks only work within one node is unsupported. Processes that do not cooperate with the lock are out of scope. | T10 + site check | 1 |
| 5 | Recoverability | After an abrupt stop at any persistence step, the next run either returns to the last valid state or adopts the completed result. No session rolls back. A corrupt index is recovered from `.bak` plus journals. | If both index files are corrupt, the run stops and names them. A stage on a lost node is replanned. | T07, T09 | 1 |
| 6 | Incrementality | A cached rerun makes no content reads and at most 20 metadata calls per session. A complete session is never reconverted automatically. | `--audit`, `--force-*`, and `migrate` (one sidecar read per volume, once). | T06, T15 | 1 (budget measured at scale in 3) |
| 7 | Provenance | Each conversion records package version, run ID, converter path, argument list, converter version or versions, and algorithm revisions. Each preparation records archive hash and run ID. | No comparison and no "outdated" flag (Q8). The converter binary is not hashed. | T18 | 1 |
| 8 | Privacy honesty | The README lists every channel that carries identifiers or dates: paths, sidecar fields, NIfTI header text (time of day in `descrip`), original DICOM bytes in `DicomExtracted/` and the canonical ZIPs, hashes, and heuristically redacted logs. Unredacted converter output is never written to disk. | No de-identification claim. Redaction matches line patterns. | T14 | 1 (full README contract in 4) |
| 9 | Pipeline integrity (narrowed, Q9) | Each published NIfTI decompresses to exactly the converter's bytes. Companions are byte-identical. Required series are accounted for. | Orientation, slice order, spacing, dimensions, scaling, and voxel values are `dcm2niix`'s responsibility. Diffusion companions get the finite-number check only. | T11, T12 | 1 |
| 10 | Operational clarity | Distinct statuses for awaiting upload, waiting for marker, held, ready, converted, converted with a failed last attempt, partial (with reason), failed, interrupted, and output modified. Every locked invocation writes a history row. | Local time without offset is ambiguous for one hour per year (Q16). | T18 | 1 |
| — | Containment | Each archive is parsed in a child process with memory, CPU, and size caps, on Linux and Windows. | Parser bugs that stay inside the caps. | T04, T08 | 2 |
| — | Bounded retention | Failure material for resolved sessions is deleted after N days. | Unresolved sessions keep everything. | T09, T16 | 2 |

### State schema 3

Phase 1 introduces every schema change, including the fields later phases
need, so there is a single migration (Q1, Q22).

| Element | Change |
|---|---|
| Index top level | `version: 3`; `last_seq` (the highest journal sequence number included) |
| Journal file | Name `<seq:016x>_<random>.json`; body carries `seq` |
| `index.json.bak` | Previous index, kept at every flush |
| Session entry | Adds `accepted_multi_study` (bool) and `last_attempt` (`status`, `output`, `created_utc`, `run_id`, `reason`, `diagnostics_removed`). `conversion` becomes the last-good record and gains `provenance` and `coverage` (`required`, `converted`, `excused` series keys) |
| Receipt descriptors | Directory-mtime map keys are relative to the receipt's `root`; `root` stays absolute |
| Preparation inventory | Series rows add `sop_classes` and `required`. File rows add `series_key`, `ipp`, `iop`, `pixel_spacing`, `slice_thickness`, `rows`, `cols`, and `frames` (Q9 tag capture; nothing compares them in phase 1). The top level adds `repeated_identical`. `content_sha256` counts byte-identical repeats once |
| Receipt files | Preparation: adds `run_id`. Conversion: adds `run_id` and `provenance` |
| `Reports/.state/intents/` | New. One file per in-flight multi-step operation |
| `run_history.csv` | `migrate` rotates the old file to `run_history.v2.csv` because the columns change |

### Interface changes

| Change | Phase |
|---|---|
| New command `migrate [--dry-run]` | 1 |
| New repeatable option `--session SUBJECT/SESSION` (both commands) | 1 |
| New repeatable option `--accept-multi-study SUBJECT/SESSION` (`prepare`) | 1 |
| `--force-reconvert` refuses folders with files not in their inventory | 1 |
| `--dry-run` help text: "no writes or converter calls; hashes only with `--audit`" | 1 |
| `prepare --audit` also re-hashes canonical ZIPs | 1 |
| Upload marker files `<name>.zip.done` or `<name>.zip.sha256` required for new ZIPs | 1 |
| Exit code 143 after SIGTERM | 1 |
| `session_status.csv` adds `required_series`, `converted_series`, `excused_series`, `last_attempt_status`, `last_attempt_folder`; new status values listed in invariant 10 | 1 |
| `run_history.csv` gains outcome `aborted` and a `reconciled` count | 1 |
| Limit options for the Q15 defaults; `--retention-days N` | 2 |
| New command `relocate --from OLD --to NEW [--dry-run]` | 3 |

### Phase map

**Phase 1, safety core and schema 3:** Q3, Q4, Q6, Q7, Q8, Q9 (integrity
checks and geometry-tag capture), Q10, Q11, Q12 (including SIGTERM and Linux
process groups), Q13, Q19, Q20, Q21 (sequence numbers, `.bak`, 5-minute
flush), Q22, the Q2 ID rules, P7, P8, real-converter smoke tests, a CI matrix
on Python 3.11 and later, and `LICENSE`/`NOTICE`.

**Phase 2, containment and platform:** Q14 child-process parsing with caps,
Windows Job Objects for the parser and the converter, Q15 limits and
retention cleanup, Windows path-length holds, and files-in-use tests.

**Phase 3, scale and relocation:** Q18 (subject index, report cadence, T15
benchmarks at 1k/10k/20k sessions and on site storage) and Q23 `relocate`.

**Phase 4, release:** the README contract rewrite (Goal 7), `SECURITY.md`,
the support table, and the first release.

| Register item | Phase | | Test group | Phase |
|---|---|---|---|---|
| R01 Crash consistency | 1 | | T01 Identity grammar | 1 |
| R02 Locks on shared roots | 1 + site check | | T02 CSV robustness | 1 (very large rosters in 3) |
| R03 NIfTI correctness, sanitizer | 1 (narrowed to integrity by Q9/Q10) | | T03 Archive eligibility | 1 (encrypted members in 2) |
| R04 Cache provenance | 1 (record only, Q8) | | T04 Archive integrity, resources | 2 |
| R05 Canonical replacement | 1 | | T05 Duplicate/conflict matrix | 1 |
| R06 Partial transfer | 1 | | T06 Fast cache and audit | 1 |
| R07 Archive resource abuse | 2 | | T07 Crash injection | 1 |
| R08 Privacy channels | 1 (README contract in 4) | | T08 Process control | 1 (Windows in 2) |
| R09 Corruption, migration, relocation | 1 (`.bak`, `migrate`); 3 (`relocate`) | | T09 Filesystem faults | 1 (disk reserve in 2) |
| R10 Audit and dry-run | 1 | | T10 Concurrent operations | 1 + site check |
| R11 Termination, descendants | 1 (Linux); 2 (Windows) | | T11 Scientific geometry | 1 (narrowed to integrity; geometry tags captured for a later check) |
| R12 Non-image, multi-study, duplicates | 1 | | T12 Diffusion | 1 (narrowed to integrity) |
| R13 Diffusion and labels | 1 | | T13 Classification | 1 |
| R14 Paths, case, reserved names | 1 (case, reserved); 2 (path length). Symlink races inside project storage: out of scope, because storage is trusted (Q14) | | T14 Privacy | 1 |
| R15 Scale | 3 (flush policy in 1) | | T15 Scale | 3 |
| R16 Reporting completeness, retention | 1 (history row); 2 (retention) | | T16 Packaging/release | 1 (CI, license); 4 (release) |
| R17 Receipt trust | Accepted and documented (Q14) | | T17 Migration | 1 (`migrate`); 3 (`relocate`) |
| R18 Missing canonical archives | 1 | | T18 Observability | 1 |
| R19 Claimed versus observed support | 1 (CI, smoke tests); 4 (support table) | | | |
| R20 Release, license, security | 1 (license); 4 (security, release) | | | |

### Release gates

These restate the baseline spec's gates G01–G08 with objective pass
conditions; the author approved them at brief reconciliation. The first
release (phase 4) requires every gate to pass. A gate's evidence is produced
in the phase listed and must stay green afterwards.

| Gate | Pass condition | Evidence phase |
|---|---|---|
| G01 Product contract | The README support table matches the CI matrix plus the site checks that have passed. Every contract guarantee cites a test or a check. | 1, 4 |
| G02 Safety and recovery | The T07 crash matrix (`test_recovery.py`) is green. The runbook covers every reconciliation outcome. The two-node lock check has passed for each storage type listed as supported. | 1 + site |
| G03 Scientific integrity (narrowed, Q9) | The integrity tests (byte-identical output, series coverage, companions) and the real-converter smoke tests are green on the oldest and newest supported `dcm2niix`. The README states that conversion correctness is `dcm2niix`'s responsibility. | 1 |
| G04 Cache and provenance | The provenance and migration tests are green. The absence of an "outdated" flag is documented (Q8). | 1 |
| G05 Privacy and security | The T14 channel tests are green. The containment-cap tests are green (phase 2). `SECURITY.md` is published (phase 4). | 1, 2, 4 |
| G06 Performance | The per-session budget test is green (phase 1). 20,000 sessions take at most 5 min and use at most 2 GB, measured on site storage (phase 3). | 1, 3 |
| G07 Compatibility and release | Every CI job is green. The sdist and wheel install cleanly. `LICENSE` and `NOTICE` are in both artifacts. The versioning and deprecation policy is published. | 1, 4 |
| G08 Operations and documentation | The runbook covers the procedures listed in the Detailed Implementation Plan. The README checklist (Goal 7) is complete. | 1, 4 |

### Departures from the brief

The draft was compared with [`concepts_brief.md`](./concepts_brief.md) on
2026-11-04, and the author decided each difference.

| # | Brief | Resolution |
|---|---|---|
| 1 | Priority 6, "Scientific correctness of converted images" | **Departure, decided after reopening Q9:** the author briefly chose a runtime geometry cross-check, then reverted to pipeline integrity only because of implementation time. Conversion correctness is `dcm2niix`'s responsibility. Geometry tags are captured at preparation so a later phase can add the check (Future Improvements). |
| 2 | "First verify the descriptive baseline against the source"; a discrepancy report with reproducible tests | Resolved: the [Baseline verification](#baseline-verification) section reproduces P1–P8 and P10, and P11 under forced listing order. **Remaining departure:** the existing 30-case suite was not re-run, because `pytest` is broken in this environment, and the reproduction scripts are throwaway. The phase 1 regression tests become the lasting evidence. |
| 3 | "Impact/severity" for every finding | Resolved: severity column and scale added to the problem table. |
| 4 | "Objective release gates" | Resolved: [Release gates](#release-gates) added. |
| 5 | Converter, tool, and configuration changes missing from cache identity (a limitation to challenge) | **Departure, reconfirmed:** Q8 records provenance but reports nothing as outdated, so P2 stays open in practice. An "outdated" report is listed under Future Improvements. |
| A1 | Not raised: code structure | **Addition, approved:** `pipeline.py` is split into modules in phase 1, with a move-only step 0. |
| A2 | CI on "Python 3.10/3.12" | **Addition, approved:** Python 3.11 or later (3.10 is end of life) and `setuptools>=77`. |
| A3 | Not raised: new CLI surface | **Additions, approved:** `migrate`, `relocate` (phase 3), `--session`, `--accept-multi-study`, required upload markers (Q4 reconfirmed), exit code 143, and new report columns. |

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
*   How sessions already marked `converted` under the old rule are re-checked
    is decided with Q8 (provenance) and Q22 (migration).

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
    The list is a versioned constant and part of the conversion cache identity
    (Q8).
*   Existing schema-2 inventories do not record SOP class. Migrating them
    needs one header-only read (`stop_before_pixels`) per prepared object, or
    some other rule for unknown classes. Decided under Q22.
*   Non-image objects stay in the `dcm2niix` input folder, as today. The
    real-converter fixtures (T03, T11) must confirm that their presence does
    not make `dcm2niix` exit nonzero. This is a hypothesis to test.

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
*   The explicit accept is a repeatable `prepare` option naming one session key
    (proposed: `--accept-multi-study SUBJECT/SESSION`). It applies to that run
    only. The acceptance is recorded in the session's state entry, so later
    runs do not hold the session again. A conflict between repeated instances
    with different bytes cannot be accepted; the ZIP must be split or replaced
    by a new `_N` upload.
*   Writing byte-identical repeats only once changes `content_sha256` for new
    preparations of ZIPs that contain repeats. A schema-2 session with repeats
    and a new copy of the same ZIP would then compare as a conflict, not a
    duplicate. The migration (Q22) must recompute such digests from the stored
    file hashes; it needs no image reads.
*   Existing sessions: `study_count > 1` and repeated file hashes can be found
    from stored inventories without reading images. Repeated UIDs with
    different bytes cannot be found without a header read. Handled under Q22.

#### Q7 Reconversion of complete sessions: only on explicit command (decided 2026-11-04)

**Decision:** A session with a complete NIfTI folder is never reconverted
automatically. When its folder fails the cache check, the session is flagged
in `issues.csv` and left as it is. Converter or algorithm changes (Q8) are
reported only. A complete session is reconverted only when the operator runs
`--force-reconvert`.

**Consequences:**

*   This fixes P10. `plan_convert` no longer selects a `converted` session
    whose receipt check fails. It records an `output modified` issue instead.
    The issue counts as unresolved, so the run exits with code 2 until the
    operator restores the folder or reconverts it.
*   The same rule covers a complete folder that has disappeared or that
    `--audit` finds changed. Known failure mode: a damaged folder stays damaged
    until someone acts on `issues.csv`.
*   `--force-reconvert` must be able to target single sessions. Without that,
    the only route is reconverting the whole study. Proposed: a repeatable
    `--session SUBJECT/SESSION` filter for both commands, applied before
    `--limit`.
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
    retry behavior.
*   Sessions already `converted` under the old completion rule (Q6) are not
    reconverted by the upgrade. How their series coverage is checked and
    reported is decided under Q22.

#### Q8 Conversion provenance: record only, no comparison (decided 2026-11-04; reconfirmed at brief reconciliation)

**Decision:** Each conversion records its provenance in its receipt and in
the session's state entry. The pipeline never compares that record with the
current environment and never reports a session as "outdated".

**Recorded fields:** package version; run ID; resolved converter path; the
converter argument list; the converter version string or strings, taken from
the outputs' `ConversionSoftwareVersion` sidecar field; and revision numbers
for the classifier, the NIfTI sanitizer, and the MR SOP class list (Q19). No
field needs an extra file read or a converter call. The converter
executable's hash is not recorded.

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
*   Existing 0.1.0 outputs can have their record rebuilt from their sidecars
    plus the fixed 0.1.0 argument list, with revision 1 for every algorithm.
    Whether the migration does this is decided under Q22.

#### Q12 Crash recovery: automatic reconciliation at startup (decided 2026-11-04)

**Decision:** Before each multi-step filesystem operation, the worker writes
an intent file. The coordinator removes the file once the operation's result
is checkpointed in state. Every non-dry run, once it holds the lock and before
planning, finishes or rolls back each open intent. A dry run only reports what
it would do.

**Intent types and reconciliation rules:**

| Intent | Found on restart | Action |
|---|---|---|
| ZIP move (upload → canonical) | Only the canonical path exists | Clear the intent; the planner picks up the canonical ZIP as new work |
| | Only the upload path exists | Move never happened; clear the intent |
| | Both or neither exist | Touch nothing; raise an issue |
| Folder publish (stage → `.incoming_*` → destination, old → `.previous_*`) | Destination present, no swap folders | Done; clear the intent |
| | Destination missing, `.previous_*` present | Roll back: rename `.previous_*` to the destination; remove `.incoming_*` |
| | Destination missing, no `.previous_*`, `.incoming_*` has its receipt | Roll forward: rename `.incoming_*` to the destination |
| | Destination present and `.previous_*` present | Remove `.previous_*` only if state or the adopted receipt refers to the destination |
| Published folder with no state record | Receipt and inventory present; source digest matches the prepared content | Adopt: rebuild the receipt metadata and write the state record from the small JSON files, without reading images |
| | Digest mismatch or receipt missing | Touch nothing; raise an issue |
| Stage directory | Named by an open intent and not referenced by any issue | Remove it |

**Consequences:**

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
    test (T07), including a crash in the middle of reconciliation.
*   Retained failure stages and `.pending/` attempts are not removed here;
    their retention is decided in Q15.
*   A stage on node-local staging from a lost node no longer exists. Its
    intent is cleared and the session is replanned.
*   SIGTERM handling (P5) is separate: SIGTERM takes the same orderly stop
    path as Ctrl+C, and the converter runs in its own process group so the
    whole group can be stopped. Shutdown time limits are set in Q15.
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
*   The old project-root lock is also kept, so a 0.1.0 process and an
    upgraded one cannot run on the same project at the same time during the
    changeover.
*   Dry runs still take no lock, as today.
*   Known failure mode, accepted: on mounts where OS locks only work within
    one node (for example Lustre mounted with `localflock`) or do not work at
    all, two runs on different nodes both get every lock and nothing is
    protected. The contract therefore lists a storage type as supported only
    after a two-node lock check has passed at the site (T10). The runbook
    gives that check as a procedure.

#### Q4 Upload readiness: marker file required (decided 2026-11-04; reconfirmed at brief reconciliation)

**Decision:** A ZIP that is not yet in state is eligible for preparation only
when a marker file sits next to it: either `<name>.zip.done`, or
`<name>.zip.sha256`. When the `.sha256` file is present, the ZIP's SHA-256
must match it before acceptance.

**Consequences:**

*   This applies to new ZIPs in `Uploads/` and to new ZIPs placed directly in
    `SubjectsSorted/`. Canonical ZIPs that are already in state need no
    marker. `--import-legacy` is exempt, because it imports files already at
    rest; the README says so.
*   No extra read: `prepare_one` already hashes the whole ZIP, and the
    checksum comparison uses that hash. The `.sha256` file is read in the
    `sha256sum` format (`<hex>  <filename>`). If it names a file, the name
    must match.
*   A ZIP without a marker gets a nonblocking `waiting for marker` issue and
    is counted in the run summary. It does not make the run exit with code 2,
    so normal in-flight uploads are not reported as failures. Known failure
    mode: a forgotten marker leaves the ZIP waiting indefinitely, visible only
    in `issues.csv` and the summary.
*   A checksum mismatch is a blocking failure, and the ZIP stays in place.
*   On acceptance, the marker moves with the ZIP to `SubjectsSorted/`. A held
    ZIP keeps its marker beside it.
*   The size and mtime checks stay as a second safeguard against a marker
    created before the transfer finished.
*   Upgrade note: ZIPs left in `Uploads/` from before the upgrade (held,
    failed, or duplicate) need markers before they are reconsidered.

#### Q5 Partial overlap: settled by Q3 (recorded 2026-11-04)

**Decision:** Q3 already settles this. A new upload whose DICOM contents
differ from the prepared session in any way (subset, superset, or partial
overlap) is held as a conflict, as today. The prepared session is never
extended or merged. A re-export that adds missing series arrives as a new
`_N` session. Reopening this would mean reversing part of Q3.

#### Q14 Trust model: untrusted archives, trusted storage (decided 2026-11-04)

**Decision:** ZIP archives and their contents are untrusted input. Each
archive is extracted and parsed in a child process with memory and time caps
and the size limits below. Encrypted members and malformed ZIP structures are
rejected explicitly. The roster CSV, state, receipts, and inventories are
trusted. Anyone with write access to project storage can change what the
pipeline believes, and the contract says so.

**Consequences:**

*   `extract_archive` runs in a child Python process started per archive by
    the subject's worker thread. The child writes the stage and returns the
    inventory as JSON. The parent enforces a wall-clock limit and kills the
    child when it is exceeded. On Linux the child also sets memory
    (`RLIMIT_AS`) and CPU-time (`RLIMIT_CPU`) caps on itself before importing
    `pydicom`. On Windows only the wall-clock limit applies. The contract
    states this weaker containment.
*   `dcm2niix` parses the same untrusted objects, so on Linux it gets the
    same kind of memory cap, set by a small launcher before it starts. Python
    `preexec_fn` is not used, because it is unsafe with threads.
*   Side benefit: header parsing leaves the coordinator process, so CPU-heavy
    preparation is no longer limited by Python's global interpreter lock.
    This bears on baseline R15.
*   Cost: one process start and `pydicom` import per new archive, roughly
    under a second (estimate, to be measured in T15). Cached reruns start no
    child processes, so Goal 5 holds.
*   Ownership receipts are still trusted when present (R17), and this is
    documented, not changed.

#### Q15 Resource limits: proposed defaults (pending author confirmation)

These numbers are configurable defaults that do not change the architecture.
They are proposed here for review at the section gate.

| Limit | Proposed default | Applies to | Basis |
|---|---|---|---|
| Members per ZIP | 200,000 | prepare | Large sessions hold about 20k objects; 10× headroom |
| Uncompressed size of one accepted member | 4 GiB | prepare | Large enhanced multi-frame objects are 1–2 GiB |
| Total extracted bytes per ZIP | 200 GiB, and never more than free space on the DICOM root minus the reserve | prepare | Bounds a decompression bomb without a ratio rule; a per-member compression-ratio rule would reject legitimate blank images |
| Free-space reserve | The smaller of 5% of the filesystem or 50 GiB | prepare, convert | Keeps a shared filesystem from being filled |
| Child parser memory / CPU time (Linux) | 4 GiB / 2 h per archive | prepare | `pydicom` header reads with `stop_before_pixels` stay small |
| Converter memory cap (Linux) | 16 GiB per process; when Slurm reports the job's memory, `workers × cap` must fit inside it | convert | 4D series load in full |
| Converter timeout | 14,400 s (unchanged) | convert | Existing default |
| Converter log kept per session | 10 MiB (first and last 5 MiB) | convert | Bounds runaway diagnostics |
| Shutdown after SIGTERM or Ctrl+C | Stop converters, checkpoint, and write reports within 20 s | both | Slurm's default gap between SIGTERM and SIGKILL (`KillWait`) is 30 s |
| `run_history.csv` | Rotate at 10 MB with 3 backups, like `pipeline.log` | both | Unbounded today |

Known gap: `shutil.disk_usage` reports filesystem free space, not user or
group quotas. On quota-managed cluster storage the free-space checks can
pass while the quota is exhausted. Quota exhaustion is therefore handled as a
write failure that publishes nothing and leaves inputs in place (T09), and
the runbook tells operators to check quotas.

#### Q15 Retention: automatic by age, resolved sessions only (decided 2026-11-04)

**Decision:** Each non-dry `convert` run, after reconciliation (Q12) and
before planning, deletes retained failure material that is older than N days
(default 30), but only for sessions with no open `convert:` issue. Sessions
with an open issue keep every attempt.

**Consequences:**

*   "Failure material" means `.work/convert_*` stages and
    `Niftis/.pending/<id>/<session>/<attempt>/` folders that the pipeline
    created. Each deletion goes through the existing `owned()` check.
*   Age is measured from the creation time recorded in the attempt's state
    record, not from directory mtime, which other tools can change. When an
    attempt is removed, its state record is kept and marked
    `diagnostics removed`.
*   A new option `--retention-days N` sets N; `0` turns deletion off. A dry
    run lists what would be deleted. The run summary and `run_history.csv`
    report the bytes freed.
*   Known failure modes, accepted: diagnostics someone wanted past N days are
    lost once the issue is resolved. An unresolved session that is retried
    many times keeps accumulating attempts.
*   Node-local staging: a run cleans only the staging root it can see. Stages
    left on another node's local disk are outside its reach. The runbook says
    so.
*   Preparation keeps no failure stages today, so nothing changes there.

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
    time of day.
*   Mixed studies: outputs made by 0.1.0 stay sanitized and are not
    reconverted (Q7). Their provenance record carries sanitizer revision 1;
    new outputs carry revision 2, "pass-through".
*   `sanitized_nifti` becomes a validate-and-gzip function. The gzip settings
    (empty file name, `mtime=0`, level 6) stay, so output bytes remain
    repeatable for the same converter output.

#### Q9 Scientific validation scope: pipeline integrity only (decided 2026-11-04; reopened and reverted at brief reconciliation)

*History: first decided as "pipeline integrity only". While the draft was
being compared with the brief's priority 6, the author reopened Q9 and chose a
runtime geometry cross-check. The author then reverted to integrity only,
because implementing and testing the geometry comparison would take too
long. Geometry-tag capture at preparation was kept, so a later phase can add
the check.*

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
*   **Geometry tags are captured, not compared:** preparation stores
    `ImagePositionPatient`, `ImageOrientationPatient`, `PixelSpacing`, and
    `SliceThickness` per object in the preparation inventory. They come from
    the same header-only read, with no extra I/O, and stay out of the index
    (Q18 unchanged). Nothing compares them in phase 1. A runtime cross-check
    is listed under Future Improvements, and it could check every session
    prepared from phase 1 onward without re-preparing it.
*   Known failure mode, accepted: a `dcm2niix` regression or a
    scanner-specific conversion bug goes unnoticed by the package.
*   Smoke tests with the real converter on *synthetic* DICOM objects
    generated with `pydicom` are still needed. They check the pipeline's
    assumptions about `dcm2niix`: the `series_%j_…` output naming used by
    the Q6 series check, exit codes when a series is skipped (P1), and
    behavior with non-image objects in the input (Q19). They need no
    approved data.
*   **Diffusion (former half of Q11), settled by this decision:** `.bval` and
    `.bvec` files are copied byte for byte, and the existing finite-number
    check stays. No checks on shape, count, or orientation are added. A
    `DWI` label does not require companions. (R13 and T12 are narrowed to
    integrity.)

#### Q11 Classification: fix the rules, keep the layout (decided 2026-11-04)

**Decision:** The classifier is rewritten to match whole tokens, with
specific patterns ordered ahead of general ones. Preparation also passes
`ImageType`, so both stages assign the same label. The label set and the
`<scan_type>/` folder layout stay the same. The result is recorded as
classifier revision 2 (Q8) and is still documented as an unverified hint.

**Behavior observed in this session** (`classify()` run on synthetic
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
*   Known failure modes, accepted: mixed layouts within a study, because new
    conversions of a sequence can land in a different folder than old outputs
    of the same sequence (no reconversion, Q7); and naming conventions not in
    the table can still be mislabeled.

#### Q22 Schema-2 migration: metadata only, strict (decided 2026-11-04)

**Decision:** A new `migrate` command upgrades a schema-2 project without
reading any DICOM object. Series in old inventories get SOP class `unknown`
and count as required under the Q6 rule. Each old `converted` session's
series coverage is checked from its output JSON sidecars.

**Steps, in order:**

1.  Take all root locks (Q13). Replay pending schema-2 journals.
2.  Per session: mark every series's SOP class as `unknown` (required).
    Recompute `content_sha256` from the stored file hashes with byte-identical
    repeats counted once (Q20). Update the conversion record's
    `source_sha256` in the same step, so the digest change does not make the
    session look stale.
3.  Per old `converted` session: read the `.json` files listed in the output
    inventory, collect their `source_series_key` values, and compare them
    with the inventory series keys. If a key is missing, the session becomes
    `partial`, with an issue that lists the missing keys. The complete
    folder stays where it is (Q7).
4.  Sessions with `study_count > 1` get the Q20 multi-study issue. They can
    be cleared with `--accept-multi-study`.
5.  Rebuild provenance records (Q8) from the sidecars' converter version
    plus the fixed 0.1.0 argument list, with revision 1 for classifier and
    sanitizer.
6.  Write the schema-3 index atomically. Keep the schema-2 file as
    `.state/index.v2.json`.

**Consequences:**

*   `migrate --dry-run` prints the full plan: sessions that will become
    `partial`, multi-study sessions, and digest changes. It writes nothing.
    The schema-2 index is not changed until step 6, so an interrupted
    migration can simply be run again (Goal 6, T17).
*   Every other command refuses to run on a schema-2 project and points to
    `migrate`. This is today's version check, with a clearer message.
*   Known failure mode, accepted: false `partial` results for every old
    session that contains non-image objects, such as reports or Siemens CSA
    Non-Image.
*   Clearing a false `partial`: `prepare --session KEY --force-reextract`
    re-extracts from the unchanged canonical ZIP (Q3), which records real SOP
    classes. Whenever preparation refreshes the inventory of a session that
    already has a conversion, series coverage is re-evaluated from the
    existing output sidecars, without reconverting. Non-image series then
    stop counting, and the session returns to `converted` if nothing
    required is missing.
*   Cost: one small JSON read per published volume. No image or DICOM reads.

#### Q23 Project relocation: absolute paths plus a `relocate` command (decided 2026-11-04)

**Decision:** State keeps absolute paths. A new command,
`relocate --from OLD --to NEW` (repeatable, one pair per moved prefix),
rewrites stored path prefixes after a move. It runs as a preview first, then
applies. `prepare` and `convert` refuse to run when the index is not empty but
none of the stored paths for a root kind lie under the configured root. In
that case they print "project appears moved; run relocate".

**Consequences:**

*   `relocate` takes all root locks and refuses while intent files are open,
    so reconciliation (Q12) must have run first. It rewrites session paths,
    receipt and inventory paths, directory-mtime maps, source-record keys,
    and issue paths. It then checks every receipt at its new location using
    metadata only, as `fast_valid` does.
*   Sessions whose receipts fail at the new location are listed. This happens
    when the move tool did not preserve directory mtimes; `rsync -a`,
    `cp -a`, and `mv` preserve them. Without that list, the next `prepare`
    would re-extract those sessions and conversion would flag them (Q7). The
    report lets the operator fix the move first.
*   The refusal check is per root kind (uploads, DICOM, NIfTI, reports), so
    moving only one root is covered too. It compares strings in memory and
    adds no filesystem calls.
*   Copying a project is not relocation. A copy keeps absolute paths that
    point to the original, and the refusal check stops runs on the copy until
    `relocate` is run there.

#### Q18 Scale target: up to ~20,000 sessions per project (decided 2026-11-04)

**Decision:** A project of up to about 20,000 sessions must meet these
budgets.

| Budget | Value | How it is checked |
|---|---|---|
| Content opens per cached session (archive, DICOM, NIfTI, sidecar) | 0 | Counted in a synthetic test that instruments file opens (extends `test_1000_cached_sessions_do_not_read_scan_contents`) |
| Filesystem metadata calls per cached session, per command | ≤ 20 | Counted in the same test; today's estimate is about 5 for `prepare` and about 16 for `convert` |
| Cached rerun at 20,000 sessions | ≤ 5 min | Measured on the site's target storage (T15); a deployment check, not a CI promise |
| Coordinator peak memory at 20,000 sessions | ≤ 2 GB | Measured in T15 with synthetic metadata sessions |

**Consequences:**

*   Rewriting a single JSON index of about 60 MB every 10 seconds does not
    fit these budgets on network storage. State storage is decided in Q21.
*   `session_status.csv` (about 3 MB at 20,000 rows) is rewritten every 60
    seconds instead of every 15, plus once at the end of the run.
*   Scale defect found by code trace: `run` builds each subject's state copy
    by scanning every session key (`k.startswith(sid + "/")`). That costs
    subjects × sessions string checks on a large first run, about 400
    million at 20,000 single-session subjects. Sessions get indexed by subject
    once per run instead.
*   T15 runs at 1k, 10k, and 20k synthetic sessions, both "many subjects"
    and "few subjects with many sessions", locally in CI and once on site
    storage before release.

#### Q21 Index storage: single JSON index, rare flushes (decided 2026-11-04)

**Decision:** State stays in one `index.json` with one backup copy,
`index.json.bak`. The index is flushed every 5 minutes while events arrive,
at the end of each run, and after reconciliation, migration, or relocation.
Per-event journals cover the time between flushes.

**Consequences:**

*   Journal sequence numbers (P11 fix): every event gets a number that only
    increases. The index and the backup each record the last number they
    include. Startup replays only journals with higher numbers than the file
    it loaded, so a partial deletion cannot roll a session back.
*   Flush order: write a temporary file and `fsync` it, then rename
    `index.json` to `index.json.bak`, then rename the temporary file to
    `index.json`. Journals are deleted only up to the backup's sequence
    number, so the backup plus the remaining journals can always rebuild the
    current state.
*   Corruption recovery (R09): if `index.json` is missing or does not parse,
    startup loads `index.json.bak` and replays the journals that follow it.
    This happens automatically, consistent with Q12, and it raises an issue
    and a prominent log line. If both files exist and neither parses, the
    run stops and names both paths. If neither file exists, the project is
    treated as new. In that case `convert` adopts existing complete folders
    whose receipt matches the prepared content and never replaces them (see
    the Detailed Implementation Plan, `convert.py`).
*   Size reduction: inside each receipt's directory-mtime map, keys are
    stored relative to that receipt's (absolute) root, which roughly halves
    the estimated index size. Q23's absolute paths are unchanged at every
    other level.
*   Known limits, accepted: every run parses the whole index (about 30–60 MB
    at 20,000 sessions, estimated). Crash recovery replays up to about 5
    minutes of journal files. The contract names 20,000 sessions as the
    tested limit, and growing past it needs another design.
*   Measured in T15: parse time and memory at 20,000 sessions, which must
    fit the Q18 budgets.

#### Q2 Supported environments: Linux and Windows for production (decided 2026-11-04)

**Decision:** Linux and Windows are both supported for production data.
Windows limitations must be fixed or covered by explicit tests; documenting
them is not enough. `dcm2niix` is supported over a tested range of versions.

| Dimension | Supported | Evidence |
|---|---|---|
| Operating system | Linux, Windows; macOS not supported | Full CI suite on `ubuntu-latest` and `windows-latest` |
| Python | 3.11 and later, limited to versions still supported upstream (3.10 reached end of life in October 2026); proposed, confirm at section review | CI on 3.11, 3.12, 3.13, 3.14 on both operating systems |
| `dcm2niix` | The oldest and newest release versions that pass the real-converter smoke tests (Q9), listed in the README | CI downloads both release binaries for Linux and Windows and runs the smoke tests |
| Storage | Local disk, plus any shared filesystem that passes the site's two-node lock check (Q13) | Site deployment check (T10) |
| Scanners | No scanner-specific claims (Q9) | Not applicable |

**Windows items that must be fixed (not only documented):**

*   **Child-process containment (Q14) and process-tree termination (R11).**
    The archive parser and `dcm2niix` run inside a Windows Job Object, created
    through `ctypes` with no new dependency. The job has memory and CPU
    limits, and closing it kills the whole process tree. This gives Windows
    the same caps as Linux.
*   **Path length.** Planning computes the longest path a session will
    create. On Windows, when long-path support is not enabled, a session
    whose path would exceed 259 characters is held with an issue rather than
    failing partway through.
*   **Case-insensitive collisions (R14).** Roster IDs that differ only by
    letter case (`S001` and `s001`) are rejected while the roster is loaded,
    on every platform, so that behavior does not depend on the filesystem.
*   **Reserved device names (R14).** IDs equal to `CON`, `PRN`, `AUX`, `NUL`,
    `COM1`–`COM9`, or `LPT1`–`LPT9`, in any letter case, are rejected on
    every platform.
*   **Files in use.** If a rename during publication fails because a file in
    the destination is open (Windows refuses the rename), the existing
    rollback in `publish_tree` keeps the old folder, and the session fails
    with a `destination in use` issue. Explicit test.

**Consequences:**

*   `requires-python` becomes `>=3.11`, and the classifiers and CI matrix
    follow. The CI matrix grows to 8 test jobs plus packaging and two
    real-converter jobs.
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
*   Internal timestamps that drive behavior, such as attempt creation times
    for Q15 retention, are stored as UTC epoch seconds. Clock-display choices
    therefore never change what the pipeline deletes.

#### Q17 Release policy: Apache-2.0 (license decided 2026-11-04; other parts proposed)

**Decision (author):** The package is licensed under Apache-2.0.

**Proposed with it, to confirm at the section gate:**

| Topic | Proposal |
|---|---|
| Versioning | Semantic versioning. Any state schema change ships with `migrate` (Q22) and at least a minor-version bump. |
| Deprecation | A deprecated CLI option keeps working, with a warning, for at least one minor release before removal. |
| Security reports | `SECURITY.md` pointing to GitHub private vulnerability reporting. |
| Support | Best effort by the maintainer, with no response-time commitment. Only the latest minor release is supported. |

**Consequences:**

*   New files `LICENSE` (Apache-2.0 text) and `NOTICE`. `pyproject.toml`
    uses the SPDX expression `license = "Apache-2.0"` with
    `license-files = ["LICENSE", "NOTICE"]`, which needs `setuptools>=77`.
    No license classifier is added, because the SPDX field replaces it.
*   `MANIFEST.in` and `tests/check_distribution.py` must include and check
    both files in the wheel and the sdist.
*   Dependency compatibility: `pydicom` (MIT) can be used from Apache-2.0
    code. `dcm2niix` is called as an external program and not
    redistributed.
*   The `Operating System :: OS Independent` classifier is replaced with
    Linux and Windows classifiers (Q2).

#### Q1 Phases: all safety work in phase 1 (decided 2026-11-04)

**Decision:** Phase 1 closes P1 to P11 and introduces schema 3 with
`migrate`. Phase 2 adds containment and platform work, phase 3 scale and
relocation, and phase 4 the release documentation and first release. See the
[Phase map](#phase-map).

**Consequences:**

*   Schema fields needed by later phases (attempt `created_utc` for Q15
    retention, relative directory-map keys for Q21 size) are added in phase
    1, so there is only one migration.
*   Known cost, accepted: phase 1 changes most of `pipeline.py`. It is the
    largest phase and the slowest to review.
*   Windows gets SIGTERM-equivalent handling (Ctrl+C) in phase 1. Its
    whole-tree converter termination arrives with the Job Objects in phase
    2. Until then, Windows stops only the direct converter process, as today.

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

The concepts brief has no "Alternatives Weighed" section, so every
alternative below comes from the decision gates in this drafting session.
Each was rejected by the author, who chose the option recorded in the
Decision Log. The "Cons" column gives the failure condition stated when the
options were presented. The author gave no further rationale, so none is
added here.

| Decision | Rejected alternative | Pros | Cons (recorded trade-off) |
|---|---|---|---|
| Document scope | One document covering every area in full file-level detail | A single complete plan | Needs every decision settled at once; stalls on decisions that need site evidence first |
| Document scope | One area only (data safety) | Smallest document | A deferred area (such as provenance) could change the schema again and force a second migration |
| Q3 Canonical inputs | Replace only on explicit command, keeping superseded copies | Corrections can keep the same label; old data is kept | More code and disk; superseded copies need retention; risk of routine use without review |
| Q3 Canonical inputs | Allow replacement, but record and flag it | Smallest change | A wrong export becomes the session's data before anyone reviews it |
| Q5 Partial overlap | Merge or extend a session with a superset upload | One visit keeps one label | Excluded by Q3: accepted session content never changes |
| Q6 Meaning of `converted` | Output or a configurable exclusion rule per series | No permanent `partial` for legitimate non-image series | An overly broad rule hides a missing series; needs SOP class plus migration. Q19 later brought back a narrower form: a fixed, versioned SOP class list instead of configurable rules |
| Q6 Meaning of `converted` | Report-only warning | No migration | `converted` still does not mean complete; P1 stays open |
| Q19 DICOM eligibility | Extract MR image SOP classes only | Smaller extraction; clean converter input | Image series outside the list are never extracted or converted |
| Q19 DICOM eligibility | Keep current acceptance | No list to maintain | Sessions with non-image objects stay `partial` permanently |
| Q20 Repeats and multi-study | Accept with warnings | Less operator work | An export that merges two people's exams converts under one ID with only a warning |
| Q20 Repeats and multi-study | Keep current behavior | No change | Repeated slices reach the converter; merged exports give no signal |
| Q7 Reconversion | Automatically repair damage only | Damaged folders fix themselves | Edits that keep the file size go undetected; needs a rule for preserving added files |
| Q7 Reconversion | Automatically reconvert on damage and on upgrades | Outputs track the current converter | One upgrade reconverts the whole study; replaces folders that are in use |
| Q8 Provenance | Full fingerprint including the converter binary hash | Detects any converter change | A rebuild of the same version marks everything outdated; existing outputs start as unknown |
| Q8 Provenance | Version-level fingerprint with an "outdated" report | Flags converter upgrades | One converter call per run; different builds with the same version string look identical |
| Q12 Crash recovery | Explicit `repair` command | A person sees every action | Sessions stay stuck when nobody runs repair |
| Q12 Crash recovery | Detect and document manual steps | No repair code to get wrong | Manual moves on shared storage are error-prone |
| Q13 Lock scope | Root locks plus a heartbeat lease | Protects mounts whose locks only work within one node | A crashed run's lease blocks new runs; clock skew between nodes |
| Q13 Lock scope | Project lock plus root claim markers | Smallest change | No protection on node-local-lock mounts; claims go stale after a move |
| Q4 Upload readiness | Keep current size/mtime checks | No new code | Noisy failures; a Linux writer can keep writing after the move |
| Q4 Upload readiness | Quiet period of N minutes | No noise; no new protocol | Stalls longer than N; every upload is delayed; depends on the server clock |
| Q14 Trust model | Trusted archives with accident limits | Simplest | Parser bugs within the limits; forged receipts |
| Q14 Trust model | Untrusted archives and storage (HMAC receipts) | Detects forged receipts | Key management; a lost key invalidates every receipt |
| Q15 Retention | Explicit cleanup command | Nothing disappears unless a person chooses it | Grows until the disk reserve stops new work |
| Q15 Retention | Keep the last K attempts per session | Bounded per session | Discards the first failure's diagnostics; grows with the number of failing sessions |
| Q10 Sanitization | Keep current clearing and verify the rest | Header text and time of day stay out of outputs | Data a future converter writes into extensions is dropped silently |
| Q10 Sanitization | Clear text fields; refuse extensions | Nothing is dropped silently | Sessions go `partial` if the converter starts writing extensions |
| Q9 Validation scope | Reference fixtures in CI | Catches converter regressions | Approved fixtures are hard to get; never complete |
| Q9 Validation scope | Runtime cross-check against DICOM geometry (chosen when Q9 was reopened, then reverted) | Checks real study data; addresses brief priority 6 | Re-implements converter geometry logic; false alarms on edge cases. The author gave implementation time as the reason for reverting |
| Q9 Revert | Revert fully, without capturing geometry tags | Smallest phase 1 | A later check would need every earlier session re-prepared |
| Q11 Classification | Keep the rules and call the label a hint | No layout change | Known mislabels stay (T2* as T2, SWI mIP as SWI) |
| Q11 Classification | Remove labels from output paths | Folder names make no claims | Breaks downstream scripts; mixed layouts |
| Q22 Migration | Full verification with header reads | Old sessions get the new guarantee | Hours to days of header reads while the pipeline is locked |
| Q22 Migration | Lazy legacy status | Fastest upgrade | P1 stays possible for every pre-upgrade session |
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
| Module layout | Keep a single `pipeline.py` | Smallest structural change; existing test patch targets keep working | The module roughly doubles to about 2,000 lines |
| Module layout | New modules only for new subsystems | New code stays separate | Risk of import cycles between `pipeline.py` and the new modules |

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Security and privacy:** Archives are untrusted input; state, receipts,
    and the roster are trusted (Q14). Containment arrives in phase 2. In phase
    1, untrusted archives are still parsed in-process, as today. Privacy
    channels are listed under invariant 8. Phase 1 changes two of them:
    NIfTI header text now passes through (Q10), and unredacted converter
    output never reaches disk (P8).
*   **Performance:** The Q18 budgets apply. Phase 1 must not break the
    per-session budgets (no content reads; at most 20 metadata calls per
    cached session per command). The 20,000-session timing and memory are
    measured in phase 3.
*   **Rollout:** Install the phase 1 release, then run `migrate --dry-run`,
    review it, and run `migrate`. Until `migrate` has run, `prepare` and
    `convert` refuse to run on a schema-2 project.
*   **Rollback:** Reinstall 0.1.0, then copy `Reports/.state/index.v2.json`
    back to `index.json` and `run_history.v2.csv` back to `run_history.csv`.
    Effect: sessions prepared or converted after the upgrade are unknown to
    0.1.0. 0.1.0 re-prepares and reconverts them, replacing their folders,
    which it is allowed to do because they hold receipts. Upload markers are
    ignored by 0.1.0. The runbook documents this.

### Additional Resources

*   [`concepts_brief.md`](./concepts_brief.md): input brief.
*   [`mriDICOMpipeline-current-state-spec.md`](./mriDICOMpipeline-current-state-spec.md):
    the baseline behavior, review register R01–R20, and stress-test matrix
    T01–T18. It stays unchanged as the baseline.
*   [`current-source-manifest.json`](./current-source-manifest.json):
    baseline file hashes.
*   EGM [README](https://github.com/google/elephant-goldfish-model/blob/main/README.md)
    and [workflow guide](https://github.com/google/elephant-goldfish-model/blob/main/docs/getting_started.md).

### Future Improvements

*   **Phases 2–4:** see the [Phase map](#phase-map). Each gets its own
    design document that follows this contract.
*   **Overlap detail in conflict issues:** a held conflict could report how
    many DICOM objects the two uploads share, using both inventories, to
    speed up operator decisions (Q5).
*   **"Outdated" reporting:** Q8 rejected comparing provenance with the
    current environment. It can be added later without a schema change,
    because the provenance record already exists.
*   **Runtime geometry cross-check (Q9):** compare each output's dimensions
    and corner coordinates with the geometry tags captured at preparation
    since phase 1. It addresses brief priority 6 and was deferred because of
    implementation time. The design worked out when Q9 was reopened (sorted
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
| 0 | Move code from `pipeline.py` into the new modules **with no logic change**. Retarget imports in tests and launchers. Delete `pipeline.py`. | All 30 existing test cases pass with only import or patch-target edits |
| 1 | Packaging and CI: Python 3.11+, `setuptools>=77`, Apache-2.0 `LICENSE`/`NOTICE`, CI matrix, distribution check | `python -m build`, `twine check`, `check_distribution.py`, CI green |
| 2 | State schema 3: journal sequence numbers, `.bak`, 5-minute flush, refusal of schema 2 | `test_recovery.py` state tests, `test_migrate.py::test_commands_refuse_schema2_and_point_to_migrate` |
| 3 | Identity rules (case-only collisions, reserved names) | `test_identity.py` |
| 4 | Preparation: markers, immutable canonical ZIPs, SOP classes, repeats and multi-study holds, `--session`, `--accept-multi-study`, canonical rehash under `--audit`, missing-archive issue | `test_inputs.py` |
| 5 | Conversion: strict coverage, Q7 rules, last-good and last attempt, adoption, pass-through NIfTI, provenance, streaming redaction, process groups, classifier revision 2 | `test_conversion.py` |
| 6 | Per-root locks plus the legacy project lock | `test_locks.py` |
| 7 | Intents, reconciliation, SIGTERM handling | `test_recovery.py` |
| 8 | Reports and run history | `test_conversion.py`, `test_recovery.py::test_history_row_written_on_early_abort` |
| 9 | `migrate` command | `test_migrate.py` |
| 10 | Cache budget instrumentation; real-converter smoke tests | `test_cache_budget.py`, `test_real_converter.py` |
| 11 | README phase 1 updates; runbook | Review against the checklist in Verification Plan |

From step 2 onward, each step's regression tests are written first and fail
on the step 0 code. That is the evidence Goal 4 asks for.

**Import convention (applies to every module):** modules import sibling
modules, not names (`from . import fsutil`, then `fsutil.sha256(...)`), so
tests can patch a function where it is defined.

### Target Files to Create/Modify

#### 1. `src/mri_dicom_pipeline/pipeline.py` (Delete)

*   **Type / Scope**: Python module.
*   **Proposed Changes**: Its contents move into the modules below in step 0.
    Nothing re-exports it: the CLI is the supported interface (baseline §4),
    and the launchers and tests are updated.

#### 2. `src/mri_dicom_pipeline/config.py` (Create)

*   **Type / Scope**: Python module, CLI parsing and root configuration.
*   **Proposed Changes**:
    *   Constants `SCHEMA_VERSION = 3` and `LEGACY_SCHEMA_VERSION = 2`.
    *   `config_parser(phase)`, moved. Adds a repeatable `--session KEY` to
        both commands and a repeatable `--accept-multi-study KEY` to
        `prepare`. New `--dry-run` help text: "Plan only: no project writes
        or converter calls; hashes files only when combined with --audit".
        New `--audit` help text adds "prepare also re-hashes canonical ZIPs".
    *   `migrate_parser()`: `--project-root`, `--report-root`,
        `--input-root`, `--dicom-root`, `--nifti-root`, `--staging-root`,
        `--dry-run`.
    *   `configure(args, phase)`, moved. Also validates every `--session` and
        `--accept-multi-study` value with `identity.parse_key`, and sets
        `cfg.lock_roots` per the Q13 table (`prepare`: uploads, DICOM,
        reports, staging; `convert`: DICOM, NIfTI, reports, staging;
        `migrate`: all five) and `cfg.run_id` (format unchanged:
        `YYYYMMDD_HHMMSS_<8 hex>`, local time, Q16).

#### 3. `src/mri_dicom_pipeline/identity.py` (Create)

*   **Type / Scope**: Python module, identity grammar and roster (T01, T02).
*   **Proposed Changes**:
    *   `NAME` and `SAFE_ID` regexes, moved. `RESERVED_NAMES` = `CON`,
        `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, `LPT1`–`LPT9`, compared in upper
        case.
    *   `valid_id(text) -> bool`: `SAFE_ID` full match and not reserved.
    *   `parse_name(name, legacy=False) -> (sid, date, label)`, moved; an ID
        that is not valid raises `ValueError`, so the ZIP is held.
    *   `parse_key(text) -> (sid, label)` for `SUBJECT/SESSION` CLI values.
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
    *   `utc_now() -> float` (epoch seconds).

#### 5. `src/mri_dicom_pipeline/receipts.py` (Create)

*   **Type / Scope**: Python module, receipts, entries, and coverage.
*   **Proposed Changes**:
    *   `receipt_info(root, receipt, inventory)`, moved. The directory map
        keys are relative to `root` (`"."` for the root itself).
    *   `fast_valid(entry, audit=False)`, moved. It resolves the relative
        keys against `entry["root"]`; otherwise unchanged.
    *   `prepared_entry(sid, date, label, canonical, digest, inventory,
        extraction, conversion, last_attempt, accepted_multi_study)` builds
        the schema-3 session entry. It is used by `prepare.prepare_one` and
        by reconciliation adoption.
    *   `conversion_record(destination, status, n_volumes, source_sha256,
        provenance, coverage, created_utc, run_id)` adds `receipt_info`.
    *   `required_series(inventory) -> (required_keys, excused_keys)`. For
        schema-2 inventories without `sop_classes`, every series is
        required (Q22).
    *   `output_series_keys(output_root, inventory_files) -> set`: reads the
        `.json` sidecars listed in a conversion inventory and returns their
        `source_series_key` values. Used for re-evaluation after
        re-preparation and by `migrate`.
    *   `coverage(required, excused, converted) -> dict` with `required`,
        `converted`, `excused`, and `missing` (sorted lists).

#### 6. `src/mri_dicom_pipeline/state.py` (Create)

*   **Type / Scope**: Python module, persistent index (Q21, P11).
*   **Proposed Changes**: `class State(report_root)`:
    *   **Load:** read `index.json`. If it is missing, read `index.json.bak`.
        If it fails to parse, read `.bak` and set
        `recovered_from_backup = True`; the runner logs this and raises a
        blocking issue `state:recovered`. If both exist and neither parses,
        raise `StateError` naming both paths. If neither exists, start empty
        (`version 3`, `last_seq 0`).
    *   **Version:** 2 raises `StateError("schema 2 project: run
        'mri-dicom-pipeline migrate'")`. Any version other than 3 raises
        "unsupported".
    *   **Replay:** apply journals whose `seq` exceeds the loaded
        `last_seq`, in sequence order.
    *   `checkpoint(event)`: assign `seq = last + 1`, write the journal
        `<seq:016x>_<4 hex>.json` with `atomic_json`, apply the event, and
        flush if at least 300 s have passed since the last flush.
    *   `flush()`: set `last_seq`, write a temporary file with `fsync`,
        rename `index.json` to `index.json.bak`, rename the temporary file
        to `index.json`, then delete journals with
        `seq <= (backup's last_seq)`.
    *   `apply(event)`: as today (session replacement, source upserts, issue
        clears and upserts).

#### 7. `src/mri_dicom_pipeline/locks.py` (Create)

*   **Type / Scope**: Python module, locking (Q13).
*   **Proposed Changes**:
    *   `ProjectLock`, moved unchanged. It still guards
        `<project-root>/.mri_dicom_pipeline.lock` for compatibility with
        0.1.0 processes.
    *   `RootLocks(roots, command, run_id)`, a context manager. It creates
        each root if missing, then, in sorted path order, takes a
        nonblocking OS lock on `<root>/.mri_dicom_pipeline.lock` (same
        mechanism as `ProjectLock`). After each lock it writes
        `<root>/.mri_dicom_pipeline.lock.owner`
        (`{host, pid, command, run_id, started_utc}`). On the first failure
        it releases the locks already taken, reads that root's owner file,
        and raises `LockBusy` naming the root and the holder. On exit it
        releases everything and removes its own owner files.

#### 8. `src/mri_dicom_pipeline/recovery.py` (Create)

*   **Type / Scope**: Python module, intents and reconciliation (Q12).
*   **Proposed Changes**:
    *   Intent schema, in `Reports/.state/intents/<id>.json`:
        `{version: 3, id, kind: "zip_move" | "publish", phase, key, run_id,
        created_utc, ...paths}`. `zip_move` has `source`, `destination`,
        and `markers` (a list of from/to pairs). `publish` has `stage`,
        `destination`, `incoming`, `previous`, and `ownership_root`.
    *   `write_intent(reports_root, kind, **fields) -> id` (workers) and
        `clear_intents(reports_root, ids)` (coordinator, after the matching
        event is checkpointed).
    *   `reconcile(cfg, state, logger, dry_run) -> counts` implements the
        Q12 table in the Decision Log. Rules:
        *   Every path it touches must pass `fsutil.owned` against the
            intent's root.
        *   Adoption builds entries with `receipts.prepared_entry` or
            `receipts.conversion_record` from the receipt and inventory
            JSON, and records them through `state.checkpoint`.
        *   Every action is logged.
        *   It is idempotent: each rule checks the current filesystem state
            before acting.
        *   A dry run only logs the planned actions.

#### 9. `src/mri_dicom_pipeline/classify.py` (Create)

*   **Type / Scope**: Python module, classifier revision 2 (Q11).
*   **Proposed Changes**: `CLASSIFIER_REVISION = 2`. Signature unchanged:
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
        | 6 | `SWI` / `SWI_PHASE` | word `SWI` or `SWAN`; `SWI_PHASE` if word `PHA` or `PHASE`, or `ImageType` contains `P` or `PHASE` |
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
    *   `SANITIZER_REVISION = 2`, meaning pass-through.
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

*   **Type / Scope**: Python module, preparation (Q3, Q4, Q19, Q20, R18).
*   **Proposed Changes**:
    *   **Constants.** `DICOM_TAGS` adds `ImageType`,
        `ImagePositionPatient`, `ImageOrientationPatient`, `PixelSpacing`,
        and `SliceThickness`, all read in the same header-only read (Q9 tag
        capture). `SKIP_EXT`, moved.
        `MR_IMAGE_SOP_CLASSES` is the four UIDs in Q19.
        `SOP_LIST_REVISION = 1`.
    *   **`HoldError(ValueError)`.** Produces outcome `held` with a blocking
        issue. The ZIP stays where it is and no session entry changes.
    *   **`discover(cfg)`**, moved.
    *   **`marker_for(zip_path) -> ("done", None) | ("sha256", hex) |
        None`.** Looks for `<name>.done` and `<name>.sha256` next to the
        ZIP. The `.sha256` file is parsed as
        `<64 hex>[ whitespace [*]filename]`; if a file name is present, it
        must equal the ZIP name.
    *   **`extract_archive(source, stage, stop, accept_multi_study)`.**
        Keeps today's behavior, plus:
        *   It keeps a `sop_uid -> (sha256, size)` map. A byte-identical
            repeat is not written and increments `repeated_identical`. A
            repeat with different bytes raises `HoldError`. A missing SOP
            Instance UID raises `ValueError`.
        *   Series rows gain `sop_classes` (sorted list) and `required`
            (any class in `MR_IMAGE_SOP_CLASSES`). Classification passes
            `ImageType`.
        *   File rows gain `series_key`, `ipp`, `iop`, `pixel_spacing`,
            `slice_thickness`, `rows`, `cols`, and `frames`. A missing tag is
            stored as `null`. Nothing reads these fields in phase 1.
        *   More than one StudyInstanceUID without `accept_multi_study`
            raises `HoldError`.
        *   `content_sha256` is computed over unique objects.
    *   **`move_archive(source, destination, cfg, fingerprint, markers)`.**
        Also renames the marker files to `<canonical name>.done` or
        `.sha256` next to the canonical ZIP.
    *   **`prepare_one(candidate, previous, cfg, stop)`.** Order of
        operations:
        1.  Hash the ZIP. With a `.sha256` marker, a mismatch is a failure
            and the ZIP stays.
        2.  Canonical-change rule (Q3): if the source is the previous
            canonical archive and the digest differs, raise `HoldError`
            ("accepted canonical ZIP changed"). If the digest matches but
            the signature changed, emit a "refresh" event that updates only
            `archive_signature`.
        3.  Duplicate and conflict logic as today, with the `replacement`
            branch removed. `--force-reextract` re-extracts only when the
            digest matches.
        4.  Write the `zip_move` intent, then move. Write the `publish`
            intent, then publish. Receipts carry `run_id`. The event carries
            its intent IDs.
        5.  If the previous entry has a conversion, re-evaluate coverage
            with `receipts.output_series_keys` and `receipts.coverage`. A
            `partial` caused only by missing series that are now excused
            returns to `converted`; anything else is unchanged. No
            reconversion happens.
    *   **`plan_prepare(cfg, state, ids, pairs, logger)`.** Today's rules,
        plus:
        *   The `--session` filter.
        *   A non-legacy ZIP whose path is not in `sources` needs a marker.
            Otherwise it gets a nonblocking `wait:<path>` issue and no
            task, unless an owned extraction exists for its key: then it is
            planned as an *adopt* candidate (hash it and compare with the
            extraction receipt's `archive_sha256`; adopt on a match, hold
            otherwise).
        *   A ready session whose canonical archive is missing from the
            discovered paths gets a nonblocking `missing:<key>` issue.
        *   With `--audit`, cached canonical ZIPs are re-hashed, and a
            mismatch is held.

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
        group, wait 5 s, then `SIGKILL` to the group; on Windows,
        terminate, wait 5 s, kill (whole-tree termination arrives in phase
        2).
    *   **`organize_conversion(...)`.** As today, but using
        `nifti.validate_and_gzip` and `nifti.check_companion`. It also
        returns `converted_keys`: the `source_series_key` of each valid
        volume.
    *   **`convert_one(entry, cfg, stop)`:**
        1.  Read the extraction inventory and call
            `receipts.required_series`.
        2.  Run the converter. Organize the output. Compute
            `receipts.coverage`. Each missing required key adds the error
            "Missing required series: …".
        3.  Build `provenance`: package version, `run_id`, the resolved
            converter path, the argument list after the executable, the
            sorted set of sidecar `ConversionSoftwareVersion` values, and
            the classifier, sanitizer, and SOP-list revisions. It is written
            into the receipt together with `coverage`, `n_volumes`, and
            `run_id`.
        4.  **Explicit reconversion over last-good:** when the destination
            is the session's recorded complete folder, list its tree. If any
            file is outside its inventory and the control files, fail with
            "files not in inventory: …" before the swap.
        5.  Write the `publish` intent, then publish.
        6.  **State:** success sets `conversion` to the new record and
            clears `last_attempt`. A failure, partial, or interrupt with an
            existing complete `conversion` keeps that record and sets
            `last_attempt = {status, output, created_utc, run_id, reason}`.
            Without a complete `conversion`, it behaves as today and adds
            `created_utc`.
    *   **`plan_convert(cfg, state, ids, pairs, logger)`:**
        *   **Complete session**, not forced: a valid receipt means cached;
            an invalid one adds the blocking issue `output:<key>` ("output
            modified; restore it or reconvert explicitly") and no task.
        *   **Complete session, `--force-reconvert`:** selected, subject to
            `--session`.
        *   **No conversion record but a complete folder exists** at the
            canonical path: read its receipt (one small read, only in this
            case). Adopt it if `complete` and `source_sha256` match the
            prepared content; otherwise add the blocking issue
            `output:<key>` ("unreferenced output folder"). It is never
            replaced automatically.
        *   **Blocked set:** prepare issues for the session, excluding the
            nonblocking prefixes.
        *   Failed, partial, and interrupted rules without a complete record
            stay as today.

#### 13. `src/mri_dicom_pipeline/reports.py` (Create)

*   **Type / Scope**: Python module, logs, CSV reports, history.
*   **Proposed Changes**:
    *   `setup_log` and `write_csv`, moved.
    *   `NONBLOCKING_PREFIXES = ("date:", "wait:", "missing:")` and
        `unresolved(state) -> int`.
    *   `reports(state, cfg, pairs)`: `session_status.csv` adds
        `required_series`, `converted_series`, `excused_series`,
        `last_attempt_status`, and `last_attempt_folder`.
        *   `prepare_status` can also be `waiting_for_marker` or `held`.
        *   `conversion_status` can also be `output_modified`.
        *   "Converted with a failed last attempt" shows as `converted`
            plus a non-empty `last_attempt_status`.
    *   `append_history(cfg, row)`: columns are today's plus `reconciled`.
        Outcomes: `ok`, `issues`, `interrupted`, `terminated`, `aborted`.

#### 14. `src/mri_dicom_pipeline/runner.py` (Create)

*   **Type / Scope**: Python module, command orchestration.
*   **Proposed Changes**:
    *   `main(phase, argv)`: configure, then load the roster, then (except
        in a dry run) enter `ProjectLock` and `RootLocks`. It loads `State`,
        reconciles (report-only in a dry run), plans, applies `--limit`,
        and runs workers.
        *   The `try`/`finally` around everything after locking writes a
            history row on every exit path, with outcome `aborted` when
            normal finalization was not reached.
        *   `LockBusy` and `StateError` return 2.
    *   **Signals:** a SIGTERM handler on the main thread raises
        `Terminated(BaseException)`. It is handled like `KeyboardInterrupt`
        (stop event, pool shutdown, drain, flush, reports) and returns 143.
        Ctrl+C still returns 130.
    *   `accept(event)`: `state.checkpoint(event)`, then
        `recovery.clear_intents(event.get("intents", []))`, then today's
        counting and logging.
    *   `process_subject`, moved. It receives `cfg` (which carries `run_id`
        and the accepted multi-study set).

#### 15. `src/mri_dicom_pipeline/migrate.py` (Create)

*   **Type / Scope**: Python module, the `migrate` command (Q22).
*   **Proposed Changes**: `main(argv) -> int`:
    *   Parse with `config.migrate_parser`. Take all locks.
    *   Read the schema-2 index directly (this bypasses the `State` version
        check) and replay schema-2 journals in name order.
    *   For each session, read its preparation inventory (one small JSON).
        Mark series `required = true` with `sop_classes = ["unknown"]`.
        Geometry tags are not back-filled; that would need header reads,
        which Q22 excludes.
        Recompute `content_sha256` over unique `(sha256, size)` pairs. When
        it changes, update the conversion `source_sha256` in the same
        step. Convert directory-map keys to relative form. Set entry
        `version: 3`.
    *   For each converted session: `receipts.output_series_keys`, then
        `receipts.coverage`. A missing key sets status `partial`, keeps the
        folder recorded, and adds the blocking issue
        `convert:<key>` ("migrated: required series missing (SOP class
        unknown); re-prepare to record SOP classes").
    *   `study_count > 1` adds the multi-study hold issue. Provenance is
        rebuilt from the sidecars plus the fixed 0.1.0 argument list, with
        revision 1 for everything.
    *   `--dry-run` prints counts and per-session changes and writes
        nothing.
    *   Apply, in this order:
        1.  copy `index.json` to `index.v2.json`;
        2.  write the schema-3 `index.json` atomically (`last_seq 0`);
        3.  delete the schema-2 journals;
        4.  rename `run_history.csv` to `run_history.v2.csv`.

        A rerun on schema 3 is a no-op that returns 0. An interruption
        before step 2 leaves schema 2 intact.

#### 16. `src/mri_dicom_pipeline/cli.py` (Modify)

*   **Type / Scope**: Installed CLI entry point.
*   **Proposed Changes**: Commands become `prepare`, `convert`, and
    `migrate`. `prepare` and `convert` dispatch to `runner.main`; `migrate`
    dispatches to `migrate.main`.

#### 17. `src/mri_dicom_pipeline/__init__.py` (Modify)

*   **Type / Scope**: Package metadata.
*   **Proposed Changes**: `__version__ = "0.2.0"`, the minor bump for the
    schema change (Q17 versioning).

#### 18. `Scripts/unzip_and_inventory_dicoms.py` and `Scripts/convert_dicoms_to_nifti.py` (Modify)

*   **Type / Scope**: Source-checkout launchers.
*   **Proposed Changes**: Import `main` from `mri_dicom_pipeline.runner`
    instead of `.pipeline`. No other change.

#### 19. `pyproject.toml` (Modify)

*   **Type / Scope**: Build configuration.
*   **Proposed Changes**:
    *   `version = "0.2.0"` and `requires-python = ">=3.11"`.
    *   `build-system.requires = ["setuptools>=77", "wheel"]`.
    *   `license = "Apache-2.0"` and
        `license-files = ["LICENSE", "NOTICE"]`.
    *   Classifiers: remove Python 3.10 and `OS Independent`; add Python
        3.13 and 3.14, `Operating System :: POSIX :: Linux`, and
        `Operating System :: Microsoft :: Windows`.
    *   Dependencies unchanged.

#### 20. `LICENSE` and `NOTICE` (Create)

*   **Type / Scope**: Legal files.
*   **Proposed Changes**: `LICENSE` is the unmodified Apache License 2.0
    text. `NOTICE` reads "mriDICOMpipeline / Copyright 2026 Levi Davis". The
    copyright holder is taken from `git config user.name`; if an employer
    or institution owns the code, it replaces the name here before the
    first release.

#### 21. `MANIFEST.in` (Modify)

*   **Type / Scope**: sdist contents.
*   **Proposed Changes**: Add `include LICENSE NOTICE`. The exclusions are
    unchanged.

#### 22. `tests/check_distribution.py` (Modify)

*   **Type / Scope**: Artifact content check.
*   **Proposed Changes**:
    *   Require `LICENSE` and `NOTICE` in both the wheel (under
        `*.dist-info/licenses/`) and the sdist.
    *   Replace `assert` statements with explicit checks that raise
        `SystemExit`, so `python -O` cannot disable them (baseline §16.3).

#### 23. `.github/workflows/ci.yml` (Modify)

*   **Type / Scope**: CI.
*   **Proposed Changes**:
    *   **`test` job:** OS `[ubuntu-latest, windows-latest]` × Python
        `["3.11", "3.12", "3.13", "3.14"]`.
    *   **New `real-converter` job:** OS `[ubuntu-latest, windows-latest]`
        × `dcm2niix` `[OLDEST, NEWEST]`, Python 3.12. It downloads the
        release archive for the OS from
        `github.com/rordenlab/dcm2niix/releases` (the tags are set in the
        workflow `env`, and the asset names are checked when the job is
        written), sets `MRI_PIPELINE_DCM2NIIX`, and runs
        `pytest tests/test_real_converter.py`.
        *   `NEWEST` is the newest release on the day this step is
            implemented.
        *   `OLDEST` is the version the target cluster's module provides
            (Q2); if that cannot be determined, `OLDEST = NEWEST`, and the
            README support table lists a single version.
    *   **`package` job:** adds `migrate --help` to the smoke commands.

#### 24. `tests/support.py` (Create)

*   **Type / Scope**: Shared test helpers (moved out of `test_pipeline.py`,
    which `test_project_configuration.py` imports today).
*   **Proposed Changes**:
    *   `dicom_bytes(...)` gains `sop_class`, `sop_uid`, `series_uid`,
        `study_uid`, `description`, and slice geometry (`ImagePositionPatient`,
        `ImageOrientationPatient`, `PixelSpacing`, `SliceThickness`), so the
        real converter accepts the output.
    *   `archive(root, relative, members, marker="done" | "sha256" | None)`.
    *   `call`, `state`.
    *   `fake_converter_script(...)`: the fake converter reads its input
        DICOMs with `pydicom` and names outputs `series_<SeriesInstanceUID>_…`.
        Switches: skip one series, set the exit code, print a sentinel line
        then sleep, spawn a child process, write a header sentinel.
    *   `make_schema2_project(root, sessions)`: writes 0.1.0-format state,
        receipts, inventories, and sidecars as described in baseline §12.1.
        Sessions can include a non-image series, `study_count = 2`,
        repeated file hashes, and a series missing from the outputs.
    *   `run_with_crash(root, phase, crash_point, *args)`: runs a child
        Python process that patches the named function to call
        `os._exit(137)` on its N-th call, then calls `runner.main`.

#### 25. `tests/conftest.py` (Create)

*   **Type / Scope**: pytest fixtures.
*   **Proposed Changes**:
    *   `project` (today's fixture).
    *   `converter` (a fake converter built with `fake_converter_script`).
    *   `real_dcm2niix`: skipped unless `MRI_PIPELINE_DCM2NIIX` is set.

#### 26. `tests/test_pipeline.py` (Modify)

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
    *   `test_1000_cached_sessions_do_not_read_scan_contents` moves to
        `test_cache_budget.py`.
    *   Every ZIP these tests create gets a `.done` marker.

#### 27. `tests/test_project_configuration.py` (Modify)

*   **Type / Scope**: Existing configuration tests.
*   **Proposed Changes**:
    *   Import helpers from `support`.
    *   `test_old_state_rejected_without_writes` is parametrized over
        versions 1 and 2; for version 2 it expects the "run migrate"
        message.
    *   `test_publish_rolls_back_if_swap_fails` passes the swap names to
        `fsutil.publish_tree`.
    *   `test_public_cli_help` adds `['migrate', '--help']`.

#### 28. `tests/test_identity.py` (Create)

*   **Type / Scope**: Unit tests (T01, T02).
*   **Proposed Changes**:
    *   `test_case_only_id_collision_ignored_with_warning`
    *   `test_reserved_device_names_rejected` (parametrized: `CON`, `nul`,
        `Com1`, `LPT9`; both roster rows and ZIP names)
    *   `test_session_key_grammar` (`_1` and `_02` rejected; `_2` and `_10`
        accepted)

#### 29. `tests/test_inputs.py` (Create)

*   **Type / Scope**: Integration tests (T03, T05, T06; R05, R06, R12,
    R18; P3, P7).
*   **Proposed Changes**:
    *   **Markers:** `test_zip_without_marker_waits_nonblocking` (exit 0,
        `wait:` issue), `test_sha256_marker_mismatch_fails_and_keeps_zip`,
        `test_marker_moves_with_zip`, `test_legacy_import_needs_no_marker`,
        `test_adopt_canonical_zip_without_marker_when_extraction_matches`.
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

#### 30. `tests/test_conversion.py` (Create)

*   **Type / Scope**: Integration tests (T08, T11–T14, T18; P1, P4, P8,
    P10).
*   **Proposed Changes**:
    *   **Coverage and reconversion:**
        `test_missing_required_series_is_partial` (P1),
        `test_complete_session_not_reconverted_when_folder_modified` (P10:
        an added file stays, exit 2),
        `test_force_reconvert_refuses_folder_with_extra_files`,
        `test_failed_reconversion_keeps_last_good` (P4),
        `test_unreferenced_complete_folder_adopted_not_replaced`.
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
        `test_status_columns_and_last_attempt_reported` (T18).
    *   **Tag capture (Q9):** `test_geometry_tags_recorded_in_inventory`
        (the four tags per object, with `null` when absent).

#### 31. `tests/test_recovery.py` (Create)

*   **Type / Scope**: Crash-injection and process-control tests (T07–T09;
    P5, P11).
*   **Proposed Changes**:
    *   **`test_crash_at_boundary_reconciles`**, parametrized over the
        crash points: after a `zip_move` intent; after the ZIP rename; after
        the stage→incoming, destination→previous, and incoming→destination
        renames; before the previous folder is removed; after publish
        before the event; after the journal write before the intent is
        cleared; after the index→bak rename before the temporary→index
        rename.
        *   Each case reruns the command and asserts: no `.incoming_*` or
            `.previous_*` left; every session at its prior valid state or
            its new result; no input lost.
    *   **State and reconciliation:**
        `test_crash_during_reconciliation_is_recoverable`,
        `test_partial_journal_deletion_does_not_roll_back` (P11; it forces
        newest-first listing order, because NTFS's alphabetical order hides
        the defect),
        `test_corrupt_index_recovers_from_backup`,
        `test_both_index_files_corrupt_stops_run`.
    *   **Signals (skipped on Windows):**
        `test_sigterm_stops_converter_group_within_20s` (exit 143; the fake
        converter's child process is gone).
    *   **Other exits and faults:**
        `test_history_row_written_on_early_abort`,
        `test_enospc_during_publish_keeps_old_output` (a simulated `OSError`
        with `ENOSPC` on rename or copy).

#### 32. `tests/test_locks.py` (Create)

*   **Type / Scope**: Multi-process lock tests (T10, local part).
*   **Proposed Changes**:
    *   `test_shared_report_root_excludes_different_projects` (a child
        process holds the locks)
    *   `test_lock_failure_names_holder`
    *   `test_legacy_project_lock_still_excludes`
    *   `test_dry_run_takes_no_lock`

#### 33. `tests/test_migrate.py` (Create)

*   **Type / Scope**: Migration tests (T17).
*   **Proposed Changes**:
    *   `test_commands_refuse_schema2_and_point_to_migrate`
    *   `test_migrate_dry_run_writes_nothing`
    *   `test_migrate_upgrades_schema2_and_keeps_backup`
    *   `test_migrate_recomputes_digest_with_repeats`
    *   `test_migrate_flags_multi_study`
    *   `test_migrate_marks_missing_series_partial_and_reprepare_clears`
    *   `test_migrate_interrupted_then_rerun`

#### 34. `tests/test_cache_budget.py` (Create)

*   **Type / Scope**: Incrementality budget (T06; Q18 per-session budgets).
*   **Proposed Changes**: `test_1000_cached_sessions_do_not_read_scan_contents`,
    moved and rebuilt on schema 3.
    *   It counts calls to `os.stat`, `os.scandir`, and `os.listdir`, and
        `open` calls on files under the managed roots.
    *   It asserts zero content opens, and metadata calls per session of at
        most 20 for `prepare` and at most 20 for `convert`.
    *   It prints the timings, as today.

#### 35. `tests/test_real_converter.py` (Create)

*   **Type / Scope**: Real `dcm2niix` smoke tests (Q9; P1 and Q19
    hypotheses). Runs only when `MRI_PIPELINE_DCM2NIIX` is set.
*   **Proposed Changes**:
    *   `test_real_converter_names_outputs_by_series_uid` (output keys
        equal the inventory keys)
    *   `test_real_converter_with_non_image_object_converts` (a CSA
        Non-Image object is present; the exit code is recorded; the session
        is `converted` with that series excused)
    *   `test_real_converter_unconvertible_series_makes_session_partial`
        (the observed exit code is logged as evidence for P1)
    *   `test_real_converter_output_byte_identical`

#### 36. `README.md` (Modify)

*   **Type / Scope**: Operator documentation (Goal 7, phase 1 part; the
    full contract rewrite is phase 4).
*   **Proposed Changes**: Update the following and remove the claims they
    contradict:
    *   Python 3.11+.
    *   Required upload markers.
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
    *   Per-root locks with a pointer to the runbook's two-node check.
    *   Exit codes 0, 2, 130, 143.
    *   `migrate` and rollback.
    *   Classifier revision 2 and mixed layouts.
    *   License: Apache-2.0 (replacing "no license has been selected").
    *   The CI matrix and the real-converter jobs.

#### 37. `docs/runbook.md` (Create)

*   **Type / Scope**: Operator procedures. Phase 1 contents; phase 4 extends
    it.
*   **Proposed Changes**: One section per procedure:
    *   The two-node lock check (T10 site evidence) and how to record it.
    *   Upgrading with `migrate`, and rolling back.
    *   Clearing migrated false `partial` sessions.
    *   Resolving a canonical-ZIP conflict, a multi-study hold, and an
        `output modified` issue.
    *   Explicit reconversion.
    *   What reconciliation does after a crash and what it reports.
    *   Recovery from a corrupt index.
    *   Quota exhaustion.
    *   Node-local staging limits.

#### Files not changed in phase 1

`src/mri_dicom_pipeline/__main__.py`, `Scripts/prepare_dicoms.sh`,
`Scripts/convert_dicoms.sh`, `Scripts/requirements.txt`,
`examples/sessions.csv`, `.gitignore` (lock, owner, marker, and backup
files all sit under roots that are already ignored), `.gitattributes`, and
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
| P5 | `test_recovery.py::test_crash_at_boundary_reconciles`, `test_sigterm_stops_converter_group_within_20s` |
| P6 | `test_locks.py::test_shared_report_root_excludes_different_projects` |
| P7 | `test_inputs.py::test_dry_run_audit_hashes_but_writes_nothing` (help-text assertion) |
| P8 | `test_conversion.py::test_raw_converter_output_never_written` |
| P9 | CI matrix and `real-converter` jobs green; `check_distribution.py` license checks |
| P10 | `test_conversion.py::test_complete_session_not_reconverted_when_folder_modified` |
| P11 | `test_recovery.py::test_partial_journal_deletion_does_not_roll_back` |
| Q9 integrity (brief priority 6, narrowed) | `test_conversion.py::test_published_nifti_is_byte_identical_to_converter_output`, `test_real_converter.py::test_real_converter_output_byte_identical` |

### Verification Plan

*   **Compilation/Build**: `python -m pip install -e ".[dev]"`, then
    `python -m build`, then `python -m twine check dist/*`, then
    `python tests/check_distribution.py` on a clean `dist/`.
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
*   **Budgets**: `test_cache_budget.py` asserts zero content opens and at
    most 20 metadata calls per cached session per command.
*   **Site checks (before claiming support for a storage type)**: the
    runbook's two-node lock check on the target cluster filesystem.
*   **Documentation**: a checklist that maps each changed README statement
    to its test, reviewed before the phase 1 merge (Goal 7).

--------------------------------------------------------------------------------

*Drafted with EGM*
