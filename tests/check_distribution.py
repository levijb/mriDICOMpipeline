"""Fail packaging checks if local study data enters a release artifact."""
from pathlib import Path
import tarfile
import zipfile


def check(names):
    forbidden = {"DicomUploads", "DicomExtracted", "Niftis", "Reports", ".work", "local_data"}
    for name in names:
        path = Path(name)
        assert not forbidden.intersection(path.parts), name
        assert not name.lower().endswith((".pdf", ".dcm", ".dicom", ".ima", ".nii", ".nii.gz", ".zip")), name
        if name.endswith(".csv"):
            assert path.parts[-2:] == ("examples", "sessions.csv"), name


def main():
    artifacts = list(Path("dist").glob("*.whl")) + list(Path("dist").glob("*.tar.gz"))
    assert len(artifacts) == 2, artifacts
    for path in artifacts:
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as archive:
                check(archive.namelist())
        else:
            with tarfile.open(path) as archive:
                check(archive.getnames())
        print(f"Checked {path.name}: no study data included")


if __name__ == "__main__":
    main()
