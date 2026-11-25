"""TG-DST: distribution (plan section 5, TG-DST; R-DST-1, R-DST-2; marker r4slow).

The commit under test is exported with `git archive`, extracted to a scratch folder seeded with fake study files,
is built (sdist and wheel, `python -m build`), checked with tests/check_distribution.py and
installed into a fresh virtual environment. Expected values: production-hardening G07 and D-6 (C).
The build fetches its build backend (setuptools, as pyproject.toml declares) from the package index,
like the CI package job; the install fetches pydicom.
"""

from __future__ import annotations

import glob
import io
import os
import subprocess
import sys
import tarfile
import zipfile

import pytest

pytestmark = [pytest.mark.r4slow, pytest.mark.filterwarnings("ignore::DeprecationWarning")]
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COMMIT = "4d8a362b03fc90159fe286333317a0a8358242c0"
PLANTED = ["sessions.csv", "DicomUploads/x.zip", "Niftis/x.nii.gz", "Reports/r.csv", ".work/w.txt",
           "local_data/l.txt", "Scripts/x.txt"]


def _unpack(dest):
    data = subprocess.run(["git", "archive", "--format=tar", COMMIT], cwd=REPO, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        t.extractall(dest, filter="data")
    for rel in PLANTED:
        path = os.path.join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("planted\n")


def _build(src):
    pytest.importorskip("build")
    proc = subprocess.run([sys.executable, "-m", "build", "--outdir", "dist", "."], cwd=src, capture_output=True,
                          text=True, timeout=1200)
    return proc


def _names(src):
    whl = glob.glob(os.path.join(src, "dist", "*.whl"))[0]
    sdist = glob.glob(os.path.join(src, "dist", "*.tar.gz"))[0]
    with zipfile.ZipFile(whl) as z:
        wheel_names = z.namelist()
        metadata = z.read(next(n for n in wheel_names if n.endswith(".dist-info/METADATA"))).decode()
    with tarfile.open(sdist) as t:
        sdist_names = t.getnames()
    return whl, wheel_names, sdist_names, metadata


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    src = str(tmp_path_factory.mktemp("dst") / "src")
    _unpack(src)
    proc = _build(src)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return src


def test_r4_dst_01_artifacts_and_install(built, tmp_path):
    src = built
    check = subprocess.run([sys.executable, os.path.join(src, "tests", "check_distribution.py")], cwd=src,
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr
    whl, wheel_names, sdist_names, _meta = _names(src)
    sdist_rel = {n.split("/", 1)[1] for n in sdist_names if "/" in n}
    for rel in PLANTED:
        assert rel not in sdist_rel and rel not in wheel_names, rel
    modules = subprocess.run(["git", "ls-tree", "--name-only", COMMIT, "src/mri_dicom_pipeline/"], cwd=REPO,
                             capture_output=True, text=True, check=True).stdout.split()
    for m in (os.path.basename(x) for x in modules if x.endswith(".py")):
        assert any(n.endswith(f"mri_dicom_pipeline/{m}") for n in wheel_names), m
    for lic in ("LICENSE", "NOTICE"):
        assert any(n.endswith(lic) for n in wheel_names), lic
        assert any(n.endswith("/" + lic) for n in sdist_names), lic
    env = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(env)], check=True)
    py = str(env / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python"))
    inst = subprocess.run([py, "-m", "pip", "install", "-q", whl], capture_output=True, text=True, timeout=900)
    assert inst.returncode == 0, inst.stderr[-2000:]
    exe = str(env / ("Scripts" if os.name == "nt" else "bin") / "mri-dicom-pipeline")
    version = subprocess.run([exe, "--version"], capture_output=True, text=True)
    assert version.returncode == 0 and version.stdout.strip() == "0.2.0", version.stdout + version.stderr
    helped = subprocess.run([py, "-m", "mri_dicom_pipeline", "--help"], capture_output=True, text=True)
    assert helped.returncode == 0, helped.stderr


def test_r4_dst_02_runtime_requirements(built):
    _whl, _w, _s, metadata = _names(built)
    requires = [l.split(":", 1)[1].strip() for l in metadata.splitlines() if l.startswith("Requires-Dist:")]
    runtime = [r for r in requires if "extra ==" not in r]
    assert [r.replace(" ", "") for r in runtime] == ["pydicom<4,>=3.0"] or \
        [r.replace(" ", "") for r in runtime] == ["pydicom>=3.0,<4"], runtime


def test_r4_dst_03_extra_example_csv_is_caught(tmp_path):
    src = str(tmp_path / "src")
    _unpack(src)
    with open(os.path.join(src, "examples", "extra.csv"), "w", encoding="utf-8") as f:
        f.write("a,b\n")
    proc = _build(src)
    assert proc.returncode == 0, proc.stderr[-2000:]
    check = subprocess.run([sys.executable, os.path.join(src, "tests", "check_distribution.py")], cwd=src,
                           capture_output=True, text=True)
    assert check.returncode != 0
    assert "examples/extra.csv" in (check.stdout + check.stderr)
