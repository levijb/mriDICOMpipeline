# Round 4 manual Windows checks: R4-XPL-06, 07, 08, 09

Phase 4 checks on this Windows machine (plan TG-XPL). Each uses a synthetic project, never the live
project. Record the result of each step, with the date, in `docs/testing/round4/R4_GATES.md`.

## Preparation

Open PowerShell at the root of the repository and run:

```powershell
$py = ".\.venv\Scripts\python.exe"
$base = "$env:TEMP\r4manual"
& $py -c "import sys; sys.path.insert(0, 'tests/round4'); from r4lib import e2e; print(e2e.build(r'$base\hc03', 'hard_cases', 'HC-03-REPEAT-CONSISTENT', 'fake').root)"
'{"repeat_policy": "later"}' | Set-Content "$base\later.json"
'{"repeat_policy": "earlier"}' | Set-Content "$base\earlier.json"
$project = "$base\hc03\project"
```

Each check below starts from a fresh `Selected` folder: `Remove-Item -Recurse -Force "$base\Selected"`.

## R4-XPL-06: interrupting with Ctrl+C

1.  `& $py -m mri_dicom_pipeline select --project-root $project --output-root "$base\Selected" --publish --profile "$base\later.json"`
2.  Press Ctrl+C while it runs (rerun with a larger exam, for example STR-04, if it ends too fast).
3.  **Expected:** `$LASTEXITCODE` is 130. Run step 1 again: it reports the visit published or
    unchanged, and no `.staging_*`, `.incoming_*` or `.previous_*` folder remains.

## R4-XPL-07: closing the terminal window

1.  Start step 1 of R4-XPL-06 in a new PowerShell window, then close the window while it runs.
2.  **Expected:** in another window, run step 1 again: it cleans up and publishes; no leftover swap
    folder below `$base\Selected\Subjects`.

## R4-XPL-08: an image viewer keeps a published file open

1.  Publish with `later.json`.
2.  Open one published `.nii.gz` from `$base\Selected\Subjects\...\T1\` in an image viewer that keeps
    the file open (for example ITK-SNAP or MRIcroGL).
3.  Publish again with `earlier.json` (a changed decision).
4.  **Expected:** after retrying for about 30 seconds the publication is refused and an issue is recorded (exit 2); the old folder is
    intact. Close the viewer and rerun: it publishes.

## R4-XPL-09: a OneDrive-synced folder

1.  Build the project and output folder inside the OneDrive folder (`$env:OneDrive\r4manual`).
2.  Run `select --publish` twice, waiting for OneDrive to finish syncing in between.
3.  **Expected:** the second run reports every visit unchanged (exit 0 or 2 as the first run).
