"""Round 4 fixtures (plan 2.1 item 1)."""

from __future__ import annotations

import csv
import importlib.util
import os
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:  # Hypothesis is a round 4 test-only dependency (D-6); profiles are fixed-seed in CI.
    from hypothesis import settings as _hyp_settings

    _hyp_settings.register_profile("ci", derandomize=True, max_examples=200, deadline=None)
    _hyp_settings.register_profile("dev", max_examples=50, deadline=None)
    _hyp_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
except ImportError:  # pragma: no cover - only without the round4 extra
    pass


def pytest_sessionstart(session):
    """TG-SEED guard: with R4_SEED_SRC set, the pipeline must be imported from that folder, in
    this process and in a child process started with sys.executable."""
    seed_src = os.environ.get("R4_SEED_SRC")
    if not seed_src:
        return
    root = os.path.normcase(os.path.abspath(seed_src))
    import mri_dicom_pipeline  # noqa: PLC0415 - only for the location check

    here = os.path.normcase(os.path.abspath(mri_dicom_pipeline.__file__))
    child = subprocess.run([sys.executable, "-c", "import mri_dicom_pipeline, os; print(os.path.abspath(mri_dicom_pipeline.__file__))"],
                           capture_output=True, text=True, check=True).stdout.strip()
    child = os.path.normcase(child)
    if not (here.startswith(root + os.sep) and child.startswith(root + os.sep)):
        pytest.exit(f"R4_SEED_SRC check failed: test process imports {here}, child imports {child}", returncode=4)


def header_reader_path():
    """The header reader: R4_HEADER_READER, else the frozen copy in oracles/, else the separate
    folder (before Gate G1)."""
    env = os.environ.get("R4_HEADER_READER")
    if env:
        return env
    frozen = os.path.join(HERE, "oracles", "header_reader.py")
    if os.path.exists(frozen) and os.path.getsize(frozen) > 0:
        return frozen
    return os.path.join(os.path.expanduser("~"), "round4-reader", "header_reader.py")


def load_header_reader():
    path = header_reader_path()
    spec = importlib.util.spec_from_file_location("r4_header_reader", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["r4_header_reader"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def header_reader():
    return load_header_reader()


@pytest.fixture
def r4_dcm2niix():
    path = os.environ.get("MRI_PIPELINE_DCM2NIIX")
    if not path:
        pytest.skip("set MRI_PIPELINE_DCM2NIIX to dcm2niix v1.0.20261029 to run r4real tests")
    return path


@pytest.fixture
def r4_tools():
    """The BIDS validator command and the PyBIDS module; skips when either is missing."""
    validator = os.environ.get("MRI_PIPELINE_BIDS_VALIDATOR")
    if not validator:
        validator = shutil.which("bids-validator-deno") or shutil.which("bids-validator")
    if not validator:
        pytest.skip("BIDS validator not found (MRI_PIPELINE_BIDS_VALIDATOR or bids-validator-deno on PATH)")
    bids = pytest.importorskip("bids")
    return {"validator": validator, "bids": bids}


@pytest.fixture
def r4_project(tmp_path):
    """A temporary project root; ``roster(rows)`` writes sessions.csv from catalog rows."""
    root = tmp_path / "project"
    root.mkdir()

    class Project:
        path = root

        @staticmethod
        def roster(rows):
            with (root / "sessions.csv").open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["subject_id", "scan_date"])
                writer.writerows(rows)

    return Project()


# ---------------------------------------------------------------------------- phase 2 fixtures

@pytest.fixture(scope="session")
def exam_store(tmp_path_factory):
    """Catalog exams prepared and converted once per test session (r4lib.e2e.ExamStore)."""
    from r4lib.e2e import ExamStore

    return ExamStore(tmp_path_factory.mktemp("exams"))


@pytest.fixture
def real_exam(exam_store, r4_dcm2niix):
    """real_exam(catalog, exam_id): the exam converted with the real dcm2niix (tier 3)."""
    return lambda cat_name, exam_id: exam_store.get(cat_name, exam_id, "real")


@pytest.fixture
def fake_exam(exam_store):
    """fake_exam(catalog, exam_id): the exam converted with the tier 2 converter."""
    pytest.importorskip("nibabel")
    return lambda cat_name, exam_id: exam_store.get(cat_name, exam_id, "fake")
