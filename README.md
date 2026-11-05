# mriDICOMpipeline

Two commands, designed for repeated batches on one allocated compute node.
Python 3.10+ and `pydicom` are required. Conversion also requires `dcm2niix`
on PATH, or an explicit `--dcm2niix-bin /path/to/dcm2niix`.

## Installation and first pilot

Install from a source checkout into an activated Python environment:

```bash
python -m pip install .
```

Keep study data separate from the code checkout when starting a new project.
Create `sessions.csv` in your study directory using the synthetic example in
`examples/sessions.csv`. Place incoming archives in `DicomUploads/Uploads`.
Run from that study directory, optionally inside one Slurm compute allocation:

```bash
mri-dicom-pipeline prepare --dry-run
mri-dicom-pipeline prepare --limit 1
mri-dicom-pipeline convert --limit 1
```

Use `--project-root /path/to/study` on each command to select another study.
The default is the current working directory, including after installation;
data is never implicitly written into the installed package. Relative path
arguments are resolved against the current working directory.
`python -m mri_dicom_pipeline` provides the same command interface.

After inspecting Reports/pipeline.log and the pilot NIfTI files:

```bash
mri-dicom-pipeline prepare
mri-dicom-pipeline convert
```

Optional Bash equivalents are `bash Scripts/prepare_dicoms.sh` and
`bash Scripts/convert_dicoms.sh`. They pass all arguments through unchanged.
No account, partition, constraint, or memory requirement is hardcoded. These
commands do not submit job arrays. One optional single-node sbatch launcher
can run the same commands, using your cluster's normal resource settings.

## Input and identity

```text
study/
  sessions.csv
  DicomUploads/
    Uploads/                         # incoming ZIPs at any nesting depth
    SubjectsSorted/S001/
      S001_20260725.zip
      S001_20260725_2.zip
  DicomExtracted/Subjects/S001/
    20260725/dicom/00000001.dcm
    20260725_2/dicom/00000001.dcm
  Niftis/Subjects/S001/
    20260725/ADC/S001_20260725_ADC_<stable-token>.nii.gz
    20260725_2/ADC/S001_20260725_2_ADC_<stable-token>.nii.gz
```

ZIP names determine identity. Dates are eight digits, YYYYMMDD. An explicit
`_2` suffix means a distinct second session on that date and is carried through
all outputs/checkpoints. Higher integer suffixes are also accepted. Wrapper
directory names do not determine subject identity.

The reference CSV defaults to `sessions.csv` and requires `subject_id` and
`scan_date`. Dates may be YYYYMMDD, YYYY-MM-DD, or M/D/YYYY. IDs may contain
letters, digits, hyphens, and underscores; leading zeros are preserved.
To use a different file or column names on either stage:

```bash
mri-dicom-pipeline prepare --reference-csv /data/visits.csv --subject-column participant --date-column acquired
mri-dicom-pipeline convert --reference-csv /data/visits.csv --subject-column participant --date-column acquired
```

Blank rows are ignored; repeated pairs are
collapsed. Invalid reference rows are logged and ignored without editing the
CSV. IDs without dates still register the subject, but do not authorize an
unlisted date. Unknown IDs and dates are held in Uploads for review. For an
intentionally unlisted date belonging to a known ID, pass
`--allow-unlisted-dates` to both commands or update the reference.

Preparation moves accepted ZIPs into SubjectsSorted. It never modifies ZIP
contents. Viewer software, previews, DICOMDIR indices and non-DICOM files are
omitted from extracted output; **extensionless DICOM objects are supported**.
Archive member paths are not used as extraction paths. The original ZIP
retains all ancillary files. ZIP members are streamed to completion to check
their CRCs; obviously short uncompressed image objects are also rejected.
This cannot establish whether the original export included every acquired
image. Nested ZIP containers and loose, already-unzipped exports are not
accepted in this version: supply the original session ZIPs.

Header dates are cross-checks only; a mismatch is recorded as a warning.
Subject identity never comes from patient identification fields or a matching
spreadsheet. Generated sidecars include selected technical fields and the
validated filename identity. Free-text source descriptions are used for the
heuristic scan classifier but are not copied into published metadata.

## Fast repeated runs

The first run hashes each new ZIP and inventories its DICOMs. Later preparation
runs scan **ZIP paths under Uploads/SubjectsSorted only**, compare size/mtime
with cached state, and check small completion records/directory timestamps.
They do not enumerate or read old DICOM files. Conversion selects sessions
from the cache; it does not perform another Python DICOM header pre-scan.

Unchanged successful sessions are skipped. Missing output directories and
file deletions/renames in managed output directories invalidate their receipts.
In-place edits to existing files that preserve directory timestamps are not
detected by this fast check. Managed outputs should be treated as immutable.
Use `--audit` for an explicit, slower per-file content check. Keep
`Reports/.state` and the hidden receipt/inventory files in each session;
deleting these removes the fast-resume information. Keep state with its
project at its existing absolute paths; relocation requires rebuilding it.

Unchanged failures/partial conversions are recorded and **not retried by
default**, so repeated batches do not continually reprocess a known bad scan.
After addressing the cause, pass `--retry-failed`. New/changed inputs are
considered automatically. Interrupted, unfinished work is retried automatically
on the next invocation. `--force-reextract` and `--force-reconvert` explicitly
bypass the relevant successful caches. `--limit N` selects work after cache
checks, so a pilot can reach new scans beyond already completed ones.

An exact repeat ZIP is skipped. A differently packaged ZIP containing the
same multiset of DICOM file contents is also skipped. Its incoming copy stays
in Uploads and its outcome is cached; later runs do not repeatedly hash it.
Different DICOM contents with the same session name are held, preserving both
archives. Resolve the naming explicitly, for example `_2` for a second
session. Different anonymized headers can make the same acquisition appear
different; the pipeline never guesses that such inputs should be merged.

## Parallel work and recovery

Each worker handles one subject at a time; dates within that subject run
serially. Conversion workers launch independent dcm2niix processes, so they
use multiple CPUs despite Python's thread-based orchestration. The default
is min(4, SLURM_CPUS_PER_TASK), or one worker outside Slurm. Set `--workers N`
to choose explicitly; it cannot exceed SLURM_CPUS_PER_TASK when that variable
is present. Converter math threads are capped at one; staging is uncompressed
and the pipeline performs gzip compression once per volume. More workers
require more memory and temporary disk space. Benchmark your node before
increasing concurrency. The same settings apply to preparation workers.

One coordinator writes state, CSVs and permanent logs. Every completed session
has a durable result journal; the compact index is updated periodically and
on exit. Journals are replayed after interruption. An OS project lock prevents
concurrent prepare/convert invocations for the same project. This depends on
the cluster filesystem providing working advisory locks; verify in the pilot.

Outputs are staged in an isolated directory, validated, then published through
a sibling directory swap. Existing successful session outputs remain in place
if a new conversion fails. NIfTI header text/extensions are cleared while
geometry and pixel bytes are retained; JSON uses an explicit technical field
list. Matching .bval/.bvec companions are renamed with their image. All
converted series/components are retained; no automatic best-scan selection
is performed. Classification is a heuristic, including ADC before DWI and
separate post-contrast T1 and SWI phase categories; review before analysis.

A nonzero converter exit, timeout, no valid volumes or unresolved artifacts
cannot become a successful cache entry. Valid partial output goes under
`Niftis/.pending/<id>/<session>/...`; original converter working files are kept
and their path is written to issues.csv. These exceptional directories need
manual review/cleanup once resolved; nothing sweeps other workers' staging
directories. Successful temporary work is cleaned up. The default working
root is project/.work. `--staging-root` on conversion can select node-local
scratch; failed scratch files may be removed by the cluster after the job,
while published partial volumes remain in Niftis/.pending.

## Where to look

No timestamped report folder is created. Both commands print these paths:

| File | Purpose |
|---|---|
| Reports/pipeline.log | Main run summaries, important events and errors; rotates at 10 MB with three backups |
| Reports/latest_prepare.log | Latest preparation details; replaced each invocation |
| Reports/latest_convert.log | Latest conversion details, including filtered converter diagnostics |
| Reports/session_status.csv | All expected sessions plus explicit additional sessions; current prepare/conversion state |
| Reports/issues.csv | Unresolved problems, with subject/session and relevant path |
| Reports/run_history.csv | One compact row per completed/interrupted invocation |
| Reports/.state | Internal persistent index and result journals; preserve it |

Start with pipeline.log, then issues.csv. Date mismatch warnings do not block
conversion. Other unresolved problems produce exit status 2. Success is 0;
an orderly keyboard interruption returns 130. `--dry-run` writes no project
data or logs, hashes no archives, and performs no conversion. Normal report
refreshes use cached metadata; they do not reopen all scans.

## Legacy cluster data

The old scripts/state and outputs are not trusted as new completion records.
For ZIPs still in DicomUploads/Subjects, explicitly preview and import:

```bash
mri-dicom-pipeline prepare --import-legacy --dry-run
mri-dicom-pipeline prepare --import-legacy
```

Only this legacy location accepts old ID_MMDDYY ZIP names, interpreted as
20YY and validated against the reference before normalization. Successful
imports move to SubjectsSorted using YYYYMMDD and build the new cache once.
Legacy extracted/NIfTI directories are left in place. Normal repeated runs
after that initial import use the fast path. Old report directories, saved
tree listings and historical specifications are not read by the new scripts
and should stay outside a public repository.

## Development and validation

```bash
python -m pip install -e ".[test]"
python -m pip install build
python -m pytest
python -m build
```

Tests create synthetic DICOM archives and a simulated converter. They cover
failure/recovery, configurable CSV schemas, command entry points, metadata,
and a 1,000-session cache regression that forbids archive hashing, DICOM
header reads, and converter calls on reruns. CI is configured for Linux and Windows
with Python 3.10 and 3.12, and checks the built distribution contents.

This is research workflow software. Review a small pilot on your target
cluster before processing a full study: check advisory locking, real scanner
coverage, conversion geometry, sequence classification, and resource use.
The included tests use a simulated converter; target-system dcm2niix validation
is still required. Outputs are not BIDS datasets or certified de-identified
data. DICOMs retain their original headers and pixel data; selected metadata
filtering does not remove identifiers burned into image pixels.

## Repository and data separation

The package source lives in `src/mri_dicom_pipeline`. `Scripts` retains the
two source-checkout Python launchers and Bash wrappers; copy the whole
checkout or install the package when deploying, since `Scripts` alone is
no longer self-contained. Commands never submit cluster jobs or hardcode an
account, partition, institution, or study.

The ignore rules exclude local reference CSVs, imaging archives, extracted
images, NIfTIs, reports, state, temporary work, PDFs, and saved directory
listings. Only the synthetic CSV under `examples` belongs in Git. Custom
storage roots should live outside the source checkout or have their own
ignore rules. Verify `git status --short --untracked-files=all` before each
commit. Ignore rules do not remove files already tracked by Git.

State schema version 2 uses `subject_id`, `.pipeline_complete.json` receipts,
and `.mri_dicom_pipeline.lock`. Earlier schema versions are rejected before
processing. Preserve old state and outputs; rebuilding them requires a
deliberate migration. The commands refuse to overwrite session directories
that lack the current ownership receipt. Cached state stores absolute paths,
so moving a processed study also requires a planned cache/output migration.

Before public distribution, choose a repository license and add its LICENSE
file and package metadata. No license has been selected on the owner's behalf.
