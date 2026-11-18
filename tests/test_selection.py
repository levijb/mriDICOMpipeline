"""The select command end to end with the fake converter (design: scan selection, 10.2-10.4)."""
import csv
import gzip
import hashlib
import json
from pathlib import Path

from pydicom.uid import generate_uid
import pytest

from mri_dicom_pipeline import locks, runner, selection_metadata
from support import archive_objects, call, mr_series, run_with_crash

KEY = 'S001/20260725'
SELECTED = 'Selected/Subjects/S001/20260725'
COMMON = {'Manufacturer': 'GE MEDICAL SYSTEMS', 'ManufacturerModelName': 'SIGNA Premier', 'MagneticFieldStrength': 3.0,
          'AcquisitionMatrix': [0, 10, 10, 0], 'InPlanePhaseEncodingDirection': 'ROW',
          'ImageType': ['ORIGINAL', 'PRIMARY', 'OTHER']}
T1 = {'MRAcquisitionType': '3D', 'ScanningSequence': ['GR'], 'SequenceVariant': ['SS'], 'ScanOptions': ['IR_GEMS'],
      'RepetitionTime': 6.0, 'EchoTime': 2.4, 'InversionTime': 450.0, 'FlipAngle': 13.0}
FLAIR = {'MRAcquisitionType': '3D', 'ScanningSequence': ['SE', 'IR'], 'RepetitionTime': 6000.0, 'EchoTime': 120.0,
         'InversionTime': 1700.0, 'FlipAngle': 90.0}
T2 = {'MRAcquisitionType': '2D', 'ScanningSequence': ['SE'], 'RepetitionTime': 7000.0, 'EchoTime': 100.0,
      'FlipAngle': 90.0, 'SliceThickness': 4.0}
DIRECTIONS = [(1, 0, 0), (0, 1, 0), (0, 0, 1), (0.707, 0.707, 0), (0.707, 0, 0.707), (0, 0.707, 0.707)]
DWI = {'MRAcquisitionType': '2D', 'ScanningSequence': ['EP', 'SE'], 'RepetitionTime': 8000.0, 'EchoTime': 85.0,
       'FlipAngle': 90.0, 'ImageComments': json.dumps({
           'bval': [0] + [1000] * 6, 'bvec': [[0] + [d[i] for d in DIRECTIONS] for i in range(3)],
           'sidecar': {'PhaseEncodingDirection': 'j-'}})}
# Synthetic series are 10 x 10 x 10 mm, so the coverage screen is switched off.
TEST_PROFILE = {'roles': {role: {'min_extent_mm': None} for role in ('T1', 'T1_POST', 'T2', 'FLAIR')}}


@pytest.fixture(autouse=True)
def realistic_converter(monkeypatch):
    monkeypatch.setenv('FAKE_CONVERTER_DIMS', '1')


def series(tmp_path, study, frame, number, description, time, fields, slices=10, positions=None, protocol=None):
    f = dict(COMMON, FrameOfReferenceUID=frame, AcquisitionTime=time, SeriesTime=time, **fields)
    if protocol:
        f['ProtocolName'] = protocol
    return mr_series(tmp_path, slices=slices, study=study, positions=positions, fields=f, description=description,
                     series_number=number, rows=10, columns=10)[1]


def exam(tmp_path, extra=(), protocol=None, t2=True):
    """A GE-like exam: localizer, 3D T1 with a reformat, 3D FLAIR, 2D T2, DWI with 6 directions."""
    study, frame = generate_uid(), generate_uid()
    objects = series(tmp_path, study, frame, 2, '3-pl Localizer', '100000',
                     {'ImageType': ['ORIGINAL', 'PRIMARY', 'LOCALIZER']}, slices=3)
    objects += series(tmp_path, study, frame, 3, 'SAG 3D T1 BRAVO', '100500', T1, protocol=protocol)
    objects += series(tmp_path, study, frame, 300, 'AX T1 PRE REFORMAT', '100500',
                      dict(T1, ImageType=['DERIVED', 'SECONDARY', 'REFORMATTED', 'AVERAGE']))
    objects += series(tmp_path, study, frame, 5, 'SAG 3D FLAIR', '101000', FLAIR)
    if t2:
        objects += series(tmp_path, study, frame, 7, 'AX T2 FSE', '101500', T2, positions=[i * 4.0 for i in range(10)])
    objects += series(tmp_path, study, frame, 9, 'AX DWI', '102000', DWI)
    for number, description, time, fields in extra:
        objects += series(tmp_path, study, frame, number, description, time, fields)
    return objects


def convert_sessions(project, tmp_path, converter, sessions):
    for name, objects in sessions.items():
        archive_objects(project, name, objects)
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0


def write_profile(tmp_path, data=TEST_PROFILE):
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(data))
    return path


def select(project, tmp_path, *extra):
    return runner.main('select', ['--project-root', str(project), '--profile', str(write_profile(tmp_path)),
                                  *map(str, extra)])


def rows(path):
    with Path(path).open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def roles(project, output='Selected'):
    return {(r['subject_id'], r['session'], r['role']): r for r in rows(project / output / 'selection.csv')}


def tree(root):
    return sorted(p.relative_to(root).as_posix() for p in Path(root).rglob('*') if p.is_file())


def digest(root):
    return {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in tree(root)}


def test_select_reports_and_publishes_one_acquisition_per_folder(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path)})
    before = {name: digest(project / name) for name in ('DicomExtracted', 'Niftis')}
    state_before = digest(project / 'Reports/.state')
    assert select(project, tmp_path, '--publish') == 0
    decided = roles(project)
    assert decided[('S001', '20260725', 'T1')]['outcome'] == 'selected'
    assert decided[('S001', '20260725', 'T1')]['decided_by'] == 'only_eligible'
    assert decided[('S001', '20260725', 'FLAIR')]['outcome'] == 'selected'
    assert decided[('S001', '20260725', 'T2')]['outcome'] == 'selected_fallback'
    assert decided[('S001', '20260725', 'T2')]['tier'] == '2d_clinical'
    assert decided[('S001', '20260725', 'DWI')]['outcome'] == 'selected'
    assert decided[('S001', '20260725', 'DWI')]['n_dirs'] == '6'
    assert decided[('S001', '20260725', 'LOCALIZER')]['outcome'] == 'kept_all'
    assert decided[('S001', '20260725', 'SWI')]['outcome'] == 'missing'
    assert decided[('S001', '20260725', 'T1')]['contrast_evidence'] == 'reformat_description'  # from 'PRE'
    assert 'contrast_presumed_pre' in decided[('S001', '20260725', 'FLAIR')]['warnings']
    volumes = rows(project / 'Selected/selection_volumes.csv')
    assert {v['fate'] for v in volumes if v['role'] == 'T1'} == {'selected', 'reformat'}
    out = project / SELECTED
    files = tree(out)
    folders = {f.rsplit('/', 1)[0] for f in files if '/' in f}
    assert folders == {'T1', 'FLAIR', 'T2', 'DWI', 'LOCALIZER'}  # no extras unless asked for
    assert sum(f.startswith('T1/') and f.endswith('.nii.gz') for f in files) == 1
    assert {Path(f).suffix for f in files if f.startswith('DWI/')} == {'.gz', '.json', '.bval', '.bvec'}
    assert {'selection.json', '.pipeline_complete.json', '.inventory.json'} <= set(files)
    assert {v['selected_path'] == '' for v in volumes if v['fate'] == 'reformat'} == {True}
    assert select(project, tmp_path, '--publish', '--extras') == 0
    volumes = rows(project / 'Selected/selection_volumes.csv')
    assert {f.rsplit('/', 1)[0] for f in tree(out) if '/' in f} == {'T1', 'OTHER/T1', 'FLAIR', 'T2', 'DWI', 'LOCALIZER'}
    # Every NIfTI is a byte copy of its source; each sidecar adds the selection fields.
    for v in volumes:
        copy = out / v['selected_path']
        assert copy.read_bytes() == (project / 'Niftis' / v['source']).read_bytes()
        sidecar = json.loads(copy.with_name(copy.name[:-7] + '.json').read_text())
        assert sidecar['selection_fate'] == v['fate'] and sidecar['source_nifti'] == v['source']
    # Nothing upstream changed.
    assert {name: digest(project / name) for name in ('DicomExtracted', 'Niftis')} == before
    assert digest(project / 'Reports/.state') == state_before


def test_rerun_uses_the_cache_and_changes_nothing(project, tmp_path, converter, monkeypatch, capsys):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path)})
    assert select(project, tmp_path, '--publish') == 0
    first = {name: (project / 'Selected' / name).read_bytes() for name in ('selection.csv', 'selection_volumes.csv')}
    published = digest(project / SELECTED)
    import pydicom
    monkeypatch.setattr(pydicom, 'dcmread', lambda *a, **k: pytest.fail('cached rerun read a DICOM header'))
    monkeypatch.setattr(gzip, 'open', lambda *a, **k: pytest.fail('cached rerun opened a NIfTI'))
    capsys.readouterr()
    assert select(project, tmp_path, '--publish') == 0
    log = capsys.readouterr().out
    assert 'cached=1' in log and 'unchanged=1' in log
    assert {name: (project / 'Selected' / name).read_bytes() for name in first} == first
    assert digest(project / SELECTED) == published


def test_dry_run_writes_nothing(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path)})
    reports_before = tree(project / 'Reports')
    assert select(project, tmp_path, '--publish', '--dry-run') == 0
    assert not (project / 'Selected').exists()
    assert tree(project / 'Reports') == reports_before


def test_subject_list_and_output_root_outside_the_project(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path),
                                                    'S002_20260725.zip': exam(tmp_path)})
    listing = tmp_path / 'ids.csv'
    listing.write_text('subject_id\nS002\n\nS999\n')
    output = tmp_path / 'ProjectX'
    assert select(project, tmp_path, '--subjects', listing, '--output-root', output, '--publish') == 0
    assert {r['subject_id'] for r in rows(output / 'selection.csv')} == {'S002'}
    assert (output / 'Subjects/S002/20260725/T1').is_dir() and not (output / 'Subjects/S001').exists()
    assert not (project / 'Selected').exists()
    listing.write_text('subject_id\nS 1\n')
    assert select(project, tmp_path, '--subjects', listing, '--output-root', output) == 2
    assert select(project, tmp_path, '--output-root', project / 'Niftis' / 'inside') == 2


def test_3d_only_leaves_the_2d_folder_empty_and_says_why(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path)})
    assert select(project, tmp_path, '--publish', '--3d-only') == 0
    t2 = roles(project)[('S001', '20260725', 'T2')]
    assert t2['outcome'] == 'no_eligible_candidate' and t2['reasons'] == 'not_3d(1)'
    assert roles(project)[('S001', '20260725', 'DWI')]['outcome'] == 'selected'
    out = project / SELECTED
    assert not (out / 'T2').exists() and not (out / 'OTHER').exists()
    assert select(project, tmp_path, '--publish', '--3d-only', '--extras') == 0
    assert not (out / 'T2').exists() and list((out / 'OTHER/T2').glob('*.nii.gz'))


def test_review_then_override_republishes(project, tmp_path, converter):
    repeat = [(11, 'SAG 3D T1 BRAVO', '103000', T1)]
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path, extra=repeat)})
    assert select(project, tmp_path, '--publish', '--extras') == 2
    t1 = roles(project)[('S001', '20260725', 'T1')]
    assert t1['outcome'] == 'review_required' and t1['reasons'] == 'repeat_needs_review'
    out = project / SELECTED
    assert not (out / 'T1').exists() and len(list((out / 'OTHER/T1').glob('*.nii.gz'))) == 3
    receipt = json.loads((out / '.pipeline_complete.json').read_text())
    (project / 'selection_overrides.csv').write_text(
        'subject_id,session,role,action,candidate_id,note\n'
        f'S001,20260725,T1,choose,{t1["rule_recommendation"]},reviewer picked the later scan\n')
    assert select(project, tmp_path, '--publish') == 0
    t1 = roles(project)[('S001', '20260725', 'T1')]
    assert (t1['outcome'], t1['decided_by']) == ('selected', 'override')
    assert len(list((out / 'T1').glob('*.nii.gz'))) == 1
    assert json.loads((out / '.pipeline_complete.json').read_text())['receipt_id'] != receipt['receipt_id']
    assert 'reviewer picked' not in (project / 'Selected/selection.csv').read_text()


def test_invalid_override_row_aborts(project, tmp_path, converter, capsys):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path)})
    (project / 'selection_overrides.csv').write_text('subject_id,session,role,action,candidate_id\n'
                                                     'S001,20260725,T1,pick,abcdefabcdef\n')
    assert select(project, tmp_path) == 2
    assert 'row 2' in capsys.readouterr().err
    assert not (project / 'Selected/selection.csv').exists()


def test_added_files_block_an_update(project, tmp_path, converter):
    repeat = [(11, 'SAG 3D T1 BRAVO', '103000', T1)]
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path, extra=repeat)})
    assert select(project, tmp_path, '--publish') == 2
    out = project / SELECTED
    (out / 'analysis_notes.txt').write_text('mine')
    t1 = roles(project)[('S001', '20260725', 'T1')]
    (project / 'selection_overrides.csv').write_text(
        f'subject_id,session,role,action,candidate_id\nS001,20260725,T1,choose,{t1["rule_recommendation"]}\n')
    published = digest(out)
    assert select(project, tmp_path, '--publish') == 2
    assert digest(out) == published
    (out / 'analysis_notes.txt').unlink()
    assert select(project, tmp_path, '--publish') == 0


def test_folder_without_a_receipt_is_never_replaced(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path)})
    out = project / SELECTED
    out.mkdir(parents=True)
    (out / 'someone_elses.nii.gz').write_bytes(b'x')
    assert select(project, tmp_path, '--publish') == 2
    assert tree(out) == ['someone_elses.nii.gz']


def test_sessions_not_ready(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path)})
    archive_objects(project, 'S002_20260725.zip', exam(tmp_path))
    assert call(project, 'prepare') == 0
    assert select(project, tmp_path) == 0  # an unconverted session alone does not need attention
    later = roles(project)[('S002', '20260725', 'T1')]
    assert (later['outcome'], later['reasons']) == ('source_not_ready', 'not_converted')
    extra = project / 'Niftis/Subjects/S001/20260725/T1/added.txt'
    extra.write_text('x')
    assert select(project, tmp_path) == 2
    assert roles(project)[('S001', '20260725', 'T1')]['reasons'] == 'output_modified'


def test_select_waits_for_a_running_convert(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path)})
    with locks.RootLocks([project / 'Niftis'], 'convert', 'TEST-RUN'):
        assert select(project, tmp_path) == 2
    assert not (project / 'Selected/selection.csv').exists()


def test_descriptions_never_reach_outputs(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter,
                     {'S001_20260725.zip': exam(tmp_path, protocol='FAKE_PRIVATE_NAME protocol')})
    assert select(project, tmp_path, '--publish') == 0
    for root in (project / 'Selected', project / 'Reports/.selection_cache'):
        for path in Path(root).rglob('*'):
            if path.is_file() and not path.name.endswith('.nii.gz'):
                assert b'FAKE_PRIVATE_NAME' not in path.read_bytes(), path
    assert 'FAKE_PRIVATE_NAME' not in (project / 'Reports/latest_select.log').read_text()


@pytest.mark.parametrize('point', ['staged', 'moved-aside', 'swapped'])
def test_interrupted_publication_is_finished_by_the_next_run(project, tmp_path, converter, point):
    repeat = [(11, 'SAG 3D T1 BRAVO', '103000', T1)]
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path, extra=repeat)})
    profile = write_profile(tmp_path)
    assert select(project, tmp_path, '--publish') == 2
    t1 = roles(project)[('S001', '20260725', 'T1')]
    (project / 'selection_overrides.csv').write_text(
        f'subject_id,session,role,action,candidate_id\nS001,20260725,T1,choose,{t1["rule_recommendation"]}\n')
    assert run_with_crash(project, 'select', point, '--profile', profile, '--publish') == 137
    assert select(project, tmp_path, '--publish') == 0
    parent = project / 'Selected/Subjects/S001'
    assert [p.name for p in parent.iterdir()] == ['20260725']
    assert len(list((parent / '20260725/T1').glob('*.nii.gz'))) == 1


def test_changed_source_is_not_published(project, tmp_path, converter, monkeypatch):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path)})
    assert select(project, tmp_path) == 0  # fills the cache
    original = selection_metadata.session_metadata

    def stale(*args, **kwargs):
        series, volumes, hit = original(*args, **kwargs)
        volumes[0] = dict(volumes[0], sha256='0' * 64)
        return series, volumes, hit
    monkeypatch.setattr(selection_metadata, 'session_metadata', stale)
    assert select(project, tmp_path, '--publish') == 2
    assert not (project / SELECTED).exists()


def test_header_records(tmp_path):
    study, frame = generate_uid(), generate_uid()
    root = tmp_path / 'extraction'
    (root / 'dicom').mkdir(parents=True)
    objects = series(tmp_path, study, frame, 3, 'SAG 3D T1 POST GAD FAKE_PRIVATE_NAME', '100500',
                     dict(T1, ContrastBolusAgent='GADAVIST'))
    objects += series(tmp_path, study, frame, 4, 'AX T1 reformat', '100500',
                      dict(T1, ImageType=['DERIVED', 'SECONDARY', 'REFORMATTED']))
    echo1 = mr_series(tmp_path, slices=4, study=study, description='QSM', series_number=6, rows=10, columns=10,
                      fields=dict(COMMON, FrameOfReferenceUID=frame, MRAcquisitionType='3D', EchoTime=5.0,
                                  AcquisitionTime='101000', ImageType=['ORIGINAL', 'PRIMARY', 'M']))[1]
    files = []
    for i, data in enumerate(objects + echo1, 1):
        path = root / 'dicom' / f'{i:05d}'
        path.write_bytes(data)
        files.append({'name': f'dicom/{i:05d}'})
    records = selection_metadata.read_series(root, {'files': files})
    by_number = {r['series_number']: r for r in records.values()}
    t1 = by_number[3]
    assert (t1['tr_ms'], t1['te_ms'], t1['ti_ms'], t1['mr_acq']) == (6.0, 2.4, 450.0, '3D')
    assert t1['orientation'] == 'AX' and t1['n_positions'] == 10 and t1['acq_rel_s'] == 0.0
    assert t1['contrast_agent'] and t1['desc_phase'] == 'post'
    assert 'FAKE_PRIVATE_NAME' not in json.dumps(records)
    assert set(t1['desc_tokens']) <= {'SAG', '3D', 'T1', 'POST', 'GAD'}
    assert by_number[4]['image_type'][:3] == ['DERIVED', 'SECONDARY', 'REFORMATTED']
    assert by_number[6]['parts'] == {'mag': 4}
    assert len(t1['for_hash']) == 20 and frame not in json.dumps(records)


def test_header_records_echoes_and_mixed_series(tmp_path):
    study, frame, series_uid = generate_uid(), generate_uid(), generate_uid()
    from support import dicom_bytes
    objects = []
    for echo, te in enumerate((5.0, 10.0), 1):
        for i in range(4):
            objects.append(dicom_bytes(tmp_path, study=study, series=series_uid, slice_index=i, series_number=8,
                                       fields=dict(COMMON, EchoTime=te, EchoNumbers=echo,
                                                   ImagePositionPatient=[0.0, 0.0, float(i)])))
    mixed_uid = generate_uid()
    for i in range(4):
        objects.append(dicom_bytes(tmp_path, study=study, series=mixed_uid, slice_index=i, series_number=1,
                                   fields={'ImageOrientationPatient': [1, 0, 0, 0, 0, -1] if i % 2 else
                                           [1, 0, 0, 0, 1, 0]}))
    root = tmp_path / 'extraction'
    (root / 'dicom').mkdir(parents=True)
    files = []
    for i, data in enumerate(objects, 1):
        (root / 'dicom' / f'{i:05d}').write_bytes(data)
        files.append({'name': f'dicom/{i:05d}'})
    records = {r['series_number']: r for r in selection_metadata.read_series(root, {'files': files}).values()}
    echoes = records[8]
    assert echoes['te_ms'] is None and [e['te_ms'] for e in echoes['echoes']] == [5.0, 10.0]
    assert echoes['n_positions'] == 4 and not echoes.get('mixed')
    mixed = records[1]
    assert mixed['mixed'] and mixed['n_stacks'] == 2 and set(mixed) == {
        'series_key', 'series_number', 'manufacturer', 'model', 'field_t', 'n_instances', 'n_stacks', 'mixed'}


def test_modalities_limit_reports_and_copies(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path)})
    assert select(project, tmp_path, '--publish', '--modalities', 't1,FLAIR') == 0
    assert {r['role'] for r in rows(project / 'Selected/selection.csv')} == {'T1', 'FLAIR'}
    assert {r['role'] for r in rows(project / 'Selected/selection_volumes.csv')} == {'T1', 'FLAIR'}
    out = project / SELECTED
    assert {f.split('/', 1)[0] for f in tree(out) if '/' in f} == {'T1', 'FLAIR'}
    assert select(project, tmp_path, '--modalities', 'T1,NOPE') == 2


def test_complete_only_skips_sessions_missing_a_listed_modality(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path),
                                                    'S002_20260725.zip': exam(tmp_path, t2=False)})
    assert select(project, tmp_path, '--publish', '--modalities', 'T1,T2', '--complete-only') == 0
    assert (project / 'Selected/Subjects/S001/20260725/T2').is_dir()
    assert not (project / 'Selected/Subjects/S002').exists()
    s2 = roles(project)[('S002', '20260725', 'T1')]
    assert s2['outcome'] == 'selected' and 'session_incomplete' in s2['warnings']
    assert select(project, tmp_path, '--complete-only') == 2  # needs --modalities


def test_resolution_requirements(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path)})
    assert select(project, tmp_path, '--isotropic', '--max-voxel-mm', '1.2') == 0
    decided = roles(project)
    assert decided[('S001', '20260725', 'T1')]['outcome'] == 'selected'  # 1.0 x 1.0 x 1.0 mm
    assert decided[('S001', '20260725', 'T2')]['reasons'] == 'not_isotropic(1)'  # 1.0 x 1.0 x 4.0 mm
    assert select(project, tmp_path, '--max-voxel-mm', '0.5') == 0
    assert roles(project)[('S001', '20260725', 'FLAIR')]['reasons'] == 'voxel_too_large(1)'
    assert select(project, tmp_path, '--min-directions', '30') == 0
    assert roles(project)[('S001', '20260725', 'DWI')]['reasons'] == 'too_few_directions(1)'
    assert select(project, tmp_path, '--no-fallback') == 0
    assert roles(project)[('S001', '20260725', 'T2')]['reasons'] == 'fallback_only(1)'


def test_unknown_resolution_goes_to_review(project, tmp_path, converter):
    no_matrix = dict(T1, AcquisitionMatrix=None)
    study, frame = generate_uid(), generate_uid()
    objects = series(tmp_path, study, frame, 3, 'SAG 3D T1 BRAVO', '100500', no_matrix)
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': objects})
    assert select(project, tmp_path, '--isotropic') == 2
    t1 = roles(project)[('S001', '20260725', 'T1')]
    assert (t1['outcome'], t1['reasons']) == ('review_required', 'native_unknown(1)')


def test_scanner_requirements(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': exam(tmp_path)})
    assert select(project, tmp_path, '--publish', '--field-strength', '1.5') == 0
    t1 = roles(project)[('S001', '20260725', 'T1')]
    assert (t1['outcome'], t1['reasons']) == ('session_excluded', 'field_strength')
    assert not (project / SELECTED).exists()
    assert select(project, tmp_path, '--field-strength', '1.5,3', '--manufacturer', 'ge') == 0
    assert roles(project)[('S001', '20260725', 'T1')]['outcome'] == 'selected'
    assert select(project, tmp_path, '--manufacturer', 'SIEMENS') == 0
    assert roles(project)[('S001', '20260725', 'T1')]['reasons'] == 'manufacturer'
    assert select(project, tmp_path, '--field-strength', 'three') == 2
