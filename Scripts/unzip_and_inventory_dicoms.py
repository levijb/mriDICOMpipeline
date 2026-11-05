#!/usr/bin/env python3
"""Prepare DICOM uploads. Run --help for options; see ../README.md."""
from pathlib import Path
import sys

# Keep source-checkout scripts usable before package installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mri_dicom_pipeline.runner import main

if __name__ == "__main__":
    raise SystemExit(main("prepare"))
