"""R4-OPS-04: run time and peak memory of one pipeline command or of the test suite (plan 2.1 item 57).

    python tests/round4/realdata/r4_timed.py --evidence FILE.json -- select --project-root ... --output-root ...
    python tests/round4/realdata/r4_timed.py --evidence FILE.json --pytest -- tests -q

The command runs inside this process (mri_dicom_pipeline.cli.main, or pytest.main with --pytest),
since on Windows the mri-dicom-pipeline launcher, like the .venv\Scripts\python.exe stub, hands the
work to a second interpreter process: timing the process the owner launches would measure only the small
launcher. Writes wall time, exit code and this process's peak working set (Windows:
GetProcessMemoryInfo through ctypes; Linux: getrusage) to the evidence file. With --pytest the figure
covers the pytest process alone, not the processes the tests start. The script prints nothing
itself, exits with the code the command returned, and will not write evidence into the live project.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone

LIVE = os.path.normcase(os.path.abspath(r"D:\mri_project"))


def _inside_live(path):
    p = os.path.normcase(os.path.abspath(path))
    return p == LIVE or p.startswith(LIVE + os.sep)


def peak_bytes():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return int(counters.PeakWorkingSetSize)
    import resource

    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    rest = []
    if "--" in argv:
        i = argv.index("--")
        argv, rest = argv[:i], argv[i + 1:]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--pytest", action="store_true")
    args = parser.parse_args(argv)
    if _inside_live(args.evidence):
        parser.error("the evidence file must not be inside the live project")
    if not rest:
        parser.error("give the command after --")
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t0 = time.monotonic()
    try:
        if args.pytest:
            import pytest

            code = int(pytest.main(rest))
        else:
            from mri_dicom_pipeline import cli

            code = cli.main(rest)
            code = int(code) if isinstance(code, int) else 0
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    seconds = round(time.monotonic() - t0, 3)
    record = {"command": "pytest" if args.pytest else rest[0], "arguments": rest, "exit_code": code,
              "wall_seconds": seconds, "peak_working_set_bytes": peak_bytes(),
              "peak_scope": "this process only; child processes not included", "utc_start": started,
              "platform": platform.platform(), "python": platform.python_version()}
    os.makedirs(os.path.dirname(os.path.abspath(args.evidence)), exist_ok=True)
    with open(args.evidence, "w", encoding="utf-8", newline="\n") as f:
        json.dump(record, f, indent=1)
    return code


if __name__ == "__main__":
    sys.exit(main())
