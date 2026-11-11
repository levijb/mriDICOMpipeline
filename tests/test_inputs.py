"""Preparation tests (T03, T05, T06, T09; R05, R06, R07, R12, R18; P3, P7)."""
from collections import namedtuple
import csv
import json
import os
from pathlib import Path
import shutil
import zipfile

from pydicom.uid import generate_uid
import pytest

from mri_dicom_pipeline import config, convert, fsutil, prepare
from support import archive, archive_objects, call, csa_non_image_bytes, dicom_bytes, mr_series, state

KEY = 'S001/20260725'
CANONICAL = 'DicomUploads/SubjectsSorted/S001/S001_20260725.zip'
EXTRACTED = 'DicomExtracted/Subjects/S001/20260725'
NIFTI = 'Niftis/Subjects/S001/20260725'


def receipt_id(folder):
    return json.loads((folder / '.pipeline_complete.json').read_text())['receipt_id']


def inventory(project):
    return json.loads((project / EXTRACTED / '.inventory.json').read_text())


def fail_if_called(*args, **kwargs):
    pytest.fail('unexpected call')


def prepared(project, tmp_path):
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    return project / CANONICAL


def overwrite_with_other_export(path, tmp_path):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('exam/1', dicom_bytes(tmp_path, value=9))


def prepare_issues(project):
    return [key for key in state(project)['issues'] if key.startswith('prepare:')]


# ---- Uploads and space ----------------------------------------------------------

def test_truncated_upload_fails_then_prepares_when_complete(project, tmp_path):
    """Q4: a partly copied ZIP fails to open and is retried once the copy completes."""
    upload = archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    data = upload.read_bytes()
    upload.write_bytes(data[:len(data) // 2])
    assert call(project, 'prepare') == 2
    assert upload.exists()
    upload.write_bytes(data)
    assert call(project, 'prepare') == 0
    assert state(project)['sessions'][KEY]['status'] == 'ready'


def test_free_space_check_refuses_extraction_and_keeps_zip(project, tmp_path, monkeypatch):
    """Q15: extraction does not start when the ZIP would not fit with the reserve."""
    upload = archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    usage = namedtuple('usage', 'total used free')
    monkeypatch.setattr(shutil, 'disk_usage', lambda path: usage(10**12, 10**12 - 1000, 1000))
    assert call(project, 'prepare') == 2
    assert upload.exists() and not (project / EXTRACTED).exists()
    assert 'Not enough free space' in json.dumps(state(project)['issues'])


def test_sorted_zips_are_prepared_in_place_and_never_deleted(project, tmp_path):
    """CUJ 6: ZIPs placed directly in SubjectsSorted/<id>/ stay where they are, unchanged.

    A ZIP there that cannot be accepted stays too, with an issue.
    """
    canonical = project / CANONICAL
    canonical.parent.mkdir(parents=True)
    with zipfile.ZipFile(canonical, 'w') as z:
        z.writestr('exam/1', dicom_bytes(tmp_path))
    unaccepted = canonical.with_name('S001_badname.zip')
    unaccepted.write_bytes(b'not a session file name')
    digest = fsutil.sha256(canonical)
    assert call(project, 'prepare') == 2
    assert fsutil.sha256(canonical) == digest
    assert state(project)['sessions'][KEY]['status'] == 'ready'
    assert unaccepted.read_bytes() == b'not a session file name'


def test_empty_state_adopts_matching_extraction(project, tmp_path, monkeypatch):
    """D6: with state lost, the existing extraction is adopted, not extracted again."""
    prepared(project, tmp_path)
    before = receipt_id(project / EXTRACTED)
    shutil.rmtree(project / 'Reports/.state')
    monkeypatch.setattr(prepare, 'extract_archive', fail_if_called)
    assert call(project, 'prepare') == 0
    assert receipt_id(project / EXTRACTED) == before
    assert state(project)['sessions'][KEY]['status'] == 'ready'


def test_moved_project_refused(project, tmp_path, capsys):
    prepared(project, tmp_path)
    moved = tmp_path / 'moved'
    project.rename(moved)
    assert call(moved, 'prepare') == 2
    assert 'appears moved' in capsys.readouterr().err


def test_session_filter_limits_prepare(project, tmp_path):
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    other = archive(project, 'S001_20260801.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare', '--session', KEY) == 0
    assert set(state(project)['sessions']) == {KEY}
    assert other.exists()
    assert call(project, 'prepare', '--session', 'S001-20260725') == 2


# ---- Immutable canonical ZIPs (Q3) ------------------------------------------------

def test_changed_canonical_zip_is_held_and_outputs_unchanged(project, tmp_path, converter):
    """P3: a different export copied over the canonical ZIP replaces nothing."""
    canonical = prepared(project, tmp_path)
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    extracted, nifti = receipt_id(project / EXTRACTED), receipt_id(project / NIFTI)
    overwrite_with_other_export(canonical, tmp_path)
    assert call(project, 'prepare') == 2
    assert receipt_id(project / EXTRACTED) == extracted and receipt_id(project / NIFTI) == nifti
    assert prepare_issues(project)


def test_restored_canonical_zip_accepted_without_reextract(project, tmp_path, monkeypatch):
    canonical = prepared(project, tmp_path)
    stat = canonical.stat()
    os.utime(canonical, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    monkeypatch.setattr(prepare, 'extract_archive', fail_if_called)
    assert call(project, 'prepare') == 0
    assert state(project)['sessions'][KEY]['archive_signature'] == list(fsutil.signature(canonical))
    monkeypatch.setattr(fsutil, 'sha256', fail_if_called)
    assert call(project, 'prepare') == 0


def test_force_reextract_refuses_changed_bytes(project, tmp_path):
    canonical = prepared(project, tmp_path)
    before = receipt_id(project / EXTRACTED)
    overwrite_with_other_export(canonical, tmp_path)
    assert call(project, 'prepare', '--force-reextract') == 2
    assert receipt_id(project / EXTRACTED) == before


def test_canonical_conflict_blocks_conversion(project, tmp_path, converter, monkeypatch):
    canonical = prepared(project, tmp_path)
    overwrite_with_other_export(canonical, tmp_path)
    assert call(project, 'prepare') == 2
    with (project / 'Reports/session_status.csv').open(newline='') as f:
        rows = [row for row in csv.DictReader(f) if (row['subject_id'], row['session']) == ('S001', '20260725')]
    assert [row['prepare_status'] for row in rows] == ['held']
    monkeypatch.setattr(convert, 'convert_one', fail_if_called)
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2


def test_audit_rehashes_canonical_zip(project, tmp_path):
    """An edit that keeps size and mtime is caught only by --audit."""
    canonical = prepared(project, tmp_path)
    stat, data = canonical.stat(), bytearray(canonical.read_bytes())
    data[len(data) // 2] ^= 0xFF
    canonical.write_bytes(bytes(data))
    os.utime(canonical, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert call(project, 'prepare') == 0
    assert call(project, 'prepare', '--audit') == 2
    assert prepare_issues(project)


def test_missing_canonical_zip_nonblocking_issue(project, tmp_path, converter):
    """R18: a missing canonical ZIP is reported, but conversion continues."""
    canonical = prepared(project, tmp_path)
    saved = canonical.read_bytes()
    canonical.unlink()
    assert call(project, 'prepare') == 0
    assert f'missing:{KEY}' in state(project)['issues']
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    canonical.write_bytes(saved)
    assert call(project, 'prepare') == 0
    assert f'missing:{KEY}' not in state(project)['issues']


# ---- SOP classes, repeats, studies (Q19, Q20) ---------------------------------------

def test_non_image_series_extracted_and_excused(project, tmp_path):
    study = generate_uid()
    _, image = mr_series(tmp_path, study=study)
    archive_objects(project, 'S001_20260725.zip', image + [csa_non_image_bytes(tmp_path, study=study)])
    assert call(project, 'prepare') == 0
    rows = {tuple(row['sop_classes']): row for row in inventory(project)['series']}
    assert rows[('1.2.840.10008.5.1.4.1.1.4',)]['required'] is True
    assert rows[('1.3.12.2.1107.5.9.1',)]['required'] is False
    assert inventory(project)['n_dicom'] == 4


def test_repeated_identical_instances_written_once(project, tmp_path):
    data = dicom_bytes(tmp_path)
    archive_objects(project, 'S001_20260725.zip', [data, data])
    assert call(project, 'prepare') == 0
    assert (inventory(project)['n_dicom'], inventory(project)['repeated_identical']) == (1, 1)
    assert len(list((project / EXTRACTED / 'dicom').iterdir())) == 1


def test_conflicting_repeated_instance_held(project, tmp_path):
    uid, study, series = generate_uid(), generate_uid(), generate_uid()
    objects = [dicom_bytes(tmp_path, value=v, sop_uid=uid, study=study, series=series) for v in (1, 2)]
    upload = archive_objects(project, 'S001_20260725.zip', objects)
    assert call(project, 'prepare') == 2
    assert upload.exists() and not (project / EXTRACTED).exists()


def test_multi_study_held_until_accepted(project, tmp_path):
    upload = archive_objects(project, 'S001_20260725.zip', [dicom_bytes(tmp_path), dicom_bytes(tmp_path)])
    assert call(project, 'prepare') == 2
    assert upload.exists() and KEY not in state(project)['sessions']
    assert call(project, 'prepare', '--accept-multi-study', KEY) == 0
    assert state(project)['sessions'][KEY]['accepted_multi_study'] is True
    assert call(project, 'prepare') == 0


def test_date_a_month_from_zip_name_held_until_accepted(project, tmp_path):
    """A StudyDate 28+ days from the ZIP-name date (a mistyped month) is held; a smaller gap only warns."""
    upload = archive(project, 'S001_20260822.zip', dicom_bytes(tmp_path))  # the headers say 20260725: 28 days
    assert call(project, 'prepare', '--allow-unlisted-dates') == 2
    assert upload.exists() and 'S001/20260822' not in state(project)['sessions']
    [issue] = prepare_issues(project)
    assert '28 days' in state(project)['issues'][issue]['reason']
    assert call(project, 'prepare', '--allow-unlisted-dates', '--accept-date-mismatch', 'S001/20260822') == 0
    assert state(project)['sessions']['S001/20260822']['status'] == 'ready'
    assert 'date:S001/20260822' in state(project)['issues'] and not prepare_issues(project)
    # Once accepted, extracting the same ZIP again is not held.
    assert call(project, 'prepare', '--allow-unlisted-dates', '--force-reextract') == 0


def test_days_apart_skips_study_dates_that_are_not_8_digits():
    """SB-10: a shortened StudyDate such as 202611 is skipped, not read as 1 January 2026."""
    assert prepare.days_apart(['202611', '2026072', '20261115.0'], '20261115') == 0
    assert prepare.days_apart(['20261018', '202611'], '20261115') == 28


def test_roster_issue_never_outlives_its_cause(project, tmp_path):
    """SB-1: a roster gap does not hold an already prepared ZIP, and a roster issue clears once fixed."""
    archive(project, 'S001_20260802.zip', dicom_bytes(tmp_path))  # 20260802 is not in the roster
    assert call(project, 'prepare', '--allow-unlisted-dates') == 0
    assert call(project, 'prepare') == 0 and not prepare_issues(project)
    # Working on the ZIP again without the option is refused...
    assert call(project, 'prepare', '--force-reextract') == 2 and prepare_issues(project)
    # ...and the refusal clears on the next run that allows the date.
    assert call(project, 'prepare', '--allow-unlisted-dates') == 0 and not prepare_issues(project)


@pytest.mark.parametrize('damage', ['encrypted', 'renamed'])
def test_zip_member_names_stay_out_of_reports(project, tmp_path, damage):
    """SB-8: zipfile's error messages name the member, and member paths can carry patient names."""
    upload = project / 'DicomUploads/Uploads/S001_20260725.zip'
    upload.parent.mkdir(parents=True)
    with zipfile.ZipFile(upload, 'w') as z:
        z.writestr('FAKE_PRIVATE_NAME/IM0001', dicom_bytes(tmp_path))
    data = bytearray(upload.read_bytes())
    if damage == 'encrypted':
        # Set the encryption flag in the local header and the central directory.
        data[6] |= 1
        data[data.rindex(b'PK') + 8] |= 1
    else:
        # The local header's copy of the name no longer matches the central directory's.
        at = data.index(b'FAKE_PRIVATE_NAME') + 16
        data[at:at + 1] = b'X'
    upload.write_bytes(bytes(data))
    assert call(project, 'prepare') == 2 and upload.exists()
    for path in (project / 'Reports').rglob('*'):
        if path.is_file():
            assert b'FAKE_PRIVATE' not in path.read_bytes(), path


def test_access_denied_during_publish_is_retried(project, tmp_path, monkeypatch):
    """Windows: a virus scanner still reading just-extracted files refuses the folder rename for a moment."""
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    original, refused = Path.rename, []

    def scanner_busy(self, target):
        if Path(target).name.startswith('.incoming_') and len(refused) < 2:
            refused.append(target)
            raise PermissionError(13, 'Access is denied')
        return original(self, target)

    monkeypatch.setattr(Path, 'rename', scanner_busy)
    assert call(project, 'prepare') == 0
    assert len(refused) == 2 and state(project)['sessions'][KEY]['status'] == 'ready'


# ---- Dry run (P7) ---------------------------------------------------------------

def test_dry_run_audit_hashes_but_writes_nothing(project, tmp_path, monkeypatch, capsys):
    prepared(project, tmp_path)
    calls, original = [], fsutil.sha256
    monkeypatch.setattr(fsutil, 'sha256', lambda *a, **k: calls.append(a) or original(*a, **k))
    before = {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
    assert call(project, 'prepare', '--dry-run', '--audit') == 0
    assert calls
    assert before == {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
    with pytest.raises(SystemExit):
        config.config_parser('prepare').parse_args(['--help'])
    assert 'hashes files only when combined with --audit' in ' '.join(capsys.readouterr().out.split())
