"""R4-OPS-03: every command refuses a moved project (CUJ 7; R-OPS-3; D-11).

A tier 2 project goes through prepare, convert and select, and the whole folder is then duplicated
elsewhere. Each command is started against the duplicate and has to stop with exit 2 and a message
that mentions the move. In the duplicate only lock files, run logs and the run history (README
"Where to look") may change. Expected values: README "Fast repeated runs" (moving a project is not
supported) and owner decision D-11 (P).
"""

from __future__ import annotations

import os
import shutil

import pytest

from r4lib import e2e, listing

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")
# README "Where to look": the run logs and history; plus the lock files of every locked root.
ALLOWED = ["*.mri_dicom_pipeline.lock", "Reports/pipeline.log", "Reports/pipeline.log.*",
           "Reports/latest_prepare.log", "Reports/latest_convert.log", "Reports/latest_select.log",
           "Reports/run_history.csv"]


@pytest.fixture(scope="module")
def moved(tmp_path_factory):
    base = tmp_path_factory.mktemp("moved")
    conv = e2e.build(base / "orig", "structural", "STR-03-SIEMENS-ESPREE-2D", "fake")
    assert conv.select(base / "orig" / "Selected").code == 0
    copy = base / "copy"
    shutil.copytree(conv.root, copy)
    return conv, str(copy), base


@pytest.mark.parametrize("command", ["prepare", "convert", "select"])
def test_r4_ops_03_moved_project_is_refused(moved, command, tmp_path):
    conv, copy, base = moved
    from r4lib.project import R4Project

    project = R4Project(copy)
    before = listing.list_tree(copy)
    args = []
    if command == "convert":
        args, env = e2e.converter_args(base, conv.exam, "fake")
    if command == "select":
        args = ["--output-root", str(tmp_path / "Selected")]
    result = project.run(command, *args)
    text = (result.stdout + result.stderr).lower()
    after = listing.list_tree(copy)
    d = listing.diff(before, after, ALLOWED, ["*.mri_dicom_pipeline.lock.owner"])
    assert result.code == 2, (result.code, text[-800:])
    assert "moved" in text or "copied" in text or "relocat" in text, text[-800:]
    assert not listing.unexplained(d), d
    if command == "select":
        left = os.listdir(tmp_path / "Selected") if (tmp_path / "Selected").exists() else []
        assert set(left) <= {".mri_dicom_pipeline.lock"}, left  # the output root's lock file only
