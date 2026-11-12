"""Preparation tests (T03, T05, T06, T09; R05, R06, R07, R12, R18; P3, P7)."""
from collections import namedtuple
import csv
import io
import json
import os
from pathlib import Path
import shutil
import time
import warnings
import zipfile

from pydicom.uid import generate_uid
import pytest

from mri_dicom_pipeline import config, convert, fsutil, prepare
from support import (OTHER_SOP_CLASSES, archive, archive_objects, archive_with_damaged_member, call,
                     csa_non_image_bytes, dicom_bytes, mr_series, other_class_objects, state)

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


# ---- ZIPs that hold nothing usable (plan B-01, B-02, B-12) --------------------------

def write_unusable_zip(path, kind, tmp_path):
    if kind == 'zero-bytes':
        path.write_bytes(b'')
        return
    with zipfile.ZipFile(path, 'w') as z:
        if kind == 'only-folders':
            z.writestr('exam/', b'')
            z.writestr('exam/series/', b'')
        elif kind == 'only-viewer-files':
            z.writestr('ddv/viewer.dll', b'not a dicom')
            z.writestr('exam/preview.jpg', b'preview')
            z.writestr('README.txt', b'how to open the viewer')
        elif kind == 'only-a-nested-zip':
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, 'w') as nested:
                nested.writestr('exam/1', dicom_bytes(tmp_path))
            z.writestr('exam/S001_20260725.zip', inner.getvalue())


@pytest.mark.parametrize('kind', ['zero-bytes', 'empty-archive', 'only-folders', 'only-viewer-files',
                                  'only-a-nested-zip'])
def test_zip_without_dicom_objects_fails_and_is_retried_when_replaced(project, tmp_path, kind):
    """README: a ZIP that cannot be accepted stays where it is, with an issue; nested ZIPs are not opened."""
    upload = project / 'DicomUploads/Uploads/S001_20260725.zip'
    upload.parent.mkdir(parents=True)
    write_unusable_zip(upload, kind, tmp_path)
    before = upload.read_bytes()
    assert call(project, 'prepare') == 2
    assert upload.read_bytes() == before and not (project / EXTRACTED).exists()
    assert prepare_issues(project) and state(project)['sessions'][KEY]['status'] == 'failed'
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))  # the same path, with usable contents
    assert call(project, 'prepare') == 0
    assert state(project)['sessions'][KEY]['status'] == 'ready' and not prepare_issues(project)


@pytest.mark.parametrize('compression', [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED], ids=['stored', 'deflated'])
@pytest.mark.parametrize('rows', [2, 64], ids=['small-object', 'object-larger-than-the-read-buffer'])
def test_member_with_bad_crc_fails_the_zip(project, tmp_path, compression, rows):
    """README: ZIP members are streamed to completion to check their CRCs (plan B-02, gap G-04).

    The damage is in the pixel data, past everything the header reader looks
    at. The member path carries a name, which must stay out of the reports
    (SB-8, the CRC case of plan K-02).
    """
    upload = archive_with_damaged_member(project, 'S001_20260725.zip', 'FAKE_PRIVATE_NAME/IM0001',
                                         dicom_bytes(tmp_path, rows=rows, columns=rows), compression)
    before = upload.read_bytes()
    assert call(project, 'prepare') == 2
    assert upload.read_bytes() == before and not (project / EXTRACTED).exists()
    [issue] = prepare_issues(project)
    assert 'BadZipFile' in state(project)['issues'][issue]['reason']
    for path in (project / 'Reports').rglob('*'):
        if path.is_file():
            assert b'FAKE_PRIVATE' not in path.read_bytes(), path


def test_image_shorter_than_its_declared_size_fails_the_zip(project, tmp_path):
    """README: obviously short uncompressed image objects are rejected."""
    upload = archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path, rows=64, columns=64, pixel_count=16))
    assert call(project, 'prepare') == 2
    assert upload.exists() and not (project / EXTRACTED).exists()
    [issue] = prepare_issues(project)
    assert 'smaller than its declared pixel dimensions' in state(project)['issues'][issue]['reason']


def test_extraction_folder_without_a_receipt_is_never_replaced(project, tmp_path):
    """README: the commands refuse to overwrite session directories that lack the ownership receipt (plan F-08)."""
    foreign = project / EXTRACTED / 'someone_elses_file.txt'
    foreign.parent.mkdir(parents=True)
    foreign.write_text('not made by the pipeline')
    upload = archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 2
    assert upload.exists() and [p.name for p in (project / EXTRACTED).iterdir()] == ['someone_elses_file.txt']
    assert foreign.read_text() == 'not made by the pipeline'
    assert 'not owned by this pipeline' in json.dumps(state(project)['issues'])


# ---- Roster checks at the pipeline level (plan A-03, A-04, A-08) ---------------------

def test_zip_whose_id_differs_from_the_roster_by_case_is_held(project, tmp_path):
    """The roster says S001, so s001_20260725.zip is an unknown ID: it is held, and no s001 folder is made."""
    upload = archive(project, 's001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 2
    assert upload.exists() and not state(project)['sessions']
    [issue] = prepare_issues(project)
    assert state(project)['issues'][issue]['reason'] == prepare.UNLISTED
    assert not (project / 'DicomUploads/SubjectsSorted').exists()
    assert not (project / 'DicomExtracted/Subjects').exists()


@pytest.mark.parametrize('relative', ['S001/S001_20260725.ZIP', 's001/S001_20260725.zip'],
                         ids=['extension-case', 'folder-case'])
def test_sorted_zip_differing_from_its_canonical_path_by_case_is_cached(project, tmp_path, monkeypatch, relative):
    """SB-4: on Windows such a ZIP was hashed again as a duplicate and got a permanent missing: issue.

    On Windows the spelling on disk is recorded; on Linux the ZIP is renamed to its canonical spelling.
    """
    placed = project / 'DicomUploads/SubjectsSorted' / relative
    placed.parent.mkdir(parents=True)
    with zipfile.ZipFile(placed, 'w') as z:
        z.writestr('exam/1', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    monkeypatch.setattr(fsutil, 'sha256', fail_if_called)
    assert call(project, 'prepare') == 0
    assert not state(project)['issues']


def test_unlisted_date_needs_the_option_on_both_commands(project, tmp_path, converter, monkeypatch):
    """README: for an unlisted date of a known ID, pass --allow-unlisted-dates to both commands."""
    upload = archive(project, 'S001_20260802.zip', dicom_bytes(tmp_path, fields={'StudyDate': '20260802'}))
    assert call(project, 'prepare') == 2
    assert upload.exists() and not state(project)['sessions']
    assert call(project, 'prepare', '--allow-unlisted-dates') == 0
    with monkeypatch.context() as patch:
        patch.setattr(convert, 'convert_one', fail_if_called)
        assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert 'absent from current reference' in state(project)['issues']['convert:S001/20260802']['reason']
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--allow-unlisted-dates') == 0
    assert state(project)['sessions']['S001/20260802']['conversion']['status'] == 'converted'
    assert not state(project)['issues']


def test_removed_roster_row_is_flagged_by_convert_not_by_prepare(project, tmp_path, converter, monkeypatch):
    """README: prepare checks the reference only for ZIPs it works on; convert checks every session again."""
    archive(project, 'S002_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    roster = project / 'sessions.csv'
    original = roster.read_bytes()
    roster.write_text('subject_id,scan_date\nS001,7/25/2026\n')
    assert call(project, 'prepare') == 0 and not state(project)['issues']
    monkeypatch.setattr(convert, 'convert_one', fail_if_called)
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert list(state(project)['issues']) == ['convert:S002/20260725']
    roster.write_bytes(original)
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0 and not state(project)['issues']


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
    """An edit that keeps size and mtime is caught only by --audit; only the next --audit clears the hold."""
    canonical = prepared(project, tmp_path)
    stat, original = canonical.stat(), canonical.read_bytes()
    data = bytearray(original)
    data[len(data) // 2] ^= 0xFF
    canonical.write_bytes(bytes(data))
    os.utime(canonical, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert call(project, 'prepare') == 0
    assert call(project, 'prepare', '--audit') == 2
    [issue] = prepare_issues(project)
    assert call(project, 'prepare') == 2  # a plain run cannot see the edit, so it keeps the hold
    assert prepare_issues(project) == [issue]
    canonical.write_bytes(original)
    os.utime(canonical, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert call(project, 'prepare', '--audit') == 0 and not prepare_issues(project)


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


def test_other_sop_classes_are_extracted_and_excused(project, tmp_path, converter):
    """Q19: only series holding MR image objects must convert (plan C-05).

    A secondary capture, a structured report, a presentation state, a key
    object selection, raw data, MR spectroscopy and a private class are all
    extracted, and none of them is required.
    """
    study = generate_uid()
    _, image = mr_series(tmp_path, study=study)
    archive_objects(project, 'S001_20260725.zip', image + other_class_objects(tmp_path, study))
    assert call(project, 'prepare') == 0
    rows = inventory(project)['series']
    assert [row['sop_classes'] for row in rows if row['required']] == [['1.2.840.10008.5.1.4.1.1.4']]
    assert sorted(row['sop_classes'][0] for row in rows if not row['required']) == sorted(
        sop_class for sop_class, _ in OTHER_SOP_CLASSES.values())
    assert inventory(project)['n_dicom'] == 3 + len(OTHER_SOP_CLASSES)
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    coverage = state(project)['sessions'][KEY]['conversion']['coverage']
    assert (len(coverage['required']), len(coverage['excused']), coverage['missing']) == (1, len(OTHER_SOP_CLASSES), [])


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


def test_date_5_days_from_zip_name_held_until_accepted(project, tmp_path):
    """A StudyDate 5+ days from the ZIP-name date is held until accepted; the accepted gap stays a warning."""
    upload = archive(project, 'S001_20260730.zip', dicom_bytes(tmp_path))  # the headers say 20260725: 5 days
    assert call(project, 'prepare', '--allow-unlisted-dates') == 2
    assert upload.exists() and 'S001/20260730' not in state(project)['sessions']
    [issue] = prepare_issues(project)
    assert '5 days' in state(project)['issues'][issue]['reason']
    assert call(project, 'prepare', '--allow-unlisted-dates', '--accept-date-mismatch', 'S001/20260730') == 0
    assert state(project)['sessions']['S001/20260730']['status'] == 'ready'
    assert 'date:S001/20260730' in state(project)['issues'] and not prepare_issues(project)
    # Once accepted, extracting the same ZIP again is not held.
    assert call(project, 'prepare', '--allow-unlisted-dates', '--force-reextract') == 0


def test_days_apart_skips_study_dates_that_are_not_8_digits():
    """SB-10: a shortened StudyDate such as 202611 is skipped, not read as 1 January 2026."""
    assert prepare.days_apart(['202611', '2026072', '20261115.0'], '20261115') == 0
    assert prepare.days_apart(['20261018', '202611'], '20261115') == 28


@pytest.mark.parametrize('header_dates, name_date, gap', [
    ([], '20260725', 0), (['20260725'], '20260725', 0), (['20260726'], '20260725', 1), (['20260724'], '20260725', 1),
    (['20260821'], '20260725', 27), (['20260822'], '20260725', 28), (['20260823'], '20260725', 29),
    (['20260628'], '20260725', 27), (['20260627'], '20260725', 28), (['20260626'], '20260725', 29),
    (['20251220'], '20260110', 21),                                  # across a year end
    (['20240201'], '20240301', 29), (['20230201'], '20230301', 28),  # February with and without its 29th day
    (['20260725', '20260601'], '20260725', 54),                      # the farthest StudyDate decides
    (['', 'notadate', '20261350', '2026-07-01'], '20260725', 0),     # values that are not dates are skipped
])
def test_days_apart_table(header_dates, name_date, gap):
    """Plan E-01 (and E-03 as examples): the gap is counted in both directions."""
    assert prepare.days_apart(header_dates, name_date) == gap


@pytest.mark.parametrize('study_date, held', [('20260721', False), ('20260720', True), ('20260719', True),
                                              ('20260729', False), ('20260730', True), ('20260731', True)],
                         ids=['4-before', '5-before', '6-before', '4-after', '5-after', '6-after'])
def test_date_hold_starts_at_5_days_in_either_direction(project, tmp_path, study_date, held):
    """README: a StudyDate 5 or more days from the ZIP-name date holds the ZIP; a smaller difference only warns."""
    upload = archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path, fields={'StudyDate': study_date}))
    assert call(project, 'prepare') == (2 if held else 0)
    assert upload.exists() == held and (KEY in state(project)['sessions']) == (not held)
    assert bool(prepare_issues(project)) == held
    assert (f'date:{KEY}' in state(project)['issues']) == (not held)


def test_renaming_a_date_held_zip_clears_the_hold(project, tmp_path):
    """Runbook: if the name is wrong, rename the ZIP to the right date and rerun; the old issue clears itself."""
    upload = archive(project, 'S001_20260822.zip', dicom_bytes(tmp_path))  # the headers say 20260725: 28 days
    assert call(project, 'prepare', '--allow-unlisted-dates') == 2 and prepare_issues(project)
    upload.rename(upload.with_name('S001_20260725.zip'))
    assert call(project, 'prepare') == 0
    assert state(project)['sessions'][KEY]['status'] == 'ready' and not state(project)['issues']


@pytest.mark.parametrize('resolution', ['accept', 'rename'])
def test_date_hold_on_a_zip_placed_in_subjects_sorted(project, tmp_path, resolution):
    """A ZIP put directly in SubjectsSorted (as in production) is held in place, then accepted or renamed there."""
    folder = project / 'DicomUploads/SubjectsSorted/S001'
    folder.mkdir(parents=True)
    placed = folder / 'S001_20260822.zip'  # the headers say 20260725: 28 days
    with zipfile.ZipFile(placed, 'w') as z:
        z.writestr('exam/1', dicom_bytes(tmp_path))
    digest = fsutil.sha256(placed)
    assert call(project, 'prepare', '--allow-unlisted-dates') == 2
    assert fsutil.sha256(placed) == digest and not (project / 'DicomExtracted/Subjects').exists()
    [issue] = prepare_issues(project)
    assert state(project)['issues'][issue]['path'] == str(placed)
    if resolution == 'accept':
        key, final = 'S001/20260822', placed
        assert call(project, 'prepare', '--allow-unlisted-dates', '--accept-date-mismatch', key) == 0
    else:
        key, final = KEY, placed.rename(folder / 'S001_20260725.zip')
        assert call(project, 'prepare') == 0
    entry = state(project)['sessions'][key]
    assert (entry['status'], entry['archive']) == ('ready', str(final)) and fsutil.sha256(final) == digest
    assert not prepare_issues(project)


def test_accept_date_mismatch_rejects_a_malformed_key(project, capsys):
    assert call(project, 'prepare', '--accept-date-mismatch', 'S001-20260822') == 2
    assert 'Invalid session key' in capsys.readouterr().err and not (project / 'Reports').exists()


def test_accept_date_mismatch_for_a_session_that_is_not_held_changes_nothing(project, tmp_path, monkeypatch):
    prepared(project, tmp_path)
    monkeypatch.setattr(prepare, 'prepare_one', fail_if_called)
    assert call(project, 'prepare', '--accept-date-mismatch', KEY) == 0


def test_accept_date_mismatch_does_not_release_a_multi_study_hold(project, tmp_path):
    upload = archive_objects(project, 'S001_20260725.zip', [dicom_bytes(tmp_path), dicom_bytes(tmp_path)])
    assert call(project, 'prepare') == 2
    assert call(project, 'prepare', '--accept-date-mismatch', KEY) == 2
    assert upload.exists() and KEY not in state(project)['sessions']
    [issue] = prepare_issues(project)
    assert 'holds 2 studies' in state(project)['issues'][issue]['reason']


def test_accepted_date_mismatch_lasts_for_those_zip_bytes_only(project, tmp_path):
    """The acceptance survives a later re-extraction; another export under the same name is still a conflict."""
    key, options = 'S001/20260822', ('--allow-unlisted-dates',)
    archive(project, 'S001_20260822.zip', dicom_bytes(tmp_path))  # the headers say 20260725: 28 days
    assert call(project, 'prepare', *options, '--accept-date-mismatch', key) == 0
    extracted = project / 'DicomExtracted/Subjects/S001/20260822/dicom'
    next(extracted.iterdir()).unlink()  # makes the next plain run extract the ZIP again
    assert call(project, 'prepare', *options) == 0
    assert state(project)['sessions'][key]['status'] == 'ready' and not prepare_issues(project)
    assert len(list(extracted.iterdir())) == 1
    other = archive(project, 'S001_20260822.zip', dicom_bytes(tmp_path, value=9))
    assert call(project, 'prepare', *options) == 2
    assert other.exists() and 'Different DICOM contents' in json.dumps(state(project)['issues'])


def test_roster_issue_never_outlives_its_cause(project, tmp_path):
    """SB-1: a roster gap does not hold an already prepared ZIP, and a roster issue clears once fixed."""
    # 20260802 is not in the roster.
    archive(project, 'S001_20260802.zip', dicom_bytes(tmp_path, fields={'StudyDate': '20260802'}))
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


# ---- Round 2: damaged objects, slow members, changed ZIPs, letter case (SB-18, SB-19, SB-22, SB-23) ----

def upload_members(project, members, compression=zipfile.ZIP_STORED):
    """A ZIP in Uploads/ with the given {member name: bytes}."""
    upload = project / 'DicomUploads/Uploads/S001_20260725.zip'
    upload.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(upload, 'w', compression) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return upload


@pytest.mark.parametrize('cut', ['dicm-mark-only', 'inside-the-file-meta', 'before-rows'])
def test_object_cut_inside_its_header_fails_the_zip(project, tmp_path, cut):
    """SB-18: an object truncated before it was zipped (so its CRC is fine) fails the ZIP; it was left out."""
    _, objects = mr_series(tmp_path, slices=3)
    data = objects[1]
    at = {'dicm-mark-only': 132, 'inside-the-file-meta': 150,
          'before-rows': data.index(b'\x28\x00\x10\x00US', 132)}[cut]
    objects[1] = data[:at]
    upload = archive_objects(project, 'S001_20260725.zip', objects)
    assert call(project, 'prepare') == 2
    assert upload.exists() and not (project / EXTRACTED).exists()
    [issue] = prepare_issues(project)
    assert 'Unreadable DICOM header' in state(project)['issues'][issue]['reason']


@pytest.mark.parametrize('content', [bytes(range(256)) * 16, bytes(4096), b'scan notes\n' * 200],
                         ids=['random', 'zeros', 'text'])
def test_unreadable_member_named_dcm_fails_the_zip(project, tmp_path, content):
    """Plan B-11: a member named .dcm must be a readable object."""
    upload = upload_members(project, {'exam/IM1': dicom_bytes(tmp_path), 'exam/garbage.dcm': content})
    assert call(project, 'prepare') == 2
    assert upload.exists() and 'Unreadable DICOM header' in json.dumps(state(project)['issues'])


def test_large_zero_filled_member_is_omitted_quickly(project, tmp_path):
    """SB-22: 20 MB of zeros without a skipped extension took about 10 s to parse; it is now skipped at once."""
    upload_members(project, {'exam/IM1': dicom_bytes(tmp_path), 'exam/padding': bytes(20 * 1024 * 1024)},
                   zipfile.ZIP_DEFLATED)
    started = time.monotonic()
    assert call(project, 'prepare') == 0
    assert time.monotonic() - started < 5
    assert (inventory(project)['n_dicom'], inventory(project)['skipped_files']) == (1, 1)


def test_audit_replaces_a_leftover_issue_with_the_changed_zip_hold(project, tmp_path, monkeypatch):
    """A leftover issue from a failed --force-reextract must not hide a change that --audit finds later."""
    canonical = prepared(project, tmp_path)
    usage = namedtuple('usage', 'total used free')
    with monkeypatch.context() as patch:
        patch.setattr(shutil, 'disk_usage', lambda path: usage(10**12, 10**12 - 1000, 1000))
        assert call(project, 'prepare', '--force-reextract') == 2
    info, data = canonical.stat(), bytearray(canonical.read_bytes())
    data[len(data) // 2] ^= 0xFF
    canonical.write_bytes(bytes(data))
    os.utime(canonical, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert call(project, 'prepare', '--audit') == 2
    [issue] = prepare_issues(project)
    assert state(project)['issues'][issue]['reason'] == prepare.AUDIT_HOLD
    assert call(project, 'prepare') == 2 and prepare_issues(project) == [issue]


def test_changed_zip_hold_survives_plain_runs_until_the_bytes_are_back(project, tmp_path):
    """SB-19: a same-size, same-time edit found by --force-reextract stays held; restoring the bytes clears it."""
    canonical = prepared(project, tmp_path)
    info, original = canonical.stat(), canonical.read_bytes()
    data = bytearray(original)
    data[len(data) // 2] ^= 0xFF
    canonical.write_bytes(bytes(data))
    os.utime(canonical, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert call(project, 'prepare', '--force-reextract') == 2
    [issue] = prepare_issues(project)
    assert 'Accepted canonical ZIP changed' in state(project)['issues'][issue]['reason']
    assert call(project, 'prepare') == 2 and prepare_issues(project) == [issue]
    canonical.write_bytes(original)
    os.utime(canonical, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert call(project, 'prepare') == 0 and not state(project)['issues']


@pytest.mark.skipif(os.name != 'nt', reason='Windows paths ignore letter case')
@pytest.mark.parametrize('rename', ['extension', 'folder'])
def test_canonical_zip_renamed_in_letter_case_stays_cached(project, tmp_path, monkeypatch, rename):
    """SB-23: renaming a prepared ZIP or its folder in letter case left a permanent missing: issue."""
    canonical = prepared(project, tmp_path)
    if rename == 'extension':
        renamed = canonical.rename(canonical.with_name('S001_20260725.ZIP'))
    else:
        renamed = canonical.parent.rename(canonical.parent.with_name('s001')) / canonical.name
    assert call(project, 'prepare') == 0
    assert not state(project)['issues'] and state(project)['sessions'][KEY]['archive'] == str(renamed)
    monkeypatch.setattr(fsutil, 'sha256', fail_if_called)
    assert call(project, 'prepare') == 0 and not state(project)['issues']


# ---- Round 2: member names, DICOMDIR, changing uploads, missing UIDs (plan B-08, B-10, B-13, C-01) ----

def test_member_names_never_become_paths(project, tmp_path):
    """README: member paths are never used as destination paths; every object gets a numbered name (B-08)."""
    study = generate_uid()
    names = ['/abs/IM1', 'C:/drive/IM2', '//server/share/IM3', '../../escape/IM4', 'back\\slash\\IM5', 'dup/IM6',
             'dup/IM6', 'L' * 300, 'IMe_7xx']
    upload = project / 'DicomUploads/Uploads/S001_20260725.zip'
    upload.parent.mkdir(parents=True)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')  # zipfile warns about the duplicate name
        with zipfile.ZipFile(upload, 'w') as z:
            for name in names:
                z.writestr(name, dicom_bytes(tmp_path, study=study))
            link = zipfile.ZipInfo('exam/link')
            link.external_attr = 0o120777 << 16  # a symbolic link member
            z.writestr(link, '../../../outside')
    upload.write_bytes(upload.read_bytes().replace(b'IMe_7xx', b'IM\xff_7xx'))  # a name that is not UTF-8
    before = sorted(tmp_path.iterdir())
    assert call(project, 'prepare') == 0
    assert (inventory(project)['n_dicom'], inventory(project)['skipped_files']) == (len(names), 1)
    assert [Path(f['name']).name for f in inventory(project)['files']] == [f'{i:08d}.dcm' for i in range(1, 10)]
    assert sorted(tmp_path.iterdir()) == before


def test_dicomdir_and_viewer_copies_are_omitted(project, tmp_path):
    """README: DICOMDIR and viewer folders are omitted; extensions in the skip list are omitted (B-10)."""
    import pydicom
    from pydicom.fileset import FileSet
    study = generate_uid()
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes(tmp_path, study=study, slice_index=0)))
    ds.StudyTime, ds.StudyID, ds.PatientSex, ds.PatientBirthDate = '101112', '1', 'O', ''
    media = tmp_path / 'media'
    fs = FileSet()
    fs.add(ds)
    fs.write(media)
    members = {p.relative_to(media).as_posix(): p.read_bytes() for p in sorted(media.rglob('*')) if p.is_file()}
    assert 'DICOMDIR' in members
    upload_members(project, dict(members, **{'DDV/hidden': dicom_bytes(tmp_path, study=study),
                                             'exam/x.bin': dicom_bytes(tmp_path, study=study)}))
    assert call(project, 'prepare') == 0
    assert (inventory(project)['n_dicom'], inventory(project)['skipped_files']) == (1, 3)


@pytest.mark.parametrize('when', ['hashing', 'extraction', 'move'])
def test_zip_changing_while_processed_fails_then_retries(project, tmp_path, monkeypatch, when):
    """README: a partly copied ZIP fails and is retried when complete; here its time changes mid-run (B-13)."""
    upload = archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))

    def bump():
        info = upload.stat()
        os.utime(upload, ns=(info.st_atime_ns, info.st_mtime_ns + 5_000_000_000))

    target, name = {'hashing': (fsutil, 'sha256'), 'extraction': (prepare, 'extract_archive'),
                    'move': (prepare, 'days_apart')}[when]
    original = getattr(target, name)

    def then_bump(*args, **kwargs):
        result = original(*args, **kwargs)
        bump()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(target, name, then_bump)
        assert call(project, 'prepare') == 2
    reason = {'hashing': 'Upload changed during hashing', 'extraction': 'Upload changed during extraction',
              'move': 'Upload changed while being processed'}[when]
    assert reason in json.dumps(state(project)['issues'])
    assert upload.exists() and not (project / EXTRACTED).exists()
    assert call(project, 'prepare') == 0 and not state(project)['issues']


@pytest.mark.parametrize('fields, outcome', [
    ({'SOPInstanceUID': None}, 'DICOM object missing SOPInstanceUID'),
    ({'SeriesInstanceUID': None}, 'DICOM object missing SeriesInstanceUID'),
    ({'SeriesInstanceUID': None, 'Modality': None}, 'omitted'),
    ({'StudyInstanceUID': None}, 'accepted'),
], ids=['no-sop-instance-uid', 'modality-without-series-uid', 'neither', 'no-study-uid'])
def test_objects_missing_uids(project, tmp_path, fields, outcome):
    """Plan C-01; an object without a Study UID is not counted as a study (pin)."""
    study = generate_uid()
    upload = upload_members(project, {'exam/good': dicom_bytes(tmp_path, study=study),
                                      'exam/odd': dicom_bytes(tmp_path, study=study, fields=fields)})
    if outcome.startswith('DICOM'):
        assert call(project, 'prepare') == 2
        assert upload.exists() and outcome in json.dumps(state(project)['issues'])
        return
    assert call(project, 'prepare') == 0
    expected = (1, 1) if outcome == 'omitted' else (2, 0)
    assert (inventory(project)['n_dicom'], inventory(project)['skipped_files']) == expected
    assert inventory(project)['study_count'] == 1 and not state(project)['sessions'][KEY]['accepted_multi_study']


# ---- Round 2: holds and refusals (plan E-06 and coverage gaps) ------------------------

def test_date_held_zip_renamed_to_an_unlisted_date(project, tmp_path):
    """Plan E-06: renaming a date-held ZIP to a date the roster lacks gives one new issue, on the new path."""
    held = archive(project, 'S001_20260801.zip', dicom_bytes(tmp_path))  # the headers say 20260725: 7 days
    assert call(project, 'prepare') == 2
    assert '7 days' in state(project)['issues'][f'prepare:{held}']['reason']
    renamed = held.rename(held.with_name('S001_20260901.zip'))
    assert call(project, 'prepare') == 2
    assert list(state(project)['issues']) == [f'prepare:{renamed}']
    assert state(project)['issues'][f'prepare:{renamed}']['reason'] == prepare.UNLISTED


def test_upload_whose_canonical_path_is_taken_fails(project, tmp_path):
    """Nothing overwrites a canonical ZIP, even one that failed; both files stay."""
    taken = project / CANONICAL
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b'not a zip')
    upload = archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 2
    assert 'Canonical ZIP already exists; refusing to overwrite it' in state(project)['issues'][f'prepare:{upload}']['reason']
    assert upload.exists() and taken.read_bytes() == b'not a zip'


def test_extraction_that_does_not_match_the_zip_is_held(project, tmp_path):
    """With state lost, an extraction on disk is adopted only after the file check; otherwise it is left alone."""
    prepared(project, tmp_path)
    shutil.rmtree(project / 'Reports')
    next((project / EXTRACTED / 'dicom').iterdir()).unlink()
    assert call(project, 'prepare') == 2
    [issue] = prepare_issues(project)
    assert state(project)['issues'][issue]['reason'] == ('The extraction on disk does not match this ZIP and was '
                                                         'left untouched')
    assert (project / CANONICAL).exists()


def test_audit_finds_an_object_edited_in_place(project, tmp_path, converter):
    """README: --audit hashes cached outputs; an extracted object edited in place is found and extracted again."""
    prepared(project, tmp_path)
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    edited = next((project / EXTRACTED / 'dicom').iterdir())
    info, data = edited.stat(), bytearray(edited.read_bytes())
    data[len(data) // 2] ^= 0xFF
    edited.write_bytes(bytes(data))
    os.utime(edited, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert call(project, 'prepare') == 0  # cached: nothing looks changed without hashing
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--audit') == 2
    assert 'Extracted input changed or missing' in state(project)['issues'][f'convert:{KEY}']['reason']
    assert call(project, 'prepare', '--audit') == 2  # extracted again; the convert: issue is still open
    assert edited.read_bytes() != bytes(data)
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0 and not state(project)['issues']


def test_symbolic_link_zip_is_held(project, tmp_path):
    """A symbolic-link ZIP is held, and its target is left alone."""
    target = tmp_path / 'elsewhere.zip'
    with zipfile.ZipFile(target, 'w') as z:
        z.writestr('exam/1', dicom_bytes(tmp_path))
    link = project / 'DicomUploads/Uploads/S001_20260725.zip'
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip('symbolic links cannot be created here')
    digest = fsutil.sha256(target)
    assert call(project, 'prepare') == 2
    assert 'Symbolic-link ZIPs are not accepted' in json.dumps(state(project)['issues'])
    assert link.is_symlink() and fsutil.sha256(target) == digest
