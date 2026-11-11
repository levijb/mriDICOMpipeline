# Operator runbook

Procedures for running `mri-dicom-pipeline` on study data. The behavior
behind each procedure is specified in
[`designs/production-hardening.md`](designs/production-hardening.md); the
README describes the commands and reports.

Every command prints where its logs and reports are. Start with
`Reports/pipeline.log`, then `Reports/issues.csv`. Exit code 0 means nothing
needs attention; 2 means `issues.csv` lists blocking problems; 143 and 130
mean the run was stopped by SIGTERM or Ctrl+C.

## First production run

1.  Keep a copy of every ZIP outside the project. `prepare` moves accepted
    ZIPs into `DicomUploads/SubjectsSorted/` and never modifies or deletes
    them, but a separate copy protects against mistakes outside the pipeline.
2.  Put `sessions.csv` in the project folder and check that every subject ID
    and scan date is listed.
3.  Put the ZIPs in `DicomUploads/Uploads/`, or, if they already have their
    final names, in `DicomUploads/SubjectsSorted/<id>/<id>_<YYYYMMDD>.zip`,
    where they are prepared in place.
4.  Preview: `mri-dicom-pipeline prepare --dry-run`. The dry run lists what
    it would do and writes nothing.
5.  Run `mri-dicom-pipeline prepare`, then `mri-dicom-pipeline convert`.
6.  Read `Reports/session_status.csv`. A session with
    `conversion_status = converted` has every required series. Look at
    `excused_series` for series that were not converted by design.

A ZIP that cannot be accepted (an unlisted ID, a bad file name, a held
conflict) stays where it is and appears in `issues.csv`.

## Before running from more than one machine (two-node lock check)

Locks protect a project only when the filesystem's advisory locks work across
machines. Runs from a single machine need no check. Before starting runs from
two machines (for example two cluster nodes) on the same storage:

1.  On machine A, hold the locks of a project on that storage:
    ```bash
    python -c "from mri_dicom_pipeline import config, locks; import sys, time; cfg = config.configure(config.config_parser('prepare').parse_args(sys.argv[1:]), 'prepare'); l = locks.RootLocks(cfg.lock_roots, 'prepare', 'LOCK-CHECK'); l.__enter__(); print('holding'); time.sleep(600)" --project-root /path/to/test-project
    ```
2.  While it prints `holding`, run `mri-dicom-pipeline prepare --project-root
    /path/to/test-project` on machine B. It must exit with code 2 and name
    machine A as the holder.
3.  If machine B runs instead, the storage's locks work only within one
    machine: run the pipeline from one machine at a time on that storage.
4.  Record the result (storage path, date, both host names, outcome) with
    your study's operating notes.

## Resolving holds and issues

`issues.csv` names the session, the stage, the reason, and a path.

* **`prepare:` issue, "Accepted canonical ZIP changed".** Different bytes
  appeared at a ZIP's place in `SubjectsSorted/`. The prepared session and
  its outputs are unchanged, and conversion of that session is blocked.
  Restore the original ZIP (from your copy) to clear the hold. If the new
  export is the one you want, move it out of `SubjectsSorted/` and upload it
  as a new session, for example `S001_20260725_2.zip`.
* **`prepare:` issue, "Different DICOM contents for an existing session".**
  An upload carries different images under an existing session name. The ZIP
  stays in `Uploads/`. Rename it with an `_N` suffix if it is a separate
  session, or remove it.
* **`prepare:` issue, "holds N studies".** One ZIP mixes more than one study,
  which can mean two people's exams were exported together. Split it into
  separate `_N` ZIPs, or, if it is one session, accept it:
  `mri-dicom-pipeline prepare --accept-multi-study S001/20260725`.
* **`prepare:` issue, "DICOM StudyDate is N days from the ZIP-name date".**
  The scan date in the DICOM headers is 28 or more days from the date in the
  ZIP name, which usually means the name was mistyped. The ZIP stays where it
  is and nothing is published. If the name is wrong, rename the ZIP to the
  right date and rerun; the old issue clears itself. If the name is right,
  accept it: `mri-dicom-pipeline prepare --accept-date-mismatch S001/20260725`.
* **`prepare:` issue, "share a SOP Instance UID but have different bytes".**
  The export contains conflicting copies of an image. Re-export the session
  and upload the new ZIP as a new `_N` session.
* **`prepare:` issue, "Not enough free space".** Free disk space (the ZIP's
  size plus a reserve of 5% of the filesystem, at most 50 GiB), then rerun.
* **`prepare:` or `convert:` issue, "Access is denied" or "being used by
  another process" (Windows).** Another program kept a file in the session's
  folders open for more than 30 seconds, which the pipeline waits out. Common
  causes are a virus scan, an Explorer window or terminal open inside the
  folder, or a viewer with a scan loaded. Nothing was published. Close the
  program, then rerun with `--retry-failed`.
* **`missing:` issue (nonblocking).** A prepared session's canonical ZIP is
  gone from `SubjectsSorted/`. Conversion continues from the extracted copy.
  Put the ZIP back to clear the issue.
* **`output:` issue, "Output folder modified or missing".** A complete NIfTI
  folder changed (a file added, removed or renamed) or disappeared. Nothing
  was reconverted. Either restore the folder and revalidate it:
  `mri-dicom-pipeline convert --audit --session S001/20260725`, which accepts
  it when every listed file matches and nothing extra is present; or move
  any files you added out of the folder and reconvert it explicitly (below).
* **`output:` issue, "Unreferenced output folder".** A NIfTI folder exists
  that state does not record and that does not match the prepared session.
  It was not replaced. Inspect it; move it aside if it is not needed, then
  run `convert` again.
* **`recover:` issue.** Crash recovery found a folder it would not touch
  (see "After a crash" below). The session is held. Inspect the folder named
  in the issue. When it is safe, delete the intent file named by the issue's
  key (`Reports/.state/intents/<id>.json`) and run the command again; the
  issue clears itself, and the session is processed normally.
* **`convert:` issue.** A conversion failed or came out partial. The reason
  lists missing required series or the converter's exit code. Fix the cause,
  then run `convert --retry-failed`.

## Explicit reconversion

A complete session is never reconverted automatically. To reconvert one, for
example after a dcm2niix upgrade:

```bash
mri-dicom-pipeline convert --force-reconvert --session S001/20260725
```

It refuses a folder holding files that are not in its inventory and names
them; move them elsewhere first. If the reconversion fails or comes out
partial, the complete folder stays in place, and `session_status.csv` shows
the failed attempt in `last_attempt_status` and `last_attempt_folder`.

## After a crash

A run killed partway through (a node failure, `kill -9`, a power cut aside)
leaves intent files in `Reports/.state/intents/`. The next run of the same
command resolves them before planning and logs each action as `RECONCILED`:

* a half-done folder swap is rolled back to the previous folder;
* an unfinished copy is removed, and the work is redone;
* a folder that was published but not yet recorded is kept only after every
  file in its inventory is checked to be present at its recorded size;
* anything else is left untouched with a `recover:` issue (see above).

`run_history.csv` counts the resolved operations in `reconciled`. A
`--dry-run` lists the same actions as `WOULD RECONCILE` and changes nothing.
Power loss with unflushed disk caches is outside what recovery guarantees.

## Recovery from a corrupt state index

The state index is `Reports/.state/index.json`; each save keeps the previous
one as `index.json.bak`. If `index.json` cannot be read, the run uses the
backup plus the newer journals, logs an error, notes
"state recovered from index.json.bak" in `run_history.csv`, and moves the
unreadable file aside to `index.json.corrupt-<run id>` at its next save. If
neither file can be read, the run stops (exit 2) and names both files: restore
`Reports/.state` from a backup.

If the state folder is lost entirely, or a run points at the wrong
`--report-root`, the next runs rebuild state from the receipts in the
published folders: each canonical ZIP is hashed once and its extraction and
NIfTI folders are adopted after the file check. Nothing is extracted or
converted again, but hashing takes time on a large project.

## Rolling back to 0.1.0

Reinstall 0.1.0 and point it at a new, empty `--report-root`; 0.1.0 refuses a
schema-3 index. With empty state, 0.1.0 re-prepares and reconverts every
session it finds and replaces folders that hold receipts. ZIPs are not
modified.

## Cleaning up failed work

Nothing deletes failure material automatically:

* `.work/convert_*` folders: working files of failed, partial or interrupted
  conversions;
* `Niftis/.pending/<id>/<session>/<attempt>/`: published partial attempts.

Their paths appear in `issues.csv`. Once a session's `convert:` issue is gone
(it converted, or you accepted the outcome), delete that session's folders
from both places. Keep anything an open issue still points to.

## Slurm time limits

At the time limit, Slurm sends SIGTERM and, after a grace period, SIGKILL. The
pipeline stops in order on SIGTERM (exit 143), and the next run resolves
anything left open. To give it time, ask Slurm to signal the pipeline before
the limit, and start the command with `exec` so the signal reaches it:

```bash
#SBATCH --signal=B:TERM@120
exec mri-dicom-pipeline convert --project-root /path/to/study
```

## Quota exhaustion

The free-space check sees filesystem free space, not user or group quotas. On
quota-managed storage, a full quota shows up as write failures: the session
fails, nothing is published, and inputs stay in place. Check your quota, free
space, and run the command again.

## Node-local staging

`convert --staging-root` can put working files on node-local scratch. A run
sees only its own node's scratch: working files left on another node's local
disk (for example by a crashed run) are out of its reach, and the cluster may
delete them after the job. Published partial attempts stay in
`Niftis/.pending/`.
