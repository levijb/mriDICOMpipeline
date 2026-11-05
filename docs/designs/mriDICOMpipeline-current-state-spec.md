# mriDICOMpipeline — Current Functionality Specification and Production Review Baseline

**Document status:** descriptive baseline and review input; not an approved future design.  
**Baseline date:** November 4, 2026.  
**Package:** `mri-dicom-pipeline` 0.1.0. **Import:** `mri_dicom_pipeline`.  
**Persistent state schema:** 2. **Declared maturity:** Alpha.  
**Primary audience:** maintainers, imaging researchers, operators, and fresh-context Elephant-Goldfish Model (EGM) reviewers.

## 1. Purpose and how to use this document

This document describes the package as implemented at the baseline date. It gives a reviewer enough context to inspect the software without the previous development conversation, and identifies the evidence needed to turn a useful research workflow into a rigorously supported package.

Read sections 2–5 for the product and architecture, 6–15 for executable behavior and data contracts, and 16–21 for evidence, limitations, proposed stress tests, and release decisions. The companion `concepts_brief.md` is a short entry point for the production-hardening design discussion. `current-source-manifest.json` records SHA-256 hashes for the public files examined.

Terminology used throughout:

| Label | Meaning |
|---|---|
| Implemented | Directly supported by inspected code; this alone does not prove every edge case |
| Tested | Covered by the identified synthetic tests or explicitly recorded prior verification |
| Intended invariant | A property worth preserving; its scope and exceptions must be evaluated |
| Static concern | A code-derived review hypothesis or limitation, not necessarily an experimentally reproduced defect |
| Proposed gate | A suggested future acceptance condition; not implemented or approved merely by appearing here |

The source is authoritative when this document and runtime behavior differ. Discrepancies are review findings, not permission to silently change either artifact. No functional source changes or new clinical/research-data processing were performed to produce this document.

EGM separates context-rich specification work from fresh-context review and human trade-off decisions. This document is input to that process, not a claim that EGM review has already occurred. See the [EGM README](https://github.com/google/elephant-goldfish-model/blob/main/README.md) and [workflow guide](https://github.com/google/elephant-goldfish-model/blob/main/docs/getting_started.md).

## 2. Product mission, users, and operating context

The package receives session ZIP archives containing DICOM objects, validates their filename identities against a study-maintained CSV, organizes accepted archives, extracts and inventories DICOM objects, converts prepared sessions through an external `dcm2niix`, and publishes organized NIfTI files with selected metadata and processing status.

Its central workflow is incremental: operators add batches over time and repeatedly invoke the same commands. Already completed scans should not require expensive archive hashing, image-tree traversal, DICOM header reading, or conversion on every normal rerun. Permanent reporting uses stable filenames rather than a new report folder for every invocation.

The expected operator is a researcher or research-computing staff member comfortable with Python environments and filesystem paths. A study supplies its own pseudonymous subject IDs, acquisition dates, uploads, filesystem permissions, and resource allocation. The package contains no institution-specific account, study name, scheduler partition, or credential.

The primary deployment is one compute-node allocation, optionally obtained through Slurm, with a Python coordinator and concurrent subject workers. Local execution is also supported. CPU allocations, storage behavior, and actual scanner coverage must be validated at the deployment site.

### 2.1 Supported scope

- Batch intake of filesystem ZIP archives; recursive discovery within designated upload directories.
- Filename-based subject/date identity with explicitly numbered additional sessions on the same date.
- CSV authorization of expected subject/date pairs, including configurable column names.
- Extraction of eligible DICOM objects, including objects without an extension.
- Cached preparation and conversion across repeated invocations.
- Parallel subjects within a single process orchestration layer.
- Organized NIfTI, selected JSON metadata, and existing diffusion companions.
- Logs, current session status, unresolved issues, and compact run history.
- Synthetic integration tests and Python distribution packaging.

### 2.2 Exclusions and non-goals of the current version

There is no PACS/DICOMweb integration, upload service, resumable network-transfer implementation, GUI, database service, job-array manager, or distributed multi-node worker system. There is no automatic detection of semantically identical images after different anonymization, automatic subject matching from patient headers, automatic same-day session numbering, or global acquisition deduplication.

The package does not produce BIDS datasets, perform defacing, remove burned-in identifiers, certify de-identification, select the scientifically best acquisition, register images, correct distortion or motion, segment anatomy, or establish diagnostic suitability. It does not prove that a source export contains every acquired slice or series. Non-MRI data is not explicitly rejected by a modality gate, but broad non-MRI support is not established.

There is no implemented state migration or relocation command, published release pipeline, declared software license, or validated compatibility matrix for converter versions and scanners.

## 3. Baseline identity and evidence hierarchy

The core implementation examined is `src/mri_dicom_pipeline/pipeline.py`, SHA-256:

```text
5667d1c1fd7e629e2559f19c0723c07c8d4f47acf1a3bcce7ccf8d7df38735a6
```

Use the companion manifest to distinguish this baseline from subsequent edits. No immutable Git commit identifier is established by this brief. File hashes establish source identity, not package authenticity or scientific correctness.

Evidence priority is: executable source and manifests; directly inspected tests; recorded test/build outcomes from this development session; README explanations; then explicitly labeled proposals. Previous success reports are not expanded into guarantees about untested platforms or data.

This baseline uses synthetic IDs such as `S001` exclusively. Review artifacts must not embed real participant lists, source archive contents, historical private reports, or credentials.

## 4. Repository and component map

All paths in this specification are relative to the pipeline repository, unless explicitly described as study-data paths.

| Component | Responsibility and public status |
|---|---|
| `src/mri_dicom_pipeline/__init__.py` | Package version, currently `0.1.0` |
| `src/mri_dicom_pipeline/cli.py` | Installed CLI dispatch for `prepare` and `convert` |
| `src/mri_dicom_pipeline/__main__.py` | `python -m mri_dicom_pipeline` entry point |
| `src/mri_dicom_pipeline/pipeline.py` | Shared implementation, configuration, state, extraction, conversion, reporting, orchestration |
| `Scripts/unzip_and_inventory_dicoms.py` | Source-checkout preparation launcher |
| `Scripts/convert_dicoms_to_nifti.py` | Source-checkout conversion launcher |
| `Scripts/prepare_dicoms.sh`, `Scripts/convert_dicoms.sh` | Bash launchers; pass arguments through; honor `PYTHON`, default `python3` |
| `Scripts/requirements.txt` | Runtime dependency declaration for source-script deployment |
| `examples/sessions.csv` | Synthetic reference example |
| `tests/test_pipeline.py` | Synthetic intake/conversion/recovery/cache integration tests |
| `tests/test_project_configuration.py` | Generic configuration, entry-point, schema, and publication rollback tests |
| `tests/check_distribution.py` | Separate built-artifact content check; not collected as a pytest test module |
| `pyproject.toml`, `MANIFEST.in` | Packaging and source-distribution inclusion/exclusion |
| `.github/workflows/ci.yml` | Proposed execution matrix expressed as GitHub Actions configuration |
| `README.md`, `.gitignore`, `.gitattributes` | Operator guidance, local-data exclusions, line-ending settings |

The main module is approximately 1,000 lines and contains multiple responsibilities. There is no separately versioned public Python library API contract. The CLI is the practical supported interface; imports used by tests are implementation interfaces.

Source launchers insert the checkout's `src` directory into Python's import path. Consequently, copying `Scripts` alone is insufficient. Deploy the package or the complete source checkout.

## 5. End-to-end architecture

```text
Study reference CSV ──> filename authorization
                              │
Incoming ZIPs ──> discover/plan/cache check ──> prepare subject workers
                                                   │
                                       isolated extraction staging
                                                   │
                         canonical ZIP + published DICOM session
                                                   │
                                      coordinator checkpoint/report
                                                   │
Prepared session index ──> conversion planner ──> conversion subject workers
                                                   │
                                   external dcm2niix in isolated staging
                                                   │
                              validate/filter/rename/compress outputs
                                                   │
                            complete NIfTI session OR preserved partials
                                                   │
                                      coordinator checkpoint/report
```

The coordinator owns permanent state updates and shared CSV/log writes. Workers receive subject-specific copies of state and return result events through a queue. Workers operate on distinct subject session trees. A single OS advisory lock is used for non-dry-run commands targeting the same project root.

Preparation and conversion are separate invocations, not an automatic combined pipeline. Conversion consumes prepared state, not a new discovery of arbitrary DICOM folders.

## 6. Installation, dependencies, and command contract

### 6.1 Runtime and build dependencies

Python 3.10 or later is declared. The sole declared Python runtime dependency is `pydicom>=3.0,<4`. Conversion also requires an externally installed executable named `dcm2niix`, or an explicit `--dcm2niix-bin` path. The package neither downloads nor bundles that converter.

The build backend is setuptools with `setuptools>=68` and `wheel`. Test extras declare `pytest>=8,<10`. Development extras additionally include `build>=1,<2` and `twine>=5,<7`. Dependency resolution is bounded, not fully locked. Python 3.11 appears in classifiers but is not in the configured CI matrix; later Python versions are not established by those classifiers.

```bash
python -m pip install .
mri-dicom-pipeline --help
mri-dicom-pipeline --version
mri-dicom-pipeline prepare --project-root /data/study --dry-run
mri-dicom-pipeline prepare --project-root /data/study --limit 1
mri-dicom-pipeline convert --project-root /data/study --limit 1
```

`python -m mri_dicom_pipeline prepare ...` and `convert ...` are equivalent routes. No shell is used to execute converter command arguments.

### 6.2 Common stage options

| Option | Default | Current meaning |
|---|---|---|
| `--project-root PATH` | Current working directory | Study root, not the installed package location |
| `--reference-csv PATH` | `PROJECT_ROOT/sessions.csv` | Authorization roster |
| `--subject-column NAME` | `subject_id` | Exact CSV subject column name |
| `--date-column NAME` | `scan_date` | Exact CSV acquisition-date column name |
| `--report-root PATH` | `PROJECT_ROOT/Reports` | Reports and persistent state |
| `--workers N` | `min(4, SLURM_CPUS_PER_TASK)`; otherwise 1 | Maximum concurrent subject workers |
| `--limit N` | Unlimited | Maximum selected work items after planning/cache checks |
| `--dry-run` | False | Plan and print without project writes or converter execution; see audit exception below |
| `--audit` | False | Hash inventory-listed cached files in addition to metadata checks |
| `--retry-failed` | False | Reconsider unchanged failed/partial work |
| `--allow-unlisted-dates` | False | Permit an unlisted date for an already known subject |
| `--version` | — | Print package version and exit |

### 6.3 Stage-specific options

| Stage | Option | Default and behavior |
|---|---|---|
| Prepare | `--input-root PATH` | `PROJECT_ROOT/DicomUploads`; parent of `Uploads` and `SubjectsSorted` |
| Prepare | `--output-root PATH` | `PROJECT_ROOT/DicomExtracted` |
| Prepare | `--force-reextract` | Bypass successful preparation cache; exact duplicate copies may still be recognized |
| Prepare | `--import-legacy` | Also discover `DicomUploads/Subjects`, permitting validated six-digit legacy dates there |
| Convert | `--dicom-root PATH` | `PROJECT_ROOT/DicomExtracted`; must match cached extraction root |
| Convert | `--output-root PATH` | `PROJECT_ROOT/Niftis` |
| Convert | `--staging-root PATH` | `PROJECT_ROOT/.work`; may be node-local scratch |
| Convert | `--dcm2niix-bin VALUE` | `dcm2niix`; executable name/path; `.py` paths run through the current Python interpreter |
| Convert | `--timeout N` | 14,400 seconds per session converter process |
| Convert | `--force-reconvert` | Bypass successful/failed conversion cache selection |

All relative path arguments resolve against the invoking working directory, not automatically against `--project-root`. The resolved upload, DICOM, NIfTI, report, and staging roots must be pairwise disjoint: neither identical nor ancestors of one another. This is checked even for roots not directly used by a stage. The roster path is outside that disjointness check.

Workers and limits must be positive integers; conversion timeout must be positive. A present `SLURM_CPUS_PER_TASK` is parsed as an integer and acts as the worker ceiling. A worker count above it is rejected. No equivalent enforcement of actual local core or memory availability exists outside Slurm.

### 6.4 Exit behavior and plan limitations

Successful normal runs return 0. Held work, unresolved non-date issues, worker-result failures, and many handled configuration/runtime errors return 2. An orderly `KeyboardInterrupt` during the worker loop returns 130. Argparse help/version exits with 0; invalid command syntax typically exits with 2. Exceptions outside the caught categories and interruption outside the worker loop do not have a uniform structured exit/report contract.

A plain dry run skips worker execution, hashing new inputs, permanent logs, state writes, and locking. Its return value is based on newly held plan items, not the full normal-run unresolved-issue calculation. It does not prove archive readability, storage sufficiency, converter availability, or successful conversion.

**Important code/documentation discrepancy:** `--dry-run --audit` passes `audit=True` into cached receipt validation during planning and can hash old output contents. The broad README/CLI statement that a dry run performs no hashes is therefore not unconditional. This is a static trace of the current implementation, not a newly added behavior.

## 7. Input identity and reference CSV contract

### 7.1 ZIP identity

The accepted ordinary name is `SUBJECT_YYYYMMDD.zip`, optionally `SUBJECT_YYYYMMDD_N.zip` for another session on the same date. `N` is a canonical decimal integer at least 2, with no leading zeros. `_1`, `_02`, six-digit ordinary dates, invalid calendar dates, and malformed names are rejected by planning. Matching is case-insensitive for the filename pattern and extension; the captured ID retains its original case and roster matching remains exact.

IDs contain ASCII letters and digits with internal single hyphen/underscore separators: `[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*`. Leading zeros are preserved. Spaces, dots, path separators, and leading/trailing separators are not accepted. Case collisions, Windows reserved device names, maximum filename lengths, and case-insensitive filesystem behavior require dedicated validation; the grammar alone is not complete cross-platform path validation.

| ZIP | Session key | DICOM/NIfTI session directory |
|---|---|---|
| `S001_20260725.zip` | `S001/20260725` | `Subjects/S001/20260725` |
| `S001_20260725_2.zip` | `S001/20260725_2` | `Subjects/S001/20260725_2` |
| `site-A_001_20260725_3.zip` | `site-A_001/20260725_3` | `Subjects/site-A_001/20260725_3` |

The filename, not a wrapper directory or DICOM patient field, establishes identity. Additional suffixes are assigned by the operator; the code does not infer their clinical meaning. Each suffix is independently checkpointed and converted. All suffixes validate against the same subject/base-date roster pair.

With `--import-legacy`, only the additional legacy discovery root interprets `SUBJECT_MMDDYY.zip` as `20YYMMDD`, validates it, and normalizes its canonical name. Legacy extracted directories are not automatically imported as completed state.

### 7.2 Roster loading and authorization

The file is opened as UTF-8 with optional BOM and parsed using `csv.DictReader`. Subject/date column names must be distinct and nonempty. Duplicate header names or missing configured columns fail loading. Extra named columns do not participate in identity. Values in the two configured columns are stripped; header names are exact.

Dates accept `YYYYMMDD`, `YYYY-MM-DD`, or month/day/four-digit-year input. They normalize to `YYYYMMDD`. Blank rows are skipped, duplicate pairs collapse into a set, and invalid IDs/dates produce row-number warnings. At least one valid pair is required globally.

A valid ID is registered before its date is parsed. Thus an ID with a blank or malformed date can still be a known ID; it grants no ordinary date authorization but may qualify for `--allow-unlisted-dates`. A blank date does not itself produce an invalid-date warning. The loader does not rewrite the CSV.

Preparation holds an unknown ID or unauthorized date in the upload area. Conversion rechecks the current roster, so removing authorization can block a formerly prepared session. The override permits extra dates only for known IDs; it does not authorize an unknown ID. No roster content hash, approval history, or schema metadata is stored with a run.

## 8. Filesystem contract and ownership

Default study layout:

```text
study/
  sessions.csv
  DicomUploads/
    Uploads/<arbitrary batch folders>/<subject>_<session>.zip
    SubjectsSorted/<subject>/<subject>_<session>.zip
    Subjects/                          # opt-in legacy root
  DicomExtracted/Subjects/<subject>/<session>/
    dicom/00000001.dcm
    .inventory.json
    .pipeline_complete.json
  Niftis/
    Subjects/<subject>/<session>/<scan_type>/<output files>
    .pending/<subject>/<session>/<conversion stage name>/
  Reports/
    pipeline.log
    latest_prepare.log
    latest_convert.log
    session_status.csv
    issues.csv
    run_history.csv
    .state/index.json
    .state/pending/<event>.json
  .work/<isolated worker stages>/
  .mri_dicom_pipeline.lock
```

`owned()` resolves a target and rejects operations on the ownership root itself or outside its descendants. Managed recursive deletion uses that check. Existing extraction or complete-conversion destinations lacking `.pipeline_complete.json` are refused replacement. The presence of that file is the ownership gate; its content is not authenticated as proof of provenance before replacement.

Uploads and canonical archives are moved, not modified internally. Accepted input relocation uses rename within the upload tree and refuses an existing canonical destination. Cross-device upload moves are not silently converted to copy-and-delete. A source already equal to its canonical path is not moved.

No configurable retention service cleans failures, `.pending`, or arbitrary abandoned `.work` directories. Successful stages are normally removed. Reports and state are operational data, not release artifacts.

## 9. Preparation behavior

### 9.1 Discovery and planning

Discovery walks `SubjectsSorted` first and `Uploads` second; legacy `Subjects` is added only with its flag. Hidden child directories and symlinked child directories are excluded. ZIP symlink files are held during planning. Discovery-root symlinks, destination symlinks, replacement races, and malicious concurrent filesystem edits are not fully specified by this policy.

ZIP names are discovered recursively and sorted within traversal. Each candidate is parsed, roster-authorized, statted, compared with source cache, and checked against the previous extraction receipt. New/changed candidates are grouped by subject. Planning can also remove obsolete preparation issues whose referenced source path no longer exists.

`--limit` is applied after the complete discovery/planning stage. It limits work items, not the number of files discovered, directories statted, held issues recorded, or roster rows read. The planner's item order determines selection; there is no explicit newest-first or fairness policy.

### 9.2 Archive processing and DICOM inventory

For a selected source, preparation creates an isolated stage, computes a whole-ZIP SHA-256, and checks that `[size, mtime_ns]` has not changed. If needed, it opens the ZIP and examines eligible members using `pydicom.dcmread(..., stop_before_pixels=True, specific_tags=...)`.

Member names are never used as output paths. Every accepted object is copied to sequential `dicom/00000001.dcm`, `00000002.dcm`, and so on, preserving bytes and allowing duplicate ZIP member names without path overwrites. Traversal strings therefore do not dictate extraction destinations.

Members in a path component named `ddv` and a fixed list of non-DICOM suffixes are skipped. The current suffix list is:

```text
.jpg .jpeg .png .gif .bmp .tif .tiff .pdf .txt .xml .html .htm .csv .json
.ini .log .dll .exe .ocx .config .manifest .css .js .ico .onnx .lic
.profiles .mapping .bin .rlv .rlv_meta .rbv .rbv_meta .local .zip
```

Consequently, a genuine DICOM stored under a skipped suffix is omitted, and nested ZIPs are not unpacked. A preamble `DICM` signature selects normal parsing; other eligible files use forced parsing. An unreadable explicit `.dcm` or magic-bearing file fails the archive; an unreadable other file may be skipped. A candidate needs an SOP class and either a series UID or modality. DICOMDIR's storage class is skipped. Accepted objects must ultimately have a series UID. There is no SOP-class allowlist restricted to MR image storage, no per-instance UID deduplication, and no complete pixel decode validation.

Accepted object streams are fully copied and hashed, invoking ZIP CRC checking for those streams. Skipped members are not all read to completion; their CRCs are not thereby established. No accepted objects is a failure. For uncompressed/non-deflated syntax, a rough declared pixel-byte lower-bound check rejects obvious short objects; this compares total file size with estimated pixel bytes and is not a comprehensive pixel-data integrity check.

Inventory records per-file names, sizes, and hashes; per-series hashed UID key, series number, heuristic scan type, and object count; total object/series counts and byte estimates; count of studies based on hashed study UIDs; skipped-file count; and distinct header StudyDate values. Series keys use the first 20 hex characters of SHA-256 of the source series UID. The study UID hashes are used for counting and are not retained as a list in the returned inventory. `n_dicom` counts accepted objects, not necessarily slices; multiframe objects can contain many frames.

If header dates exist and are not exactly the singleton filename date, a nonblocking date issue is written. Filename identity remains unchanged. Multiple studies or dates within one archive do not automatically split the session or trigger rejection.

### 9.3 Duplicate and conflict rules

Deduplication is scoped to a session key with a valid previous preparation, not the entire study:

| Situation | Current action |
|---|---|
| Same key and identical ZIP digest, valid previous extraction | Record duplicate; avoid re-extraction unless forcing the canonical archive |
| Different ZIP bytes but same multiset of accepted DICOM hashes/sizes | Record duplicate after inventory; packaging, wrapper paths, and skipped ancillary differences do not distinguish it |
| Incoming different-path ZIP, same key, valid previous extraction, different DICOM contents | Hold conflict; keep inputs; require operator resolution |
| Explicit `_2` key with identical contents | Treat as separate session; no cross-key deduplication |
| Changed canonical source path already associated with that key | Treat as replacement input rather than the incoming-copy conflict case; may republish outputs |

Content digest construction is SHA-256 of sorted newline-separated `file_sha256:file_size` records. Multiplicity matters; object order and archive member names do not. This does not compare image semantics, pixel values after different headers, or partial overlap. Duplicate incoming archives remain in `Uploads`, with a cached outcome to avoid repeated hashing.

The canonical-replacement distinction is material: it must not be described as universal refusal to change an existing session. An already-invalid prior extraction also changes which duplicate/conflict branches are reachable. Review tests should cover both cases.

### 9.4 Publication and outcomes

After inventory, the code checks source metadata again, refuses an unowned extraction destination, moves an accepted input to its canonical path, writes stage inventory and completion receipt, publishes the stage, computes output-directory receipt metadata, and returns a result event. The session becomes `ready`; previous conversion state is retained but becomes stale when content digests differ.

A caught preparation failure returns a failure/source event; a newly failed session receives `failed` or `interrupted`. Existing previous session data is retained when no replacement result is produced. Preparation always attempts to remove its stage in `finally`; it does not retain the same level of failed diagnostic working material as conversion. Failures before entering its internal try block or during cleanup can propagate differently.

## 10. Conversion behavior

### 10.1 Selection

Only state entries with preparation status `ready` are considered. Each is checked against the current roster, configured DICOM root, unresolved preparation issues for that subject/session, and extraction receipt validity. Date-warning issues do not block conversion. A fully cached matching conversion is skipped even if the converter binary is now unavailable.

Successful-cache matching uses prepared DICOM content digest, expected output root, and receipt validation. Unchanged conversion failures/partials are skipped unless retry or force is requested. Interrupted results remain eligible for retry. Conversion does not independently hash or require the canonical source ZIP before accepting a valid prepared session.

### 10.2 Converter invocation

Each selected session has isolated staging with `raw/` and `organized/`. Before launch the code compares free space on the staging filesystem with `max(2 * pixel_bytes, 2 * dicom_bytes)`. This is a per-session estimate, not a reservation or a complete check of simultaneous worker, publication-destination, or retained-failure space.

The literal argument vector after the executable is:

```text
-z n -b y -ba y -c "" -d 9 -f series_%j_%s_%e -o <stage/raw> <extraction/dicom>
```

The external converter defines the scientific interpretation of its options. The pipeline requests uncompressed staging and JSON output, then performs its own output compression/filtering. No compatible converter version is pinned or validated at startup. A `.py` binary value is executed with the current Python interpreter, supporting the simulated converter tests.

`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and `MKL_NUM_THREADS` are set to `1` in the child environment. These are environment controls, not proof that every converter or child tool uses exactly one CPU.

Stdout/stderr share a staging log. The parent polls approximately every 0.25 seconds. On stop or elapsed timeout it calls terminate, waits up to ten seconds, then kills and waits if necessary. This controls the direct child; a full descendant process-group termination contract is absent. Timeout covers converter runtime, not a hard wall limit for the entire session's validation, compression, hashing, and publication.

### 10.3 Organizing output and scan classification

Top-level raw files ending in `.nii` or `.nii.gz` are processed in sorted order. Matching JSON is required for overall success. Each output goes to `<scan_type>/<subject>_<session>_<scan_type>_<token>.nii.gz`, with an accompanying JSON and any existing valid `.bval` and `.bvec`. The token is the first 16 hex characters of SHA-256 of the converter's output stem. It is deterministic for that stem, not a pixel-content digest or a guarantee of cross-converter name stability.

The JSON contains selected technical metadata plus `subject_id`, `session`, `session_date`, `scan_type`, `source_archive_sha256`, and `source_series_key`. The series key is derived from the UID-like segment of recognized `series_...` stems, with token fallback. There is no collision-resolution strategy beyond reporting a colliding target name.

Classification concatenates description and protocol in uppercase and applies ordered substring rules:

| Priority | Rule/output |
|---|---|
| 1 | `LOCALIZER`, `SCOUT`, or `SURVEY` → `LOCALIZER` |
| 2–4 | `EADC` → `EADC`; `ADC` → `ADC`; `QSM` → `QSM` |
| 5 | `SWI` or `SWAN` → `SWI_PHASE` when phase-like text or phase image tokens match, otherwise `SWI` |
| 6 | `MIN IP` or `MINIP` → `SWI_MINIP` |
| 7 | `FLAIR` → `FLAIR` |
| 8 | `T1`, `MPRAGE`, `BRAVO`, `SPGR`, or `THRIVE` → `T1`, or `T1_POST` with `POST`, `GAD`, `+C`, or `CONTRAST` |
| 9 | `T2` → `T2` |
| 10 | `DWI`, `DIFFUSION`, `TRACE`, or `DTI` → `DWI` |
| 11–13 | `MRA`/`TOF` → `MRA`; `MRV` → `MRV`; `GRE` → `GRE` |
| 14 | `PERFUSION`, `DSC`, `DCE`, or `ASL` → `PERFUSION` |
| Fallback | `OTHER` |

Ordering matters: SWI text can take precedence over MINIP, and T2-like text over diffusion-like text. Rules are not token-bounded or scanner-specific and are not an imaging quality classifier. The system preserves components rather than selecting a best scan. Preparation classification does not pass ImageType to the classifier, while conversion does when available, so the two stages' scan-type labels can differ.

### 10.4 NIfTI and companion validation

The sanitizer supports single-file NIfTI-1 and NIfTI-2, checks header size/magic, endian interpretation, active positive dimensions, positive bit depth, and voxel offset. The extension region is limited to 16 MiB. It checks that the post-offset byte stream is at least the dimensions/bit-depth-derived pixel count.

For NIfTI-1 it clears byte ranges `[4,32)`, `[148,252)`, and `[328,344)`. For NIfTI-2 it clears `[240,344)` and `[508,524)`. It zeros extension/padding bytes between header and voxel offset and retains offset and image payload. Output gzip uses empty filename, `mtime=0`, and compression level 6. This improves repeatability of the compression header; it does not establish fully reproducible conversion across platforms or converter versions.

The code does not establish datatype/bit-depth consistency, finite/valid geometric transforms, anatomical orientation correctness, exact payload length, valid scaling, slice completeness, or cross-file scientific consistency. The selected header ranges target text fields, while zeroing the entire extension area can also discard structured scientific metadata stored in extensions. Whether that loss is acceptable needs an explicit format policy and independent NIfTI-library comparisons.

Existing diffusion companions must have nonempty whitespace-delimited values convertible to finite floats. They are copied byte-for-byte under renamed stems. There is no b-vector shape/orientation validation, b-value nonnegativity requirement, volume-count agreement, unit check, or mandatory companion requirement inferred from a DWI label. Missing companion files do not by themselves fail a session.

Any unhandled raw artifact, including orphan sidecars, unexpected additional files, or nested outputs not handled by the top-level image loop, makes the result incomplete. Inventories include organized output files, sizes, and hashes. Inventories and completion receipts are not themselves included in that output file list.

### 10.5 Complete, partial, and failed publication

Overall completion requires no organization errors, at least one valid volume, and no converter error reason. A nonzero converter exit cannot be converted into success simply because some images exist.

Complete results publish to `Niftis/Subjects/<subject>/<session>`. Incomplete organized results publish under `Niftis/.pending/<subject>/<session>/<stage name>`. The latter can include zero usable images plus control files; `.pending` is not a promise of scientific usability. Exceptions can leave only the working stage rather than an organized partial publication.

Partial/error cases retain the conversion stage and log its location in the issue. A failed new attempt leaves earlier complete output in place, but the latest state entry can record the new failed/partial attempt; existence of an older successful folder alone does not imply current successful state. Retries do not automatically delete old partial attempts.

## 11. Metadata, privacy, and trust boundary

The original DICOM bytes and canonical ZIP retain their headers and pixels. Extracted files are not anonymized by extraction. Filename subject IDs and acquisition dates are deliberately carried into output paths and sidecars. Use of a pseudonymous ID is an operator decision, not an enforcement mechanism.

JSON filtering is by allowed field name and shallow value type. Allowed technical field names are:

```text
Modality Manufacturer ManufacturersModelName MagneticFieldStrength
SeriesNumber EchoNumber EchoTime RepetitionTime InversionTime FlipAngle
ImageType ImageOrientationPatientDICOM PixelSpacing SliceThickness
SpacingBetweenSlices MRAcquisitionType ScanningSequence SequenceVariant
PhaseEncodingDirection TotalReadoutTime EffectiveEchoSpacing DiffusionBValue
ConversionSoftware ConversionSoftwareVersion
```

Values may be scalar strings, integers, floats, booleans, or lists of those scalar types, provided JSON serialization rejects no nonfinite value. Nested unknown containers are excluded. Allowed strings are not semantically screened for identifying content. Hashes are not anonymization and may remain linkable. Source hashes, dates, IDs, directory paths, and inventory header dates must be treated as controlled research metadata.

Converter diagnostic filtering replaces entire lines matching patient-name/ID, accession, birth-date, physician, institution, station-name, or serial-number patterns, and truncates lines to 2,000 characters. It is heuristic: unlabeled values, identifiers split across lines, unknown fields, raw errors, and hard interruption before filtering can leave sensitive material. The log file is initially written in raw form during the converter run.

There is no encryption-at-rest layer, access-control service, consent enforcement, immutable audit store, secret scanner, or guaranteed removal of identifying data. Host permissions and research governance remain external. Archive paths and filesystem/state content are trusted operational inputs; this is not a hardened public-facing untrusted-upload service.

## 12. State, receipts, cache, and incremental performance

### 12.1 Persistent index and event schema

`Reports/.state/index.json` has top-level `version`, `sessions`, `sources`, and `issues`. A version other than 2 is rejected. This is a version check, not full JSON schema validation.

| Record | Principal fields |
|---|---|
| Session keyed by `subject/session` | `subject_id`, base `date`, full `label`, preparation `status`, `version`, canonical `archive`, archive signature/digest, DICOM content digest, counts/byte estimates, `extraction`, optional `conversion` |
| Source keyed by absolute path | `path`, `[size,mtime_ns]` signature, session `key`, outcome, `against` prior archive digest, schema version |
| Receipt descriptor | `version`, absolute `root`, receipt path and signature, absolute directory→mtime map, inventory path |
| Conversion descriptor additions | `status`, output path, `n_volumes`, prepared `source_sha256` content digest |
| Issue | `key`, stage, subject, session, reason, path |
| Event | Optional session replacement, source/sources upserts, issue clears/upserts; worker events also carry outcome/note/detail |

Preparation receipt files hold version, archive SHA-256, and a random receipt ID. Conversion receipt files hold version, a `complete` boolean, prepared content digest, and random receipt ID. A fast check uses stored receipt metadata rather than reopening and validating receipt JSON content.

### 12.2 Checkpoint sequence

Before applying an event to in-memory state, the coordinator atomically writes a small event journal under `.state/pending`. Event application upserts records and clears/adds issues. Approximately after ten seconds of checkpoint activity, and at orderly finalization, the full index is written and pending journals are removed. Startup replays sorted pending journals.

`atomic_json()` writes a unique sibling temporary file, flushes and `fsync`s it, then uses `os.replace`; leftover temporary files are removed in its `finally`. There is no parent-directory fsync or multi-file database transaction. Replay and publication are designed for practical recovery but are not a demonstrated all-crash/power-loss durability guarantee. Corrupted JSON/index/journal recovery tooling and comprehensive schema validation are absent.

### 12.3 Fast validation versus audit

Normal receipt validation compares schema version, receipt `[size,mtime_ns]`, and stored directory mtimes. Missing or changed managed directories invalidate cached work. It does not normally open the inventory or inspect every image. Metadata-preserving in-place image edits can go undetected. It does not cryptographically authenticate files, state, or receipts.

Audit additionally opens the inventory and checks every listed file's size and SHA-256. It verifies listed contents rather than performing a complete independent reconstruction of the file tree; directory timestamps participate in detecting additions/removals. Audit does not necessarily rehash a canonical source ZIP merely because it rehashes extracted/output files. Missing or manipulated inventories and malicious state are not a supported adversarial trust model.

Preparation's normal fast path additionally compares source signature, schema version, canonical source association, prior archive signature, and valid extraction state. Cached duplicate and failed source outcomes are tied to the prior archive digest to avoid indefinitely applying them to a changed canonical session.

Conversion matches its source digest to current prepared DICOM content. Its cache does **not** include converter executable checksum/version, package version apart from schema version, classification algorithm revision, sanitizer revision, relevant option fingerprint, or environment. Changing a converter or algorithm can leave successful old outputs cached until force is requested or schema/input/output validation changes. This is a production provenance decision requiring resolution.

### 12.4 Complexity and scaling limits

Normal reruns avoid image-content work, but are not constant-time. They still read the roster and index, enumerate ZIP-containing trees for preparation, stat cached receipt directories, build/sort plans, and regenerate complete reports/index. First-run receipts enumerate their new output tree. State and planning are in memory; the index is rewritten wholesale. Building each subject's state copy filters the session dictionary, which can add cost as subject/session counts increase.

The 1,000-session regression specifically prevents calls to archive hashing, header reading, preparation workers, and conversion workers. It uses synthetic metadata/placeholder archives and does not represent 1,000 real scans' filesystem layout. It reports elapsed time but does not assert a latency threshold, test remote filesystem contention, or establish behavior at 10,000–100,000 sessions.

## 13. Parallelism, locking, and resource behavior

`ThreadPoolExecutor` orchestrates subject tasks. Each task processes that subject's selected sessions serially and updates its local session view between preparation events. Different subjects can run concurrently. A single subject with many dates does not exploit all workers. Preparation uses thread-based Python orchestration and I/O; scaling is not assured for Python-heavy parsing. Conversion gains parallelism from independent external converter processes.

One task is submitted per subject, and completed events are consumed by the coordinator. A worker exception outside its normal result handling is surfaced through its future. The current code does not provide a separate reliable per-session failure event for every possible exception path.

`ProjectLock` opens a persistent project-root lock file and obtains a nonblocking one-byte Windows lock or Unix `flock`. The OS releases it when the process exits; the filename can remain. Prepare and convert share the lock. Dry runs do not acquire it. Advisory locks must function on the actual cluster filesystem and are honored only by cooperating processes.

The lock key is the project root, not every resolved storage root. Two different project roots pointing at shared report/output/staging roots are not necessarily mutually excluded. Custom storage aliases, multiple mount paths, network filesystem behavior, concurrent roster edits, and non-cooperating writers need explicit threat and deployment policies.

No distributed scheduler, memory budget, global disk reservation, adaptive backpressure, per-series task queue, or worker-output-size limit is implemented. The queue can retain diagnostics; converter log output is not bounded as a whole. `--workers` is a concurrency control, not a resource isolation mechanism.

## 14. Publication, interruption, and recovery semantics

`publish_tree()` moves a stage to a unique short sibling `.incoming_<token>`. On cross-device rename it copies the tree. If a destination exists it is renamed to `.previous_<token>`, then incoming is renamed to the destination. A caught final-swap failure attempts to restore the previous destination. After success the previous tree is removed and incoming cleanup is attempted.

The existing rollback test covers a simulated final-rename exception and restoration of the old content. It does not prove crash recovery at every boundary. Between renames the public destination can temporarily be absent. A hard kill can leave incoming/previous directories, a moved archive without a state event, or published output without updated state. There is no startup recovery scan dedicated to these abandoned swap directories. Copying publication across devices increases temporary space needs and exposes an additional interruption window.

| Interruption point | Current expectation / review boundary |
|---|---|
| Before source processing | Unprocessed discovery candidates remain for later runs |
| Preparation worker observes stop | Caught interruption event; stage cleanup attempted; later planning can retry |
| ZIP moved before result checkpoint | Canonical discovery may find it later; exact behavior depends on publication/cache residue |
| Conversion stop/timeout | Direct converter child terminated/killed; organized partials or stage retained; current issue recorded if event reaches coordinator |
| Event journal durable, index not flushed | Pending event replay on next startup |
| Index flushed, journal still present | Replay of upsert-style event is intended to tolerate repetition; needs fault-injection proof |
| Hard kill during directory swap | No unconditional rollback guarantee; remnants may need manual investigation |
| SIGTERM/Slurm termination/node loss | No dedicated SIGTERM checkpoint/child-process-group handler; do not equate with orderly Ctrl+C |

The worker loop catches `KeyboardInterrupt`, sets a stop event, waits for the pool, drains result events, flushes state, and refreshes reports. Some planning/configuration/initialization/finalization failures occur outside this graceful path. A complete run-history row is therefore not guaranteed for every process invocation.

Preserve state, inventories, and receipts during ordinary operation. State contains absolute paths. Moving a processed study, changing a schema version, or adopting already-existing output folders requires a deliberate migration; no safe automatic rebuild tool is implemented. Deleting state alone does not authorize overwriting folders without current ownership receipts.

## 15. Reporting and observability contract

| Artifact | Update/retention semantics |
|---|---|
| `pipeline.log` | Appended normal-run summaries/errors; rotates at 10,000,000 bytes, three backups |
| `latest_prepare.log` | Replaced each non-dry preparation invocation that reaches log setup |
| `latest_convert.log` | Replaced each non-dry conversion invocation; includes filtered converter details |
| `session_status.csv` | Full current table, atomically replaced through a sibling temporary file |
| `issues.csv` | Full current issue table, similarly replaced |
| `run_history.csv` | Appends one row after a run reaches normal/history finalization; no size rotation |
| `.state/index.json` and pending journals | Internal resume records; preserve, not a user-editable report |

Reports refresh roughly every fifteen seconds while the coordinator loop runs and again at finalization; progress messages are emitted roughly every thirty seconds. Timing can be delayed by blocking work. Log timestamps and run IDs use the executing host's local time without explicit timezone metadata; a run ID includes timestamp plus a random suffix.

`session_status.csv` fields are `subject_id`, `session`, `prepare_status`, `dicom_instances`, `series`, `conversion_status`, `converted_volumes`, `archive`, and `nifti_folder`. Rows are the union of roster base sessions and cached session keys. Thus processing only an explicit `_2` does not automatically mark the base-date row complete. Missing expected sessions appear as `awaiting_upload`. A stale conversion digest displays `needs_conversion`.

`issues.csv` fields are `key`, `stage`, `subject`, `session`, `reason`, and `path`. Issue keys are namespaced as preparation-source, conversion-session, or nonblocking date issues. Date issues remain visible but do not count toward blocking unresolved totals.

`run_history.csv` fields are `run_id`, `stage`, `outcome`, `seconds`, `completed`, `cached`, `duplicate`, `failed`, `held`, and `unresolved`. `cached` can include deliberately skipped failures, not just successful scans. `completed` counts work-item outcomes, not image volumes. `held` describes planning; `failed` describes received worker-result failures/interruption. A run ID is not included in every session's persistent provenance record.

No historical per-run directory is created during normal reporting. Historical detailed logs are overwritten/rotated, while run-history and source/state record counts can grow. There is no JSON log schema, metrics exporter, dashboard, tracing ID on every output, or external alert integration.

## 16. Existing verification and its practical limits

### 16.1 Recorded baseline verification

The preceding development session recorded 30 passing parameterized pytest cases against this deployed checkout on Windows/Python 3.12. These are recorded results, not a new test run performed for this document. The last recorded 1,000-session metadata regression took about 2.8 seconds for preparation and 2.9 seconds for conversion, with zero forbidden content-processing calls. Timing varied across local runs and is not a cluster service-level guarantee.

Wheel and source distribution builds succeeded locally; artifact-content checks and installed CLI/module smoke checks succeeded. A read-only plan accepted the existing local archives without processing them. This is not a real-study conversion validation of this package version. Earlier historical converter experiments do not substitute for a maintained integration suite against the present package.

### 16.2 Test traceability

| Behavior | Existing test names / evidence |
|---|---|
| Incremental preparation and numbered sessions | `test_prepare_incremental_and_suffix` |
| Exact/repackaged duplicates and conflicts | `test_duplicates_and_conflicts_are_cached` |
| Ordinary dry-run writes nothing; bad names held | `test_dry_run_writes_nothing_and_bad_names_held` |
| Metadata/header sentinel filtering, diffusion companions, cached conversion | `test_conversion_privacy_companions_and_fast_rerun` |
| Partial retention and explicit retry | `test_partial_outputs_retained_and_retry` |
| Deleted outputs invalidate receipts | `test_deleted_dicom_and_nifti_detected` |
| Output-root override invalidates inappropriate cache reuse | `test_output_root_override_does_not_reuse_other_root` |
| Lock exclusion and pending-event reload | `test_lock_and_pending_journal_recovery` |
| ZIP traversal names do not escape | `test_path_traversal_member_cannot_escape` |
| Cached broken ZIP failure and explicit retry | `test_failed_archive_not_retried_until_requested` |
| Zero converter output is incomplete | `test_zero_output_is_not_success` |
| Cooperative preparation interruption | `test_interrupted_preparation_remains_resumable` |
| Selected classifier rules and invalid NIfTI rejection | `test_sequence_categories_and_truncated_nifti` |
| New batch processes only new work | `test_new_batch_only_processes_new_session` |
| 1,000-session no-content-work rerun | `test_1000_cached_sessions_do_not_read_scan_contents` |
| CSV date formats/custom columns/leading zeros | `test_custom_roster_schema_preserves_ids` |
| Custom schema through both stages | `test_custom_schema_used_in_both_stages` |
| Default study root is current directory | `test_default_project_is_working_directory` |
| Caught publication-swap failure restores old data | `test_publish_rolls_back_if_swap_fails` |
| Missing/duplicate CSV columns | `test_ambiguous_or_missing_columns_rejected` |
| Older state rejected without rewriting the index | `test_old_state_rejected_without_writes` |
| Public help/version and source scripts | `test_public_cli_help`, `test_source_script_launchers` |

The simulated converter produces small known files and controlled diagnostics. This establishes orchestration behaviors, not the full correctness of dcm2niix or diverse image geometries. The suite is not a complete adversarial, chaos, property-based, privacy, or scientific validation suite. No coverage percentage or mutation score is established.

### 16.3 CI and packaging status

CI is configured for Ubuntu and Windows with Python 3.10 and 3.12, plus an Ubuntu packaging job that builds artifacts, runs twine checks and content checks, installs the wheel, and invokes CLI help. Configuration existence is not evidence that these jobs have run successfully on GitHub. There is no macOS job, actual Slurm job, real-converter job, or automated release publication.

The content check verifies selected forbidden directory names/extensions and permits only `examples/sessions.csv` CSV paths. It is a filename-based exclusion test, not semantic scanning for identifiers or a comprehensive ban on every sensitive file type. It expects exactly two build artifacts in `dist` and uses Python assertions; running it with optimized Python can disable those assertions. Git ignore rules do not remove already tracked data. Custom output roots inside the checkout need additional exclusion rules.

## 17. Production-review register

These are starting questions for rigorous review. Static concerns need reproduction and impact assessment before becoming implementation tasks; entries are not exhaustive or a predetermined architecture.

| ID | Concern / decision | Why it matters | Evidence/status |
|---|---|---|---|
| R01 | Crash consistency across archive move, tree swap, checkpoint, and cleanup | Data availability and recoverability at abrupt termination | Multi-step source sequence; only caught-swap rollback tested |
| R02 | Advisory locks across shared/custom roots and filesystem aliases | Two nominal projects can race on shared storage | Lock derives from project root; deployment policy unresolved |
| R03 | Scientific NIfTI correctness and sanitizer field preservation | Structurally readable output can still be scientifically wrong | Minimal manual header checks; narrow synthetic images |
| R04 | Cache provenance excludes converter/config/algorithm identity | Upgrades can silently reuse stale outputs | Static inspection of successful-cache criteria |
| R05 | Canonical archive replacement policy | Same-path changed input can replace prepared session contents | Explicit `replacement` branch; must be made intentional and tested |
| R06 | Partial transfer detection | Stable metadata during a read is not an upload-complete protocol | Size/mtime checks only; no atomic upload manifest or readiness marker |
| R07 | Archive/resource abuse | Excessive decompressed size, file counts, CPU or log output can exhaust a node | No configured expansion/object/log quota |
| R08 | Privacy boundaries of selected strings, raw logs, failures, paths, images | Filtering is not comprehensive anonymization | Whitelist types and heuristic redaction only |
| R09 | State corruption, migration, and relocation | Damaged or moved operational data lacks supported repair path | Version check and JSON parsing; no migration/recovery command |
| R10 | Audit and dry-run semantics | Operator expectation of no content reads conflicts with option combination | Direct planner call path with `cfg.audit` |
| R11 | Termination and descendant process cleanup | Slurm expiry can orphan work or leave uncertain status | Ctrl+C/direct-child handling; no dedicated SIGTERM/process-group policy |
| R12 | Multi-study/multiframe/non-image/duplicate-instance policy | Counts and outputs may not match assumptions about a scan session | Broad acceptance, UID presence checks, no automatic split |
| R13 | Diffusion integrity and heuristic labels | Valid floats and substring labels do not prove analysis readiness | Shape/count/orientation and scanner vocabulary not validated |
| R14 | Symlink/path/case/reserved-name races | Cross-platform safety exceeds basic ID grammar | Partial link filtering and resolved-path checks; deeper cases untested |
| R15 | Long-run scale and metadata I/O | Fast 1,000-session local rerun may not predict shared-filesystem operation | Whole-index/report rewrites and in-memory planning |
| R16 | Reporting completeness and bounded retention | Early failures may lack a history row; retained failures grow | History after finalization; no total retention budget |
| R17 | Ownership receipt trust and tamper model | A marker file's presence is not authenticated ownership | Marker existence gate and metadata validation |
| R18 | Missing canonical archives and orphan state | Prepare discovery and convert index have different source requirements | No canonical-archive requirement in conversion selection |
| R19 | Claimed support versus observed support | Installation success does not imply scanner/platform compatibility | Sparse environment matrix and simulated converter |
| R20 | Release/license/security maintenance policy | A distributable artifact still needs an accountable support contract | No selected license or release/security process established |

## 18. Proposed stress-test and validation program

This is a proposed program for EGM to refine, prioritize, and turn into approved acceptance tests. It is not a claim that these cases currently pass. Prefer synthetic or explicitly approved de-identified fixtures; do not supply real participant data to reviewers by default.

| Test group | Experiments | Required observations / candidate pass condition |
|---|---|---|
| T01 Identity grammar | Property-based valid/invalid IDs, ambiguous numeric/underscore segments, leap days, suffix limits, case collisions, Windows device names, long paths | Deterministic key parsing; no path escape or cross-subject collision; documented platform policy |
| T02 CSV robustness | BOM, blank/duplicate rows, invalid dates, duplicate headers, extra/missing columns, malformed quoting, concurrent edits, very large rosters | Explicit parsing policy, no silent identity reassignment, controlled errors and reproducible authorization |
| T03 Archive eligibility | Extensionless objects, misleading suffixes, corrupt headers, encrypted ZIP, ZIP64, nested ZIP, duplicate member paths, non-image SOPs, absent UIDs | Accepted/skipped/failed populations match a documented policy and retain evidence |
| T04 Archive integrity/resources | Bad CRC in accepted and skipped members, truncated transfer, concurrent source change, decompression bombs, huge central directory, many tiny members | Bounded resource usage, explicit failure, intact source, no unintended publication |
| T05 Duplicate/conflict matrix | Exact and repackaged copies, ancillary-only changes, duplicate instances, subset/superset series, changed anonymized headers, same-path replacement, explicit `_2` | No silent merge or unintended overwrite; policy is reproducible with explanation |
| T06 Fast cache and audit | Normal rerun, content edit preserving metadata, deleted/added files, missing receipt/inventory, corrupted state, force flags, audit+dry-run | Distinguish metadata trust from verified integrity; normal rerun performs no image-content work; audit detects defined tampering |
| T07 Crash injection | Kill before/after ZIP rename, each publication rename, index replace, journal write/deletion, report replace, and cleanup | Every state reconciles to prior valid output or a recoverable new result with documented repair steps |
| T08 Process control | Converter hang, timeout, child spawning, huge logs, nonzero exit with good files, SIGINT/SIGTERM/SIGKILL, scheduler time limit | No false success; bounded shutdown; documented descendants and durable completed-session recovery |
| T09 Filesystem faults | No space/inodes, quota, permission revocation, EXDEV, read-only roots, rename failure, cleanup failure, network disconnect | Source/old valid output preserved; clear status; no unrelated deletion; space accounting documented |
| T10 Concurrent operations | Two real processes for one project; prepare versus convert; distinct roots sharing reports/output; dry-run during writer; two compute nodes | Lock policy proven on supported storage; no corrupt index/report or cross-project replacement |
| T11 Scientific geometry | Trusted real converter with known phantoms; both endians; NIfTI-1/2; 2D/3D/4D/multiframe; qform/sform/scaling; nontrivial voxel values | Independent library verifies dimensions, transforms, scaling, data values, and allowed sanitizer changes |
| T12 Diffusion | Missing/malformed bval/bvec, 3×N and N×3, mismatched N, nonfinite/negative values, rotated gradients | Explicit supported conventions and scientific validation, not numeric parsing alone |
| T13 Classification | Labeled scanner/protocol corpus, overlapping keywords, SWI/minIP/phase, contrast labels, unfamiliar sequences | Human-reviewed confusion matrix and conservative fallback policy; no hidden rule-order assumptions |
| T14 Privacy | Sentinels in every header text field, allowed strings, image pixels, ZIP paths, exception messages, raw/partial logs, source provenance | Field/channel-specific disclosure policy verified; no unsupported de-identification claim |
| T15 Scale | 1k/10k/100k metadata sessions, many subjects versus one subject, realistic directories, slow Lustre/NFS, accumulating sources/issues/history | Measure p50/p95 latency, stat/open calls, RAM, report/index size, I/O, worker scaling, and retained disk use |
| T16 Packaging/release | Fresh install, sdist→wheel round trip, minimum/latest allowed dependencies, Windows/Linux/macOS as claimed, dirty checkout with planted private files | All supported environments tested, no private fixture in artifacts, correct metadata and entry points |
| T17 Migration | Schema upgrade, moved project, changed storage root, missing old binary, existing unowned outputs | Reversible, explicit migration or refusal; no instruction to delete valuable state/output blindly |
| T18 Observability | Early abort, partial failure, retry, interruption, reused cache, date warning, orphan output | Tables/logs/history accurately distinguish attempt, session, volume, warning, and completed state |

Benchmark acceptance thresholds must be agreed before optimization claims. At minimum, measure elapsed time and system calls separately from expensive content work; a no-content-read invariant is more portable than promising a particular number of seconds on every cluster. Set explicit budgets for memory, disk expansion, log volume, and maximum shutdown latency.

## 19. Intended invariants to preserve and prove

These are candidate top-level requirements for the next design. Some are partially implemented; approval should explicitly resolve exceptions described above.

1. **Identity:** subject and session association is deterministic from authorized filenames, with no inference from identifying patient fields.
2. **Input preservation:** processing never changes ZIP contents; archive relocation and any canonical replacement policy are explicit and recoverable.
3. **No false completion:** a session is not reported as successfully converted when converter execution, required outputs, or scientific validation fail under the chosen policy.
4. **Isolation:** a worker cannot delete or publish into another subject/project's managed tree; concurrency cannot corrupt shared state.
5. **Recoverability:** interruption at every persistence boundary has a documented, tested reconciliation path.
6. **Incrementality:** unchanged successful work does not perform image-content reads or converter calls in ordinary mode; full audit is an explicit, accurately described cost.
7. **Provenance:** outputs can be traced to input contents, tool/version/configuration, and processing attempt under an agreed reproducibility policy.
8. **Privacy honesty:** retained identifiers and channels are documented, with no claim of de-identification beyond tested controls.
9. **Scientific integrity:** geometry, scaling, voxel values, and companion relationships survive conversion and sanitization within a validated tolerance/format policy.
10. **Operational clarity:** expected, uploaded, prepared, complete, partial, failed, stale, and intentionally skipped work have distinct understandable reporting.

## 20. Proposed release gates and unresolved decisions

| Gate | Evidence needed before claiming a production-supported release |
|---|---|
| G01 Product contract | Approved input types, scanner/converter/platform matrix, identity and replacement policy, explicit non-goals |
| G02 Safety/recovery | Crash/fault matrix completed; repair runbook; concurrent-process/filesystem lock tests on supported storage |
| G03 Scientific validation | Independent geometry/voxel checks and representative real-converter fixtures; diffusion/classification limits documented |
| G04 Cache/provenance | Approved version/config cache fingerprint and invalidation/migration behavior |
| G05 Privacy/security | Defined trust model, sensitive-channel tests, resource limits, secure disclosure and data-handling guidance |
| G06 Performance | Agreed realistic scale targets and measured budgets on target shared storage |
| G07 Compatibility/release | Green reproducible CI, clean-install/build tests, package metadata, license choice, versioning/deprecation/release policy |
| G08 Operations/docs | Troubleshooting, backup/restore, relocation/migration, partial cleanup, scheduler interruption, and pilot checklist |

Decisions still required include: which exact environments are supported; whether uploads must use atomic rename/readiness markers; whether canonical ZIPs are immutable; how to handle same-study partial overlap; whether conversion failures preserve last-good state separately; which NIfTI fields may be cleared; what diffusion and classification guarantees are in scope; how to handle old state; whether a transactional index is warranted at the expected scale; which timezones/retention policies to use; and which license/support obligations the owner wants.

None of these decisions should be silently settled by a reviewer. EGM findings should identify consequences, options, evidence, and a recommendation for owner approval.

## 21. Handoff instructions for EGM reviewers

Start from the companion concepts brief and this specification. Provide reviewers the repository's public source files and manifest. Ask them to distinguish implementation defects from unclear requirements and missing evidence. Use fresh context for independent review, as described by EGM; do not describe this document's preparation as a completed Goldfish review.

For each finding request: identifier, severity, affected invariant, exact source function/path, concrete triggering input or interruption sequence, observed or predicted result, confidence/evidence, recommended design alternatives, and an executable regression/acceptance test. Explicitly request both false-success/data-loss findings and cases where this document overstates current behavior.

Review the current implementation first, then draft a separate production-hardening design. Keep the baseline immutable or versioned so proposed behavior is not confused with current behavior. Changes should be traceable from finding to decision to test to implementation. A reviewer finding is not by itself authorization to process private data, delete state, publish a release, or change the scientific contract.

## Appendix A. Source navigation anchors

Line numbers refer to the hashed baseline and can drift after edits. Function names are the durable navigation aid.

| Source location | Inspection target |
|---|---|
| `pipeline.py:37–51` | Schema, filename/ID regexes, skipped suffixes, technical fields, DICOM tags |
| `pipeline.py:54–124` | Atomic JSON, signatures/hashing, owned paths, tree publication |
| `pipeline.py:127–173` | Filename parsing and roster loading |
| `pipeline.py:176–240` | Locking and state/journal persistence |
| `pipeline.py:243–323` | Logging, reports, receipt checks and construction |
| `pipeline.py:326–377` | CLI stage parser and root/resource configuration |
| `pipeline.py:380–417` | Classifier and archive discovery |
| `pipeline.py:420–617` | DICOM extraction, archive move, preparation and planning |
| `pipeline.py:620–749` | NIfTI sanitization, metadata filtering, output organization |
| `pipeline.py:752–836` | Diagnostic redaction and converter session execution |
| `pipeline.py:839–1009` | Conversion planner, subject worker, entry point, coordinator and final reports |

Every `pipeline.py` anchor above means `src/mri_dicom_pipeline/pipeline.py`. Consult `tests/test_pipeline.py` and `tests/test_project_configuration.py` for the named test implementations. Build and CI contracts are in `pyproject.toml`, `MANIFEST.in`, `.github/workflows/ci.yml`, and `tests/check_distribution.py`.

## Appendix B. Suggested baseline verification commands

Run in a clean development environment using synthetic fixtures:

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m build
python -m twine check dist/*
python tests/check_distribution.py
mri-dicom-pipeline --help
mri-dicom-pipeline prepare --help
mri-dicom-pipeline convert --help
```

Use a fresh/controlled `dist` directory for the artifact checker; its current implementation expects one wheel and one source archive. A successful local command sequence does not replace platform CI, real-converter validation, cluster filesystem tests, or review of the production gates.
