# mriDICOMpipeline

Two commands, designed for repeated batches on one allocated compute node.
Python 3.11+ and `pydicom` are required. Conversion also requires `dcm2niix`
on PATH, or an explicit `--dcm2niix-bin /path/to/dcm2niix`. Linux and Windows
are supported; macOS is not tested. Operator procedures (first production run,
resolving holds, crash recovery, Slurm settings) are in
[`docs/runbook.md`](docs/runbook.md).

## Installation and first pilot

Install from a source checkout into an activated Python environment:

```bash
python -m pip install .
```

Keep study data separate from the code checkout when starting a new project.
Create `sessions.csv` in your study directory using the synthetic example in
`examples/sessions.csv`. Place incoming archives in `DicomUploads/Uploads`;
ZIPs that already have their final name (`S001_20260725.zip`) can also go
directly into `DicomUploads/SubjectsSorted/S001/`, where they are prepared in
place.
Keep a copy of every ZIP outside the project before the first run.
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

`--session SUBJECT/SESSION` (repeatable, both commands) limits a run to the
named sessions, for example `--session S001/20260725`.

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
CSV; a row with a valid ID and an invalid date still registers the ID, like a
row without a date. IDs that differ only by letter case (`S001` and `s001`) would share
folders on Windows, so every spelling of such an ID is ignored, with a warning
naming the rows. IDs equal to a Windows device name (`CON`, `PRN`, `AUX`,
`NUL`, `COM0`-`COM9`, `LPT0`-`LPT9`, in any case) are rejected. IDs without
dates still register the subject, but do not authorize an unlisted date.
Unknown IDs and dates are held in Uploads for review. For an intentionally
unlisted date belonging to a known ID, pass `--allow-unlisted-dates` to both
commands or update the reference. `prepare` checks the reference only for ZIPs
it is about to work on; `convert` checks every session again before converting
it.

### Uploads and canonical ZIPs

Do not run `prepare` while copies into `Uploads/` are in progress, or copy each
ZIP under a temporary name (not ending in `.zip`) and rename it when the copy
is done. A partly copied ZIP fails to open, and it is retried automatically
once its size or modification time changes.

Preparation moves accepted ZIPs into SubjectsSorted. It never modifies or
deletes a ZIP, and a ZIP it cannot accept stays where it is, with an issue.
**An accepted ZIP in SubjectsSorted is immutable:** if different bytes appear
at that path (for example a re-export copied over it), the session is held
and its extraction and NIfTI outputs stay unchanged. A corrected export
becomes a new `_N` session. The same bytes with a new file time (a restore
from backup) are accepted without extracting again. While such a hold is open,
each run hashes that ZIP again, and the hold clears once the original bytes
are back. `--force-reextract` never accepts changed bytes. A prepared session whose canonical ZIP disappears gets
a nonblocking `missing:` issue; conversion continues from the extracted copy.

Viewer software, previews, DICOMDIR indices and non-DICOM files are omitted
from extracted output; **extensionless DICOM objects are supported**. Archive
member paths are not used as extraction paths. The original ZIP retains all
ancillary files. ZIP members are streamed to completion to check their CRCs;
obviously short uncompressed image objects are also rejected, and so is a
member with the `DICM` mark or a `.dcm` name that cannot be read as an object
(for example one cut off inside its header), or an MR image object without
Rows or Columns. Objects that
repeat byte for byte are stored once. A ZIP is held when two objects share a
SOP Instance UID but differ, or when it holds more than one study; accept a
genuine multi-study session with `--accept-multi-study SUBJECT/SESSION`.
This cannot establish whether the original export included every acquired
image. Nested ZIP containers and loose, already-unzipped exports are not
accepted in this version: supply the original session ZIPs.

Archives are assumed to come from your own scanner exports; they are parsed
in-process. Before extracting, `prepare` checks that the ZIP's contents fit on
disk with a reserve to spare: 5% of the filesystem or 50 GiB, whichever is
smaller. A ZIP that does not fit is left in place with an issue. On
quota-managed storage, check your quota as well; the check sees only free
space.

Header dates are cross-checks only. A StudyDate that differs from the
ZIP-name date is recorded as a warning. A StudyDate 5 or more days away holds
the ZIP until its name is corrected, or until the date is accepted with
`--accept-date-mismatch SUBJECT/SESSION`.
Subject identity never comes from patient identification fields or a matching
spreadsheet. Generated sidecars include selected technical fields and the
validated filename identity. Two date fields sit side by side so they can be
compared: `session_date` is the date in the ZIP name, and `dicom_study_date`
is the StudyDate read from the session's DICOM headers (normally one date;
several are comma-separated; `null` when the headers carry none). Free-text
source descriptions are used for the heuristic scan classifier but are not
copied into published metadata.

## Fast repeated runs

The first run hashes each new ZIP and inventories its DICOMs. Later preparation
runs scan **ZIP paths under Uploads/SubjectsSorted only**, compare size/mtime
with cached state, and check small completion records/directory timestamps.
They do not enumerate or read old DICOM files. Conversion selects sessions
from the cache; it does not perform another Python DICOM header pre-scan.

Unchanged successful sessions are skipped. Missing output directories and
file deletions/renames in managed output directories invalidate their receipts.
In-place edits to existing files that preserve directory timestamps are not
detected by this fast check. Use `--audit` for an explicit, slower per-file
content check; with `prepare`, it also re-hashes canonical ZIPs, so an edit
that kept a ZIP's size and time is caught. Keep `Reports/.state` and the hidden
receipt/inventory files in each session; deleting these removes the
fast-resume information.

State keeps absolute paths. **Moving a project is not supported yet:** when no
recorded ZIP lies under the configured uploads folder, both commands refuse to
run and say the project appears moved.

Unchanged failures/partial conversions are recorded and **not retried by
default**, so repeated batches do not continually reprocess a known bad scan.
After addressing the cause, pass `--retry-failed`. When the cause is in the
data itself (for example an aborted diffusion run whose last volume is
incomplete, which makes dcm2niix exit with an error), review the issue and
accept the session with `convert --accept-partial SUBJECT/SESSION`: it is
converted again and published with every valid volume, the accepted errors
are recorded in its receipt and in `session_status.csv`, and later runs treat
it as done. A timeout or a session with no valid volume is never accepted.
New/changed inputs are
considered automatically. Interrupted, unfinished work is retried automatically
on the next invocation, except an interrupted explicit reconversion.
`--force-reextract` and `--force-reconvert` explicitly bypass the relevant
successful caches. `--limit N` selects work after cache checks, so a pilot can
reach new scans beyond already completed ones.

An exact repeat ZIP is skipped. A differently packaged ZIP containing the
same set of DICOM file contents is also skipped. Its incoming copy stays
in Uploads and its outcome is cached; later runs do not repeatedly hash it.
Different DICOM contents with the same session name are held, preserving both
archives. Resolve the naming explicitly, for example `_2` for a second
session. Different anonymized headers can make the same acquisition appear
different; the pipeline never guesses that such inputs should be merged.

## What "converted" means

A session is `converted` only when dcm2niix exited 0, **every required series
has at least one valid volume**, nothing failed to organize, and no unexpected
files remain. Required series are those holding MR image objects; other
series (for example Siemens CSA Non-Image objects or reports) stay in
`DicomExtracted/` and are listed as excused. A session that misses a required
series is `partial`, even when dcm2niix exits 0 (it does so for some series it
cannot decode).

The pipeline checks its own handling: published NIfTI files decompress to
exactly the bytes dcm2niix wrote, companions are copied intact, and required
series are accounted for. **Whether dcm2niix converts correctly (orientation,
slice order, spacing, scaling, voxel values) is dcm2niix's responsibility.**

**A complete session is never reconverted automatically.** If its folder is
changed or disappears, the session gets an `output modified` issue and is left
as it is. Restore the folder and run `convert --audit --session KEY` to
revalidate it, or reconvert it explicitly with
`--force-reconvert --session KEY`. An explicit reconversion refuses a folder
holding files that are not in its inventory, so files people add are never
deleted. If a reconversion fails, the complete folder and its record stay in
place; the failed attempt is reported next to it.

Each conversion records its provenance in its receipt and state: package
version, run ID, converter path and options, and converter versions. Nothing
compares provenance with the current environment, so outputs made with an
older dcm2niix stay `converted`; reconvert explicitly after an upgrade if you
need new outputs.

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

One coordinator writes state, CSVs and permanent logs. Every result has a
numbered journal; the index is saved periodically and on exit, with the
previous index kept as `index.json.bak`. After a crash, journals newer than
the saved index are replayed, so no session rolls back. Each command locks
every storage folder it touches (uploads, DICOM, NIfTI, reports, staging), so
two commands that share any folder cannot run at once. The lock depends on
the filesystem's advisory locks; if runs start from more than one machine on
shared storage, perform the two-node check in the runbook first.

Outputs are staged in an isolated directory, validated, then published through
a sibling directory swap. Each publication is recorded first; after a crash,
the next run rolls back an interrupted swap, redoes unfinished work, or keeps
a published folder only after checking every listed file. JSON uses an
explicit technical field list. Matching .bval/.bvec companions are renamed
with their image. A DICOM overlay plane (graphics stored with the images,
group 6000) comes out of dcm2niix as a 0/1 mask without a sidecar; it is
published next to its image as `<image name>_ROI1.nii.gz` (`_ROI2` and so on
for further planes). In the same way, a series with uneven slice spacing also
yields `<image name>_Eq_1.nii.gz`, a copy resampled to even spacing (the same
rule covers dcm2niix's `_Tilt_1` gantry-tilt copies, not reproduced with MR
test data). Such a session converts, with a nonblocking `spacing:` issue
saying a slice may be missing. A GE diffusion series that holds the combined
image next to the direction images yields `<image name>_ADC.nii.gz`, published
the same way. A Siemens physiological log (`…_PhysioLog`) becomes two
`_physio` files and no image; they are not published (the DICOM object stays
in DicomExtracted). All converted series/components are retained; no automatic
best-scan selection is performed. Classification is a heuristic that matches
whole words in the series description (revision 2: for example `AX T2* GRE`
is GRE, `SWI mIP` is SWI_MINIP, `HEADCOIL T1` is T1); review before analysis.

SIGTERM (for example a Slurm time limit) and Ctrl+C stop the run in order:
converters are stopped (on Linux with every process they started), sessions
still running are recorded as interrupted with their working files kept, and
nothing more is published. The exit code is 143 after SIGTERM and 130 after
Ctrl+C. On Windows, processes started by the converter are not stopped.

A nonzero converter exit, timeout, no valid volumes or unresolved artifacts
cannot become a successful cache entry. Valid partial output goes under
`Niftis/.pending/<id>/<session>/...`; original converter working files are kept
and their path is written to issues.csv. These exceptional directories need
manual review/cleanup once resolved (see the runbook); nothing deletes them
automatically. Successful temporary work is cleaned up. The default working
root is project/.work. `--staging-root` on conversion can select node-local
scratch; failed scratch files may be removed by the cluster after the job,
while published partial volumes remain in Niftis/.pending.

## Privacy

The outputs carry identifiers and dates through these channels, and the
pipeline does not de-identify any of them:

* file and folder names (subject ID and session date);
* JSON sidecar fields (a selected technical list; no patient names, IDs or
  free text);
* NIfTI header text, which dcm2niix fills with fields such as
  `TE=…;Time=…` (the time of day), and NIfTI header extensions (the tested
  dcm2niix versions write none);
* the original DICOM bytes in `DicomExtracted/` and in the canonical ZIPs;
* dcm2niix's own sidecars in the working files kept after a failed, partial
  or interrupted conversion (`.work/convert_*/raw/*.json`), which carry the
  fields dcm2niix extracts, such as institution, station, device serial
  number, protocol and descriptions, when the DICOM headers hold them;
* hashes;
* logs, where converter lines that look identifying are replaced before they
  reach disk; this matches line patterns and can miss identifiers.

File and folder names inside the ZIPs, which can contain patient names, are
not written to reports, logs or state.

## Where to look

No timestamped report folder is created. Both commands print these paths:

| File | Purpose |
|---|---|
| Reports/pipeline.log | Main run summaries, important events and errors; rotates at 10 MB with three backups |
| Reports/latest_prepare.log | Latest preparation details; replaced each invocation |
| Reports/latest_convert.log | Latest conversion details, including filtered converter diagnostics |
| Reports/session_status.csv | All expected sessions plus explicit additional sessions; current prepare/conversion state |
| Reports/issues.csv | Unresolved problems, with subject/session and relevant path |
| Reports/run_history.csv | One row per locked invocation, including terminated and aborted runs |
| Reports/.state | Internal index (`index.json`, `index.json.bak`), result journals and open intents; preserve it |

`session_status.csv` columns: `subject_id`, `session`, `prepare_status`
(`awaiting_upload`, `ready`, `held`, `failed`, `interrupted`; a session
whose ZIP has an open `prepare:` issue shows `held`),
`dicom_instances`, `series`, `conversion_status` (`not_converted`,
`converted`, `accepted_partial`, `partial`, `failed`, `interrupted`,
`needs_conversion`, `output_modified`), `converted_volumes`, `archive`,
`nifti_folder`, `required_series`, `converted_series`, `excused_series`
(semicolon-separated series keys), `accepted_errors` (what
`--accept-partial` accepted), `last_attempt_status` and
`last_attempt_folder`. A converted session with a non-empty
`last_attempt_status` kept its complete folder after a failed reconversion.
`needs_conversion` means the extraction changed after the session was
converted (for example after `prepare --force-reextract`); a plain `convert`
leaves it as it is, and `convert --force-reconvert --session SUBJECT/SESSION`
converts it again.

`run_history.csv` columns: `run_id`, `stage`, `outcome` (`ok`, `issues`,
`interrupted`, `terminated`, `aborted`), `seconds`, `completed`, `cached`,
`duplicate`, `failed`, `held`, `unresolved`, `reconciled` (operations left
open by a crash and resolved at startup) and `notes`.

Start with pipeline.log, then issues.csv. Header-date mismatches and missing
canonical ZIPs do not block conversion. Other unresolved problems produce exit
status 2. Success is 0; SIGTERM returns 143 and Ctrl+C returns 130. A report
file held open by another program (for example Excel on Windows) does not stop
a run; it is rewritten at the next report write. `--dry-run` writes no project
data or logs and performs no conversion; it hashes files only when combined
with `--audit`. Normal report refreshes use cached metadata; they do not
reopen all scans.

## Development and validation

```bash
python -m pip install -e ".[test]"
python -m pip install build
python -m pytest
python -m build
```

Tests create synthetic DICOM archives and a simulated converter. They cover
crash injection at every publication step, stop signals, locks, preparation
holds, series coverage, configurable CSV schemas, command entry points,
metadata, and a 1,000-session cache regression that forbids archive hashing,
DICOM header reads, and converter calls on reruns. Smoke tests with the real
dcm2niix run when `MRI_PIPELINE_DCM2NIIX` names its executable. CI runs the
tests on Linux and Windows with Python 3.11 to 3.14, runs the real-converter
tests against a pinned dcm2niix release (v1.0.20261029), and checks the built
distribution contents.

This is research workflow software. Review a small pilot on your target
cluster before processing a full study: check advisory locking, real scanner
coverage, conversion geometry, sequence classification, and resource use.
Outputs are not BIDS datasets or certified de-identified data. DICOMs retain
their original headers and pixel data; selected metadata filtering does not
remove identifiers burned into image pixels.

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

State schema version 3 uses `subject_id`, `.pipeline_complete.json` receipts,
and a `.mri_dicom_pipeline.lock` file inside each storage folder. Any other
state version is refused before processing, and neither state nor data is
written (the run's lock files, log and `aborted` history row still are); start
older projects with a new `--report-root`. The commands refuse to overwrite
session directories that lack the current ownership receipt.

## License

Apache License 2.0; see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
