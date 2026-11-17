"""Shared pytest fixtures."""
import csv
import os

import pytest

from support import fake_converter_script


@pytest.fixture
def real_dcm2niix():
    """Path of a real dcm2niix executable; the test is skipped unless one is configured."""
    path = os.environ.get('MRI_PIPELINE_DCM2NIIX')
    if not path:
        pytest.skip('set MRI_PIPELINE_DCM2NIIX to a dcm2niix executable to run real-converter tests')
    return path


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    with (root / "sessions.csv").open("w", newline="") as f:
        csv.writer(f).writerows([['subject_id', 'scan_date'], ['S001', '7/25/2026'], ['S001', '7/25/2026'],
                                ['S001', '8/1/2026'], ['S002', '7/25/2026'], ['', ''], ['`', '7/25/2026']])
    return root


@pytest.fixture
def converter(tmp_path):
    """A fake dcm2niix that names each output by the -f pattern (support.FAKE_CONVERTER)."""
    return fake_converter_script(tmp_path / 'fake_converter.py')
