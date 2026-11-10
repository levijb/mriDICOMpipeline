"""Conversion and report tests (T08, T11-T14, T18; P1, P4, P8, P10)."""
import csv
import gzip
import json
import os
from pathlib import Path
import shutil
import threading
import time

from pydicom.uid import generate_uid
import pytest

from mri_dicom_pipeline import __version__, classify, convert
from support import archive, archive_objects, call, dicom_bytes, mr_series, state

KEY = 'S001/20260725'
NIFTI = 'Niftis/Subjects/S001/20260725'


def fail_if_called(*args, **kwargs):
    pytest.fail('unexpected call')


def converted(project, tmp_path, converter, objects=None):
    """Prepare and convert one session; returns its published NIfTI folder."""
    if objects is None:
        archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    else:
        archive_objects(project, 'S001_20260725.zip', objects)
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    return project / NIFTI


def receipt(folder):
    return json.loads((folder / '.pipeline_complete.json').read_text())


def conversion(project):
    return state(project)['sessions'][KEY]['conversion']


# ---- Coverage and reconversion (Q6, Q7) ------------------------------------------

def test_missing_required_series_is_partial(project, tmp_path, converter, monkeypatch):
    """P1: a converter run that skips a series is partial, even with exit code 0."""
    study = generate_uid()
    _, first = mr_series(tmp_path, study=study, series_number=2)
    skipped, second = mr_series(tmp_path, study=study, series_number=4)
    archive_objects(project, 'S001_20260725.zip', first + second)
    assert call(project, 'prepare') == 0
    monkeypatch.setenv('FAKE_CONVERTER_SKIP', skipped)
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert conversion(project)['status'] == 'partial'
    assert not (project / NIFTI).exists()
    assert 'Missing required series' in json.dumps(state(project)['issues'])


def test_complete_session_not_reconverted_when_folder_modified(project, tmp_path, converter, monkeypatch):
    """P10: a file added to a complete folder stays, and nothing is reconverted."""
    folder = converted(project, tmp_path, converter)
    added = folder / 'ADC' / 'notes.txt'
    added.write_text('analysis notes')
    monkeypatch.setattr(convert, 'convert_one', fail_if_called)
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert added.read_text() == 'analysis notes'
    assert f'output:{KEY}' in state(project)['issues']


def test_force_reconvert_refuses_folder_with_extra_files(project, tmp_path, converter):
    folder = converted(project, tmp_path, converter)
    before = receipt(folder)['receipt_id']
    added = folder / 'ADC' / 'notes.txt'
    added.write_text('analysis notes')
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--force-reconvert') == 2
    assert added.exists() and receipt(folder)['receipt_id'] == before
    assert 'files not in inventory' in json.dumps(state(project)['issues'])
    added.unlink()
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--force-reconvert') == 0
    assert receipt(folder)['receipt_id'] != before
    assert f'output:{KEY}' not in state(project)['issues']


def test_failed_reconversion_keeps_last_good(project, tmp_path, converter, monkeypatch):
    """P4: a failed explicit reconversion leaves the complete folder and its record in place."""
    folder = converted(project, tmp_path, converter)
    before = conversion(project)
    monkeypatch.setenv('FAKE_CONVERTER_EXIT', '1')
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--force-reconvert') == 2
    entry = state(project)['sessions'][KEY]
    assert entry['conversion']['status'] == 'converted'
    assert entry['conversion']['receipt_id'] == before['receipt_id'] == receipt(folder)['receipt_id']
    assert entry['last_attempt']['status'] == 'partial' and '.pending' in entry['last_attempt']['output']


def test_unreferenced_complete_folder_adopted_not_replaced(project, tmp_path, converter, monkeypatch):
    folder = converted(project, tmp_path, converter)
    before = receipt(folder)['receipt_id']
    shutil.rmtree(project / 'Reports/.state')
    assert call(project, 'prepare') == 0
    monkeypatch.setattr(convert, 'convert_one', fail_if_called)
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    assert conversion(project)['status'] == 'converted' and receipt(folder)['receipt_id'] == before


def test_restored_folder_revalidated_by_audit_session(project, tmp_path, converter):
    """Q7: a folder restored from a backup is accepted again by --audit --session."""
    folder = converted(project, tmp_path, converter)
    backup = tmp_path / 'backup'
    shutil.copytree(folder, backup)
    shutil.rmtree(folder)
    shutil.copytree(backup, folder)
    # A restore tool that does not keep folder times (copytree does keep them).
    for directory in [folder, *(p for p in folder.rglob('*') if p.is_dir())]:
        os.utime(directory)
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert f'output:{KEY}' in state(project)['issues']
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--audit', '--session', KEY) == 0
    assert f'output:{KEY}' not in state(project)['issues']
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0


# ---- Output bytes and privacy (Q10, P8) ------------------------------------------

def test_published_nifti_is_byte_identical_to_converter_output(project, tmp_path, converter, monkeypatch):
    """Q10: each published .nii.gz decompresses to exactly the converter's bytes."""
    raw = tmp_path / 'raw_copy'
    monkeypatch.setenv('FAKE_CONVERTER_KEEP', str(raw))
    folder = converted(project, tmp_path, converter)
    [published], [original] = list(folder.rglob('*.nii.gz')), list(raw.glob('*.nii'))
    assert gzip.decompress(published.read_bytes()) == original.read_bytes()


def test_overlay_mask_published_next_to_its_volume(project, tmp_path, converter, monkeypatch):
    """dcm2niix writes a DICOM overlay plane as <volume>_ROI1.nii with no sidecar (seen in production)."""
    raw = tmp_path / 'raw_copy'
    monkeypatch.setenv('FAKE_CONVERTER_KEEP', str(raw))
    monkeypatch.setenv('FAKE_CONVERTER_OVERLAY', '1')
    folder = converted(project, tmp_path, converter)
    [volume] = [p for p in folder.rglob('*.nii.gz') if '_ROI' not in p.name]
    mask = volume.with_name(volume.name[:-len('.nii.gz')] + '_ROI1.nii.gz')
    [original] = raw.glob('*_ROI1.nii')
    assert gzip.decompress(mask.read_bytes()) == original.read_bytes()
    inventory = json.loads((folder / '.inventory.json').read_text())['files']
    assert mask.relative_to(folder).as_posix() in {item['name'] for item in inventory}
    assert conversion(project)['status'] == 'converted' and conversion(project)['n_volumes'] == 1


def test_sidecar_keeps_dicom_study_date_apart_from_name_date(project, tmp_path, converter):
    """session_date comes from the ZIP name; dicom_study_date is the StudyDate in the DICOM headers."""
    archive(project, 'S001_20260801.zip', dicom_bytes(tmp_path))  # the headers say 20260725
    assert call(project, 'prepare') == 0
    assert 'date:S001/20260801' in state(project)['issues']
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    [sidecar] = [p for p in (project / 'Niftis/Subjects/S001/20260801').rglob('*.json') if not p.name.startswith('.')]
    metadata = json.loads(sidecar.read_text())
    assert metadata['session_date'] == '20260801' and metadata['dicom_study_date'] == '20260725'


def test_header_text_passes_through_and_sidecar_stays_filtered(project, tmp_path, converter):
    """T14: NIfTI header text reaches the output (a documented channel); sidecars and logs stay filtered."""
    folder = converted(project, tmp_path, converter)
    [published] = folder.rglob('*.nii.gz')
    assert b'FAKE_PRIVATE_NAME' in gzip.decompress(published.read_bytes())
    for sidecar in folder.rglob('*.json'):
        assert 'FAKE_PRIVATE' not in sidecar.read_text()
    assert 'FAKE_PRIVATE' not in (project / 'Reports/latest_convert.log').read_text()


def test_raw_converter_output_never_written(project, tmp_path, converter, monkeypatch):
    """P8: identifying converter output is redacted before it reaches the staging log."""
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    printed = tmp_path / 'printed'
    monkeypatch.setenv('FAKE_CONVERTER_PRINTED', str(printed))
    monkeypatch.setenv('FAKE_CONVERTER_SLEEP', '3')
    seen = []

    def read_log_while_converter_sleeps():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            logs = list((project / '.work').glob('convert_*/converter.log'))
            text = logs[0].read_text(errors='replace') if printed.exists() and logs else ''
            if 'identifying metadata diagnostic omitted' in text or 'FAKE_PRIVATE' in text:
                seen.append(text)
                return
            time.sleep(0.05)

    watcher = threading.Thread(target=read_log_while_converter_sleeps)
    watcher.start()
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    watcher.join()
    assert seen and 'FAKE_PRIVATE' not in seen[0]


# ---- Provenance and labels (Q8, Q11) ------------------------------------------------

def test_provenance_recorded_in_receipt_and_state(project, tmp_path, converter):
    folder = converted(project, tmp_path, converter)
    recorded = receipt(folder)['provenance']
    assert recorded['package_version'] == __version__ and recorded['run_id']
    assert recorded['converter'].endswith('fake_converter.py')
    assert recorded['arguments'][:2] == ['-z', 'n'] and '-o' not in recorded['arguments']
    assert recorded['converter_versions'] == ['fake-1.0']
    assert conversion(project)['provenance'] == recorded


@pytest.mark.parametrize('description, image_type, label', [
    ('AX T2* GRE', None, 'GRE'), ('AX T2 STAR GRE', None, 'GRE'), ('SWI mIP', None, 'SWI_MINIP'),
    ('SWI_MinIP', None, 'SWI_MINIP'), ('HEADCOIL T1', None, 'T1'), ('PROGRESSIVE', None, 'OTHER'),
    ('T1 FLAIR', None, 'FLAIR'), ('AX Diffusion_ADC', None, 'ADC'), ('SAGITTAL T1 VOLUME POST', None, 'T1_POST'),
    ('T1 MPRAGE +C', None, 'T1_POST'), ('Localizer', None, 'LOCALIZER'), ('3D TOF', None, 'MRA'),
    ('pCASL perfusion', None, 'PERFUSION'), ('AXIAL SWI', 'ORIGINAL\\PRIMARY\\M', 'SWI'),
    ('AXIAL SWI', ['ORIGINAL', 'PRIMARY', 'P'], 'SWI_PHASE'), ('AXIAL SWI', ['ORIGINAL', 'PRIMARY', 'PHASE'], 'SWI_PHASE'),
])
def test_classifier_revision_2_table(description, image_type, label):
    assert classify.classify(description, image_type=image_type) == label


def test_prepare_and_convert_labels_agree(project, tmp_path, converter):
    _, objects = mr_series(tmp_path, description='AX T2* GRE')
    folder = converted(project, tmp_path, converter, objects)
    inventory = json.loads((project / 'DicomExtracted/Subjects/S001/20260725/.inventory.json').read_text())
    assert [row['scan_type'] for row in inventory['series']] == ['GRE']
    assert [p.name for p in folder.iterdir() if p.is_dir()] == ['GRE']


# ---- Selection and reports ----------------------------------------------------------

def read_status(project):
    with (project / 'Reports/session_status.csv').open(newline='') as f:
        return {f"{row['subject_id']}/{row['session']}": row for row in csv.DictReader(f)}


def test_status_columns_and_last_attempt_reported(project, tmp_path, converter, monkeypatch):
    """T18: coverage, the last attempt, and output_modified appear in session_status.csv."""
    folder = converted(project, tmp_path, converter)
    row, series_key = read_status(project)[KEY], conversion(project)['coverage']['required'][0]
    assert (row['required_series'], row['converted_series'], row['excused_series']) == (series_key, series_key, '')
    monkeypatch.setenv('FAKE_CONVERTER_EXIT', '1')
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--force-reconvert') == 2
    row = read_status(project)[KEY]
    assert row['conversion_status'] == 'converted' and row['last_attempt_status'] == 'partial'
    assert '.pending' in row['last_attempt_folder']
    monkeypatch.delenv('FAKE_CONVERTER_EXIT')
    (folder / 'ADC' / 'notes.txt').write_text('added')
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert read_status(project)[KEY]['conversion_status'] == 'output_modified'


def test_report_file_held_open_does_not_stop_run(project, tmp_path, monkeypatch, caplog):
    """Q2: a report file held open (for example by Excel) is logged and retried; the run continues."""
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    real_replace = os.replace

    def locked(src, dst, *args, **kwargs):
        if Path(dst).name == 'session_status.csv':
            raise PermissionError(13, 'The file is being used by another process')
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, 'replace', locked)
    assert call(project, 'prepare') == 0
    assert 'session_status.csv' in caplog.text
    assert state(project)['sessions'][KEY]['status'] == 'ready'
    with (project / 'Reports/run_history.csv').open(newline='') as f:
        assert list(csv.DictReader(f))[-1]['outcome'] == 'ok'

def test_session_filter_limits_work(project, tmp_path, converter):
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    archive(project, 'S001_20260801.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--session', KEY) == 0
    sessions = state(project)['sessions']
    assert sessions[KEY]['conversion']['status'] == 'converted'
    assert not sessions['S001/20260801'].get('conversion')
