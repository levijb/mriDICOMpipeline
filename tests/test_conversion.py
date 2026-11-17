"""Conversion and report tests (T08, T11-T14, T18; P1, P4, P8, P10)."""
from collections import namedtuple
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import threading
import time
import zipfile

from pydicom.uid import generate_uid
import pytest

from mri_dicom_pipeline import __version__, classify, convert, nifti, prepare
from support import archive, archive_objects, call, dicom_bytes, mr_series, state, write_nifti

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


def test_series_sharing_number_and_time_both_convert(project, tmp_path, converter, monkeypatch):
    """dcm2niix names Siemens XA classic exports by SeriesTime in place of the SeriesInstanceUID (its
    issue 394), so output names carry each volume's first image UID instead. A scan and its one-image
    key-image series share a series number and time; both convert (production run of 2026-11-16)."""
    monkeypatch.setenv('FAKE_CONVERTER_XA', '1')
    study, scan, key_images, fields = generate_uid(), generate_uid(), generate_uid(), {'SeriesTime': '164343'}
    objects = [dicom_bytes(tmp_path, study=study, series=uid, series_number=3, description=description, fields=fields)
               for uid, description in ((scan, 'SAG FLAIR'), (key_images, 'KEY_IMAGES PR'))]
    folder = converted(project, tmp_path, converter, objects)
    sidecars = [p for p in folder.rglob('*.json') if not p.name.startswith('.')]
    assert {json.loads(p.read_text())['source_series_key'] for p in sidecars} == {
        hashlib.sha256(uid.encode()).hexdigest()[:20] for uid in (scan, key_images)}


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


def test_force_reconvert_rebuilds_a_deleted_folder(project, tmp_path, converter):
    """SB-2: the documented recovery for a complete folder that disappeared."""
    folder = converted(project, tmp_path, converter)
    shutil.rmtree(folder)
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--force-reconvert', '--session', KEY) == 0
    assert (folder / '.pipeline_complete.json').exists()
    assert f'output:{KEY}' not in state(project)['issues']


def test_force_reconvert_after_lost_state_keeps_added_files(project, tmp_path, converter):
    """SB-7: with state lost, an explicit reconversion still refuses a folder holding files people added."""
    folder = converted(project, tmp_path, converter)
    added = folder / 'ADC' / 'notes.txt'
    added.write_text('analysis notes')
    shutil.rmtree(project / 'Reports/.state')
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--force-reconvert') == 2
    assert added.exists() and 'files not in inventory' in json.dumps(state(project)['issues'])


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


def test_nifti_folder_without_a_receipt_is_never_replaced(project, tmp_path, converter):
    """README: the commands refuse to overwrite session directories that lack the ownership receipt (plan F-08)."""
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    foreign = project / NIFTI / 'someone_elses_file.txt'
    foreign.parent.mkdir(parents=True)
    foreign.write_text('not made by the pipeline')
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert [p.name for p in (project / NIFTI).iterdir()] == ['someone_elses_file.txt']
    assert foreign.read_text() == 'not made by the pipeline'
    assert 'not owned by this pipeline' in state(project)['issues'][f'convert:{KEY}']['reason']


# ---- Converter failures (plan D-08) ---------------------------------------------------

def test_converter_timeout_is_not_success_and_is_not_retried(project, tmp_path, converter, monkeypatch):
    """README: a timeout cannot become a successful cache entry; the working files are kept."""
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    monkeypatch.setenv('FAKE_CONVERTER_SLEEP', '60')
    started = time.monotonic()
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--timeout', 1) == 2
    assert time.monotonic() - started < 30
    assert 'Converter timed out' in state(project)['issues'][f'convert:{KEY}']['reason']
    assert conversion(project)['status'] == 'partial' and not (project / NIFTI).exists()
    assert list((project / '.work').glob('convert_*/converter.log'))
    monkeypatch.delenv('FAKE_CONVERTER_SLEEP')
    with monkeypatch.context() as patch:
        patch.setattr(convert, 'convert_one', fail_if_called)
        assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--retry-failed') == 0
    assert conversion(project)['status'] == 'converted'


def test_missing_converter_stops_the_run_before_any_work(project, tmp_path, capsys):
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', tmp_path / 'no_such_dcm2niix') == 2
    assert 'dcm2niix not found' in capsys.readouterr().err
    assert not state(project)['sessions'][KEY].get('conversion') and not state(project)['issues']
    assert not list((project / '.work').glob('convert_*'))
    with (project / 'Reports/run_history.csv').open(newline='') as f:
        assert list(csv.DictReader(f))[-1]['outcome'] == 'aborted'


def test_converter_that_cannot_start_fails_the_session(project, tmp_path):
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    broken = tmp_path / 'dcm2niix_broken.exe'
    broken.write_text('not a program')
    assert call(project, 'convert', '--dcm2niix-bin', broken) == 2
    assert conversion(project)['status'] == 'failed' and not (project / NIFTI).exists()
    assert 'working files retained' in state(project)['issues'][f'convert:{KEY}']['reason']


# ---- Organizing converter output (plan D-01; the _ROI1 failure of 2026-11-10) -----------

SERIES_UID, IMAGE_UID = '1.2.826.0.1.3680043.8.498.1', '1.2.826.0.1.3680043.8.498.2'
SERIES_KEY = hashlib.sha256(SERIES_UID.encode()).hexdigest()[:20]
STEM = f'series_{IMAGE_UID}_5_1'
# Suffixes dcm2niix adds to an output name: echo, phase, real and imaginary parts, coil, unstacked
# image number, name collisions, derived ADC, motion-corrected, equalized spacing, tilt-corrected.
NAMED_VARIANTS = ['', '_e2', '_ph', '_e2_ph', '_real', '_imaginary', '_c1', '_i00001', 'a', 'b', '_ADC', '_MoCo',
                  '_Eq_1', '_Tilt_1']


def raw_output(tmp_path, stems):
    """A hand-built converter output folder: a valid volume per stem, with a sidecar unless it is an _ROI mask."""
    raw = tmp_path / 'raw'
    raw.mkdir()
    for stem in stems:
        write_nifti(raw / (stem + '.nii'))
        if '_ROI' not in stem:
            (raw / (stem + '.json')).write_text(json.dumps({'SeriesDescription': 'AX FLAIR',
                                                            'ConversionSoftwareVersion': 'v1'}))
    return raw


def organize(tmp_path, raw):
    """(volumes, published file names, errors, series keys) for a converter output folder."""
    volumes, files, errors, keys, _ = convert.organize_conversion(raw, tmp_path / 'organized', 'S001', '20260725',
                                                                 'archive-digest', series_of={IMAGE_UID: SERIES_KEY})
    return volumes, sorted(Path(item['name']).name for item in files), errors, keys


def test_organizer_publishes_each_named_variant_under_its_series_key(tmp_path):
    """Every output that has its own sidecar is a volume of the series named at the front of its file name."""
    stems = [STEM + suffix for suffix in NAMED_VARIANTS]
    volumes, names, errors, keys = organize(tmp_path, raw_output(tmp_path, stems))
    assert (volumes, errors, keys) == (len(stems), [], {SERIES_KEY})
    assert sum(name.endswith('.nii.gz') for name in names) == sum(name.endswith('.json') for name in names) == len(stems)


def test_organizer_publishes_every_overlay_mask_with_its_volume(tmp_path):
    """README: an overlay plane is published as <image name>_ROI1.nii.gz, _ROI2 and so on for further planes."""
    stems = [STEM, STEM + '_ROI1', STEM + '_ROI2', STEM + '_ph', STEM + '_ph_ROI1']
    volumes, names, errors, keys = organize(tmp_path, raw_output(tmp_path, stems))
    assert (volumes, errors, keys) == (2, [], {SERIES_KEY})

    def published(stem):
        return 'S001_20260725_FLAIR_' + hashlib.sha256(stem.encode()).hexdigest()[:16]

    assert [name for name in names if '_ROI' in name] == sorted([
        published(STEM) + '_ROI1.nii.gz', published(STEM) + '_ROI2.nii.gz', published(STEM + '_ph') + '_ROI1.nii.gz'])


@pytest.mark.parametrize('kind, published_volumes', [
    ('mask-without-its-volume', 2), ('volume-without-a-sidecar', 2), ('truncated-volume', 1),
    ('stray-text-file', 1), ('file-in-a-subfolder', 1)])
def test_organizer_reports_output_it_cannot_place(tmp_path, kind, published_volumes):
    """Anything the organizer cannot place is an error, so the session cannot become `converted`.

    A volume without a sidecar is still gzipped (the attempt goes to .pending);
    a volume that fails validation is not.
    """
    raw = raw_output(tmp_path, [STEM])
    if kind == 'mask-without-its-volume':
        write_nifti(raw / 'series_1.2.3_7_1_ROI1.nii')
    elif kind == 'volume-without-a-sidecar':
        write_nifti(raw / (STEM + '_e2.nii'))
    elif kind == 'truncated-volume':
        (raw / (STEM + '_e2.nii')).write_bytes((raw / (STEM + '.nii')).read_bytes()[:-2])
        (raw / (STEM + '_e2.json')).write_text('{}')
    elif kind == 'stray-text-file':
        (raw / 'notes.txt').write_text('left by the converter')
    else:
        (raw / 'sub').mkdir()
        (raw / 'sub' / 'notes.txt').write_text('left by the converter')
    volumes, names, errors, keys = organize(tmp_path, raw)
    assert errors and SERIES_KEY in keys
    assert volumes == sum(name.endswith('.nii.gz') for name in names) == published_volumes


def test_organizer_reports_a_sidecar_that_is_not_a_json_object(tmp_path):
    """SB-13: such a sidecar is reported as an error, not a crash, and the volume is still gzipped."""
    raw = raw_output(tmp_path, [STEM])
    (raw / (STEM + '.json')).write_text('["not", "an", "object"]')
    volumes, names, errors, keys = organize(tmp_path, raw)
    assert errors == [f'Missing/invalid JSON sidecar for volume {hashlib.sha256(STEM.encode()).hexdigest()[:12]}']
    assert volumes == 1


def test_volume_not_named_by_series_uid_does_not_count_as_coverage(tmp_path):
    """A volume whose name does not start with series_<UID>_ is published but covers no inventory series."""
    volumes, names, errors, keys = organize(tmp_path, raw_output(tmp_path, ['unexpected_name']))
    assert (volumes, errors) == (1, []) and SERIES_KEY not in keys
    assert all(len(key) == 16 for key in keys)  # inventory series keys have 20 characters, so none can match


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
    archive(project, 'S001_20260801.zip', dicom_bytes(tmp_path, fields={'StudyDate': '20260728'}))  # 4 days: a warning
    assert call(project, 'prepare') == 0
    assert 'date:S001/20260801' in state(project)['issues']
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    [sidecar] = [p for p in (project / 'Niftis/Subjects/S001/20260801').rglob('*.json') if not p.name.startswith('.')]
    metadata = json.loads(sidecar.read_text())
    assert metadata['session_date'] == '20260801' and metadata['dicom_study_date'] == '20260728'


def sidecar_dates(folder):
    """(session_date, dicom_study_date) of every published sidecar."""
    sidecars = [json.loads(p.read_text()) for p in folder.rglob('*.json') if not p.name.startswith('.')]
    return [(sidecar['session_date'], sidecar['dicom_study_date']) for sidecar in sidecars]


def test_sidecar_study_date_is_null_without_a_header_date(project, tmp_path, converter):
    """README: dicom_study_date is null when the headers carry no StudyDate (plan C-02)."""
    folder = converted(project, tmp_path, converter, [dicom_bytes(tmp_path, fields={'StudyDate': None})])
    assert sidecar_dates(folder) == [('20260725', None)]
    assert f'date:{KEY}' not in state(project)['issues']


def test_several_study_dates_are_listed_in_every_sidecar(project, tmp_path, converter):
    """README: several StudyDates are comma-separated (plan C-02, and E-08: every sidecar carries both fields)."""
    study = generate_uid()
    objects = [dicom_bytes(tmp_path, study=study, series_number=number, fields={'StudyDate': date})
               for number, date in ((2, '20260725'), (3, '20260726'))]
    folder = converted(project, tmp_path, converter, objects)
    assert sidecar_dates(folder) == [('20260725', '20260725,20260726')] * 2
    assert f'date:{KEY}' in state(project)['issues']


@pytest.mark.filterwarnings('ignore')  # pydicom warns that the value is not a valid date
def test_study_date_that_is_not_a_date_warns_and_does_not_hold(project, tmp_path, converter):
    """A StudyDate shortened by a de-identification tool: a date: warning, never a hold (plan C-02)."""
    folder = converted(project, tmp_path, converter, [dicom_bytes(tmp_path, fields={'StudyDate': '2026'})])
    assert sidecar_dates(folder) == [('20260725', '2026')]
    assert f'date:{KEY}' in state(project)['issues']


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
    archive(project, 'S001_20260801.zip', dicom_bytes(tmp_path, fields={'StudyDate': '20260801'}))
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--session', KEY) == 0
    sessions = state(project)['sessions']
    assert sessions[KEY]['conversion']['status'] == 'converted'
    assert not sessions['S001/20260801'].get('conversion')


# ---- Round 2: companions, physio logs, converter options (SB-14, SB-15, SB-24) ---------------

def test_organizer_publishes_the_ge_adc_file_as_a_companion(tmp_path):
    """SB-14: a GE diffusion series' combined images come out as <volume>_ADC.nii, with no sidecar."""
    raw = raw_output(tmp_path, [STEM])
    write_nifti(raw / (STEM + '_ADC.nii'))
    volumes, names, errors, keys = organize(tmp_path, raw)
    assert (volumes, errors, keys) == (1, [], {SERIES_KEY})
    assert sum(name.endswith('_ADC.nii.gz') for name in names) == 1


def test_organizer_leaves_physio_logs_out_without_an_error(tmp_path):
    """SB-15: a Siemens physio log becomes two files and no image; they are not published and are no error."""
    raw = raw_output(tmp_path, [STEM])
    for suffix in ('.tsv.gz', '.json'):
        (raw / f'series_1.2.3_99_0_recording-cardiac_physio{suffix}').write_bytes(b'log')
    volumes, names, errors, keys = organize(tmp_path, raw)
    assert (volumes, errors) == (1, []) and not [name for name in names if 'physio' in name]


def test_converter_options_fix_16_bit_scaling():
    """SB-24: dcm2niix can load saved defaults; the pipeline passes -l o so 16-bit data is never rescaled."""
    options = convert.CONVERTER_OPTIONS
    assert options[options.index('-l') + 1] == 'o'


# ---- Round 2: accepting a reviewed partial session (SB-20) -----------------------------

def test_accept_partial_publishes_a_reviewed_session(project, tmp_path, converter, monkeypatch):
    """README: after review, --accept-partial publishes a partial session and records the accepted errors."""
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    monkeypatch.setenv('FAKE_CONVERTER_EXIT', '1')
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert conversion(project)['status'] == 'partial' and not (project / NIFTI).exists()
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--accept-partial', KEY) == 0
    record = conversion(project)
    assert (record['status'], record['accepted_errors']) == ('converted', ['dcm2niix exited with code 1'])
    assert receipt(project / NIFTI)['accepted_errors'] == ['dcm2niix exited with code 1']
    assert not state(project)['issues']
    row = read_status(project)[KEY]
    assert (row['conversion_status'], row['accepted_errors']) == ('accepted_partial', 'dcm2niix exited with code 1')
    # Later plain runs treat the session as done.
    monkeypatch.setattr(convert, 'convert_one', fail_if_called)
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0


def test_accept_partial_needs_a_valid_volume(project, tmp_path, converter, monkeypatch):
    """With no valid volume there is nothing to accept: the session stays partial and the run exits 2."""
    uid = generate_uid()
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path, series=uid))
    assert call(project, 'prepare') == 0
    monkeypatch.setenv('FAKE_CONVERTER_SKIP', uid)
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--accept-partial', KEY) == 2
    assert conversion(project)['status'] == 'partial' and not (project / NIFTI).exists()
    assert 'No valid NIfTI volumes produced' in state(project)['issues'][f'convert:{KEY}']['reason']


def test_accept_partial_never_accepts_a_timeout(project, tmp_path, converter, monkeypatch):
    """A timeout's outcome can differ from run to run, so it is never accepted."""
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    monkeypatch.setenv('FAKE_CONVERTER_SLEEP', '60')
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--timeout', 1, '--accept-partial', KEY) == 2
    assert 'Converter timed out' in state(project)['issues'][f'convert:{KEY}']['reason']
    assert not (project / NIFTI).exists()


def test_needs_conversion_is_not_converted_automatically(project, tmp_path, converter, monkeypatch):
    """D-12: after the extraction changes, the status is needs_conversion; only an explicit reconversion acts."""
    study = generate_uid()
    upload = project / 'DicomUploads/Uploads/S001_20260725.zip'
    upload.parent.mkdir(parents=True)
    with zipfile.ZipFile(upload, 'w') as z:
        z.writestr('exam/IM1', dicom_bytes(tmp_path, study=study))
        z.writestr('exam/IM2.bin', dicom_bytes(tmp_path, study=study, series_number=4))  # omitted at first
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    monkeypatch.setattr(prepare, 'SKIP_EXT', prepare.SKIP_EXT - {'.bin'})
    assert call(project, 'prepare', '--force-reextract') == 0
    assert read_status(project)[KEY]['conversion_status'] == 'needs_conversion'
    with monkeypatch.context() as patch:
        patch.setattr(convert, 'convert_one', fail_if_called)
        assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--force-reconvert', '--session', KEY) == 0
    assert read_status(project)[KEY]['conversion_status'] == 'converted' and conversion(project)['n_volumes'] == 2


# ---- Round 2: diffusion companions, NIfTI validation, sidecar filter (plan D-02, D-03, D-10) ----

@pytest.mark.parametrize('bval, bvec, error', [
    ('0 1000\n', '0 1\n0 0\n0 0\n', None),
    ('', '0 1\n0 0\n0 0\n', 'Invalid diffusion companion values'),
    ('abc', '0 1\n0 0\n0 0\n', 'could not convert string to float'),
    ('0 inf', '0 1\n0 0\n0 0\n', 'Invalid diffusion companion values'),
    ('0 1000', '0 nan\n0 0\n0 0\n', 'Invalid diffusion companion values'),
    ('0 1000\n', None, None),
], ids=['valid', 'empty-bval', 'text-bval', 'inf-bval', 'nan-bvec', 'bval-without-bvec'])
def test_diffusion_companions(tmp_path, bval, bvec, error):
    """Valid .bval/.bvec are copied byte for byte; invalid values are an error. A .bval alone is published (pin)."""
    raw = raw_output(tmp_path, [STEM])
    (raw / (STEM + '.bval')).write_text(bval)
    if bvec is not None:
        (raw / (STEM + '.bvec')).write_text(bvec)
    volumes, files, errors, keys, _ = convert.organize_conversion(raw, tmp_path / 'organized', 'S001', '20260725', 'x')
    if error is None:
        assert errors == []
        for item in files:
            if item['name'].endswith(('.bval', '.bvec')):
                published = (tmp_path / 'organized' / item['name']).read_bytes()
                assert published == (raw / (STEM + Path(item['name']).suffix)).read_bytes()
    else:
        assert any(error in e for e in errors) and any('unresolved converter artifacts' in e for e in errors)


def nifti_bytes(endian='<', version=1, dims=(3, 2, 2, 1, 1, 1, 1, 1), bitpix=16, offset=None, magic=None,
                extension=b'', data=None):
    """A single-file NIfTI-1 or NIfTI-2 image, with the given header values."""
    count = 1
    for d in dims[1:dims[0] + 1]:
        count *= d
    data = bytes(range(1, count * bitpix // 8 + 1)) if data is None else data
    if version == 1:
        offset = 352 + len(extension) if offset is None else offset
        h = bytearray(348)
        struct.pack_into(endian + 'i', h, 0, 348)
        struct.pack_into(endian + '8h', h, 40, *dims)
        struct.pack_into(endian + 'h', h, 70, 4)
        struct.pack_into(endian + 'h', h, 72, bitpix)
        struct.pack_into(endian + 'f', h, 108, offset)
        h[344:348] = b'n+1\0' if magic is None else magic
    else:
        offset = 544 + len(extension) if offset is None else offset
        h = bytearray(540)
        struct.pack_into(endian + 'i', h, 0, 540)
        h[4:12] = b'n+2\0\r\n\x1a\n' if magic is None else magic
        struct.pack_into(endian + 'h', h, 12, 4)
        struct.pack_into(endian + 'h', h, 14, bitpix)
        struct.pack_into(endian + '8q', h, 16, *dims)
        struct.pack_into(endian + 'q', h, 168, offset)
    return bytes(h) + (b'\1\0\0\0' if extension else b'\0\0\0\0') + extension + data


EXTENSION = struct.pack('<ii', 32, 4) + b'x' * 24
NIFTI_CASES = {
    'nifti1-little-endian': (nifti_bytes(), None), 'nifti1-big-endian': (nifti_bytes('>'), None),
    'nifti2-little-endian': (nifti_bytes(version=2), None), 'nifti2-big-endian': (nifti_bytes('>', version=2), None),
    'header-extension': (nifti_bytes(extension=EXTENSION), None), 'trailing-bytes': (nifti_bytes() + b'tail', None),
    '4d': (nifti_bytes(dims=(4, 2, 2, 1, 3, 1, 1, 1)), None),
    'short-data': (nifti_bytes()[:-1], 'Truncated NIfTI pixel data'),
    'two-file-magic': (nifti_bytes(magic=b'ni1\0'), 'Expected single-file NIfTI-1'),
    'bad-nifti2-magic': (nifti_bytes(version=2, magic=b'n+2\0\0\0\0\0'), 'Expected single-file NIfTI-2'),
    'dim0-zero': (nifti_bytes(dims=(0, 2, 2, 1, 1, 1, 1, 1), data=b'12345678'), 'Invalid NIfTI dimensions/bit depth'),
    'zero-dimension': (nifti_bytes(dims=(3, 2, 0, 1, 1, 1, 1, 1), data=b'12345678'), 'Invalid NIfTI dimensions/bit depth'),
    'bitpix-zero': (nifti_bytes(bitpix=0, data=b'12345678'), 'Invalid NIfTI dimensions/bit depth'),
    'offset-348': (nifti_bytes(offset=348), 'Invalid or unusually large NIfTI extension area'),
    'offset-20-mb': (nifti_bytes(offset=20 * 1024 * 1024), 'Invalid or unusually large NIfTI extension area'),
    'header-cut': (nifti_bytes()[:100], 'Truncated NIfTI header'), 'empty': (b'', 'Truncated NIfTI header'),
    'header-size-1': (struct.pack('<i', 1) + bytes(400), 'Invalid NIfTI header size'),
    'extension-cut': (nifti_bytes(extension=EXTENSION)[:360], 'Truncated NIfTI extension area'),
}


@pytest.mark.parametrize('compressed', [False, True], ids=['nii', 'nii.gz'])
@pytest.mark.parametrize('case', sorted(NIFTI_CASES))
def test_nifti_validation_table(tmp_path, case, compressed):
    """Plan D-03: accepted volumes decompress to the input byte for byte; refused ones leave no output."""
    content, error = NIFTI_CASES[case]
    source = tmp_path / ('in.nii.gz' if compressed else 'in.nii')
    source.write_bytes(gzip.compress(content) if compressed else content)
    dest = tmp_path / 'out.nii.gz'
    if error is None:
        nifti.validate_and_gzip(source, dest)
        assert gzip.decompress(dest.read_bytes()) == content
    else:
        with pytest.raises(ValueError, match=error):
            nifti.validate_and_gzip(source, dest)
        assert not dest.exists()


def test_sidecar_filter():
    """Plan D-10: listed technical fields pass; others, NaN, infinities, nested values and null are dropped (pin)."""
    listed = {name: 'x' for name in nifti.TECH_FIELDS}
    extra = {'PatientName': 'FAKE', 'SeriesDescription': 'free text', 'AcquisitionTime': '10:11:12'}
    assert nifti.clean_metadata(dict(listed, **extra)) == listed
    odd = json.loads('{"EchoTime": NaN, "FlipAngle": Infinity, "PixelSpacing": [1, NaN], "ImageType": ["A", ["B"]], '
                     '"Modality": {"a": 1}, "SeriesNumber": null, "RepetitionTime": true, "SliceThickness": 2.5, '
                     '"ScanningSequence": [], "Manufacturer": ""}')
    assert nifti.clean_metadata(odd) == {'ScanningSequence': [], 'RepetitionTime': True, 'Manufacturer': '',
                                         'SliceThickness': 2.5}


# ---- Round 2: published folders changed by people or restores (plan F-05, F-09) -------------

@pytest.mark.parametrize('name', ['Thumbs.db', '.DS_Store', 'desktop.ini'])
def test_stray_desktop_file_in_published_folder(project, tmp_path, converter, name):
    """Plan F-05: a file the desktop adds counts as a change; revalidation works once it is gone (pin)."""
    folder = converted(project, tmp_path, converter)
    before = receipt(folder)
    options = ('--dcm2niix-bin', converter)
    stray = folder / name
    stray.write_bytes(b'x')
    assert call(project, 'convert', *options) == 2
    assert call(project, 'convert', *options, '--audit', '--session', KEY) == 2
    stray.unlink()
    assert call(project, 'convert', *options) == 2  # the folder time changed, so it stays held
    assert call(project, 'convert', *options, '--audit', '--session', KEY) == 0
    assert receipt(folder) == before and not state(project)['issues']


def test_stray_desktop_file_in_an_extraction_is_extracted_again(project, tmp_path, converter):
    """Plan F-05: in an extraction, prepare extracts again and the stray file goes (pin); conversion stays cached."""
    folder = converted(project, tmp_path, converter)
    before = receipt(folder)
    extraction = project / 'DicomExtracted/Subjects/S001/20260725'
    (extraction / '.DS_Store').write_bytes(b'x')
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert call(project, 'prepare') == 2  # the convert: issue is still open
    assert not (extraction / '.DS_Store').exists()
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    assert receipt(folder) == before and not state(project)['issues']


def touch_folders(folder):
    later = time.time_ns() + 3_000_000_000
    for p in [folder, *(d for d in folder.rglob('*') if d.is_dir())]:
        os.utime(p, ns=(later, later))


def test_restored_project_with_new_folder_times(project, tmp_path, converter):
    """Plan F-09: after a restore that resets folder times nothing is reconverted; the NIfTI receipt never changes."""
    niftis = converted(project, tmp_path, converter)
    extraction = project / 'DicomExtracted/Subjects/S001/20260725'
    canonical = project / 'DicomUploads/SubjectsSorted/S001/S001_20260725.zip'
    before = receipt(niftis)
    options = ('--dcm2niix-bin', converter)
    touch_folders(extraction)
    assert call(project, 'convert', *options) == 2
    assert call(project, 'prepare') == 2  # extracted again; the convert: issue is still open
    assert call(project, 'convert', *options) == 0
    touch_folders(niftis)
    assert call(project, 'convert', *options) == 2
    assert call(project, 'convert', *options, '--audit') == 2  # --audit alone does not revalidate
    assert call(project, 'convert', *options, '--audit', '--session', KEY) == 0
    os.utime(canonical, ns=(time.time_ns(), time.time_ns() + 7_000_000_000))
    assert call(project, 'prepare') == 0  # the ZIP is hashed once and its new time recorded
    touch_folders(extraction)
    touch_folders(niftis)
    os.utime(canonical, ns=(time.time_ns(), time.time_ns() + 9_000_000_000))
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', *options) == 2
    assert call(project, 'convert', *options, '--audit', '--session', KEY) == 0
    assert call(project, 'convert', *options) == 0
    assert receipt(niftis) == before and not state(project)['issues']


def test_unreferenced_output_folder_that_does_not_match_is_held(project, tmp_path, converter):
    """With state lost, a NIfTI folder that does not match the prepared session is never replaced."""
    folder = converted(project, tmp_path, converter)
    shutil.rmtree(project / 'Reports')
    data = receipt(folder)
    (folder / '.pipeline_complete.json').write_text(json.dumps(dict(data, source_sha256='0' * 64)))
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert state(project)['issues'][f'output:{KEY}']['reason'] == (
        'Unreferenced output folder that does not match the prepared session; it was not replaced')


def test_audit_session_refuses_a_changed_volume(project, tmp_path, converter):
    """An edit that keeps size and time is found only by --audit, and --audit --session does not accept it."""
    folder = converted(project, tmp_path, converter)
    volume = next(folder.rglob('*.nii.gz'))
    info, data = volume.stat(), bytearray(volume.read_bytes())
    data[len(data) // 2] ^= 0xFF
    volume.write_bytes(bytes(data))
    os.utime(volume, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--audit') == 2
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--audit', '--session', KEY) == 2
    assert read_status(project)[KEY]['conversion_status'] == 'output_modified'


def test_convert_with_another_dicom_root_is_held(project, tmp_path, converter):
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--dicom-root', tmp_path / 'other_dicoms') == 2
    assert state(project)['issues'][f'convert:{KEY}']['reason'].startswith(
        'Prepared cache belongs to a different DICOM root')


def test_not_enough_staging_space_fails_the_session(project, tmp_path, converter, monkeypatch):
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    usage = namedtuple('usage', 'total used free')
    monkeypatch.setattr(convert.shutil, 'disk_usage', lambda path: usage(10**12, 10**12 - 10, 10))
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert 'Insufficient free staging space for this session' in state(project)['issues'][f'convert:{KEY}']['reason']


# ---- Round 3: --accept-partial limits and lifecycle (SB-27, D-14) ---------------------------

SECONDARY_CAPTURE = '1.2.840.10008.5.1.4.1.1.7'


def test_accept_partial_needs_a_converted_required_series(project, tmp_path, converter, monkeypatch):
    """SB-27: a session where only an excused series (a screen capture) converted is never accepted."""
    study, mr = generate_uid(), generate_uid()
    archive_objects(project, 'S001_20260725.zip', [
        dicom_bytes(tmp_path, study=study, series=mr),
        dicom_bytes(tmp_path, study=study, sop_class=SECONDARY_CAPTURE, description='Screen Save', series_number=90,
                    fields={'Modality': 'OT'})])
    assert call(project, 'prepare') == 0
    monkeypatch.setenv('FAKE_CONVERTER_SKIP', mr)
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--accept-partial', KEY) == 2
    assert conversion(project)['status'] == 'partial' and not (project / NIFTI).exists()
    row = read_status(project)[KEY]
    assert row['missing_series'] == row['required_series'] != ''


STRAY_CONVERTER = '''
import pathlib, subprocess, sys
fake = pathlib.Path(__file__).with_name('fake_converter.py')
code = subprocess.run([sys.executable, str(fake)] + sys.argv[1:]).returncode
out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
(out / 'stray_output.txt').write_text('x')
sys.exit(code)
'''


def test_accepted_unplaced_file_is_not_said_to_be_retained(project, tmp_path, converter):
    """D-14: an accepted session's working files are deleted, so its recorded error must not say they are kept."""
    stray = tmp_path / 'stray_converter.py'
    stray.write_text(STRAY_CONVERTER)
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', stray) == 2
    assert call(project, 'convert', '--dcm2niix-bin', stray, '--accept-partial', KEY) == 0
    assert conversion(project)['accepted_errors'] == ['1 unresolved converter artifacts, not published']


def accepted(project, tmp_path, converter, monkeypatch):
    """A session accepted with --accept-partial after the converter exited 1 (it still does)."""
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    monkeypatch.setenv('FAKE_CONVERTER_EXIT', '1')
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    return ('--dcm2niix-bin', converter)


def test_accept_partial_dry_run_changes_nothing(project, tmp_path, converter, monkeypatch, caplog):
    options = accepted(project, tmp_path, converter, monkeypatch)
    index = project / 'Reports/.state/index.json'
    before = index.read_bytes()
    assert call(project, 'convert', *options, '--dry-run', '--accept-partial', KEY) == 0
    assert 'WOULD convert S001/20260725' in caplog.text
    assert index.read_bytes() == before and not (project / NIFTI).exists()


def test_accepted_session_lifecycle(project, tmp_path, converter, monkeypatch):
    """An accepted session is cached by --audit; a failed reconversion keeps it; it survives state loss."""
    options = accepted(project, tmp_path, converter, monkeypatch)
    assert call(project, 'convert', *options, '--accept-partial', KEY) == 0
    folder_receipt = receipt(project / NIFTI)
    assert call(project, 'convert', *options, '--audit') == 0
    assert call(project, 'convert', *options, '--audit', '--session', KEY) == 0
    # A reconversion without the option fails again; the accepted folder stays.
    assert call(project, 'convert', *options, '--force-reconvert', '--session', KEY) == 2
    row = read_status(project)[KEY]
    assert (row['conversion_status'], row['last_attempt_status']) == ('accepted_partial', 'partial')
    assert receipt(project / NIFTI) == folder_receipt
    assert call(project, 'convert', *options) == 0 and not state(project)['issues']  # pin: the issue clears
    # State lost: both commands adopt the folders, and the acceptance is kept.
    shutil.rmtree(project / 'Reports')
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', *options) == 0
    assert conversion(project)['accepted_errors'] == ['dcm2niix exited with code 1']
    assert read_status(project)[KEY]['conversion_status'] == 'accepted_partial'
