"""Installed command-line entry point."""
import argparse
import sys

from . import __version__
from .runner import main as run_phase


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="Study-agnostic DICOM intake and NIfTI conversion")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("command", choices=("prepare", "convert"), help="Run COMMAND --help for stage options")
    if not args or args[0] not in ("prepare", "convert"):
        parser.parse_args(args)
    return run_phase(args[0], args[1:])
