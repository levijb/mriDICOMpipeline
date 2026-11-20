"""select --bids end to end with the fake converter (design: BIDS output, 5.4)."""
import json
import os
from pathlib import Path
import shutil
import subprocess

from pydicom.uid import generate_uid
import pytest

from support import run_with_crash
from test_selection import (DIRECTIONS, DWI, FLAIR, T1, convert_sessions, digest, first_study, rows, second_study,
                            select, series, tree, write_profile)

P = 'sub-S001_ses-20260725'
SESSION = 'BIDS/sub-S001/ses-20260725'
DWI_READOUT = dict(DWI, ImageComments=json.dumps({
    'bval': [0] + [1000] * 6, 'bvec': [[0] + [d[i] for d in DIRECTIONS] for i in range(3)],
    'sidecar': {'PhaseEncodingDirection': 'j-', 'TotalReadoutTime': 0.05}}))
REVERSE_B0 = dict(DWI, ImageComments=json.dumps({
    'bval': [0], 'bvec': [[0], [0], [0]], 'sidecar': {'PhaseEncodingDirection': 'j', 'TotalReadoutTime': 0.05}}))


@pytest.fixture(autouse=True)
def realistic_converter(monkeypatch):
    monkeypatch.setenv('FAKE_CONVERTER_DIMS', '1')


def bids_exam(tmp_path, repeat=False, post=False):
    """Localizer, 3D T1 (optionally repeated, optionally a post-contrast T1), 3D FLAIR, DWI and a reverse b0."""
    study, frame = generate_uid(), generate_uid()
    objects = series(tmp_path, study, frame, 2, '3-pl Localizer', '100000',
                     {'ImageType': ['ORIGINAL', 'PRIMARY', 'LOCALIZER']}, slices=3)
    objects += series(tmp_path, study, frame, 3, 'SAG 3D T1 BRAVO', '100500', T1)
    objects += series(tmp_path, study, frame, 5, 'SAG 3D FLAIR', '101000', FLAIR)
    objects += series(tmp_path, study, frame, 9, 'AX DWI', '102000', DWI_READOUT)
    objects += series(tmp_path, study, frame, 10, 'AX DWI', '102500', REVERSE_B0)
    if repeat:
        objects += series(tmp_path, study, frame, 11, 'SAG 3D T1 BRAVO', '103000', T1)
    if post:
        objects += series(tmp_path, study, frame, 13, 'SAG 3D T1 BRAVO', '104000', dict(T1, ContrastBolusAgent='GADAVIST'))
    return objects


SWI = {'MRAcquisitionType': '3D', 'ScanningSequence': ['GR'], 'RepetitionTime': 40.0, 'EchoTime': 20.0,
       'FlipAngle': 15.0}
TOF = {'MRAcquisitionType': '3D', 'ScanningSequence': ['GR'], 'RepetitionTime': 22.0, 'EchoTime': 3.4,
       'FlipAngle': 18.0}


def susceptibility_exam(tmp_path, date='20260725'):
    """3D T1, SWI magnitude and phase (one acquisition), the scanner's processed SWI, minIP and QSM map, TOF + MIP."""
    study, frame = generate_uid(), generate_uid()
    objects = []
    for number, description, time, fields, image_type in (
            (3, 'SAG 3D T1 BRAVO', '100500', T1, None),
            (20, 'AX SWI', '101000', SWI, ['ORIGINAL', 'PRIMARY', 'M']),
            (21, 'AX SWI', '101000', SWI, ['ORIGINAL', 'PRIMARY', 'P']),
            (22, 'AX SWI', '101100', SWI, ['DERIVED', 'PRIMARY', 'SWI']),
            (23, 'AX SWI MinIP', '101100', SWI, ['DERIVED', 'PRIMARY', 'MIN IP']),
            (24, 'QSM', '101100', SWI, ['DERIVED', 'PRIMARY', 'QSM']),
            (30, 'TOF MRA', '102000', TOF, ['ORIGINAL', 'PRIMARY', 'M']),
            (31, 'TOF MRA MIP', '102000', TOF, ['DERIVED', 'PRIMARY', 'MIP'])):
        fields = dict(fields, StudyDate=date, **({'ImageType': image_type} if image_type else {}))
        objects += series(tmp_path, study, frame, number, description, time, fields)
    return objects


def sidecar(path):
    return json.loads(Path(path).read_text())


def test_publish_builds_a_bids_dataset(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': bids_exam(tmp_path, post=True)})
    assert select(project, tmp_path, '--bids', '--publish', '--bids-contrast-agent', 'Gadolinium') == 0
    root = project / 'BIDS'
    assert not (project / 'Selected').exists()
    session = tree(root / 'sub-S001/ses-20260725')
    data = [f for f in session if not f.startswith('.')]
    assert data == sorted([f'anat/{P}_{s}.{x}' for s in ('FLAIR', 'T1w', 'ce-gadolinium_T1w') for x in ('json', 'nii.gz')]
                          + [f'dwi/{P}_dwi.{x}' for x in ('bval', 'bvec', 'json', 'nii.gz')]
                          + [f'fmap/{P}_acq-dwi_dir-j_epi.{x}' for x in ('json', 'nii.gz')]
                          + [f'{P}_scans.json', f'{P}_scans.tsv'])
    assert {'.selection.json', '.pipeline_complete.json', '.inventory.json'} <= set(session)
    files = set(tree(root))
    assert {'participants.tsv', 'dataset_description.json', 'README.md', '.bidsignore', '.bids_export.json',
            'code/bids_filter_precontrast.json', 'selection.csv', 'selection_volumes.csv',
            'sub-S001/sub-S001_sessions.tsv', 'sub-S001/sub-S001_sessions.json'} <= files
    out = root / 'sub-S001/ses-20260725'
    fmap = sidecar(out / f'fmap/{P}_acq-dwi_dir-j_epi.json')
    assert fmap['IntendedFor'] == [f'bids::sub-S001/ses-20260725/dwi/{P}_dwi.nii.gz']
    assert (fmap['PhaseEncodingDirection'], fmap['TotalReadoutTime']) == ('j', 0.05)
    assert sidecar(out / f'anat/{P}_ce-gadolinium_T1w.json')['ContrastBolusIngredient'] == 'GADOLINIUM'
    t1 = sidecar(out / f'anat/{P}_T1w.json')
    assert not {'subject_id', 'session', 'session_date', 'dicom_study_date', 'selection_role', 'source_nifti'} & set(t1)
    volumes = {(v['role'], v['series_number']): v for v in rows(root / 'selection_volumes.csv')}
    assert (volumes[('LOCALIZER', '2')]['export_status'], volumes[('LOCALIZER', '2')]['export_reason']) == \
        ('omitted', 'role_not_exported')
    t1_row = volumes[('T1', '3')]
    assert (t1_row['selected_path'], t1_row['export_status']) == (f'anat/{P}_T1w.nii.gz', 'standard')
    assert (out / t1_row['selected_path']).read_bytes() == (project / 'Niftis' / t1_row['source']).read_bytes()
    scans = rows_tsv(out / f'{P}_scans.tsv')
    assert [s['filename'] for s in scans] == sorted(f for f in data if f.endswith('.nii.gz'))
    assert scans[0]['source_nifti'].startswith('Subjects/S001/20260725/')
    assert (root / 'participants.tsv').read_text() == 'participant_id\nsub-S001\n'
    assert (root / 'sub-S001/sub-S001_sessions.tsv').read_text() == 'session_id\tsource_sessions\nses-20260725\t20260725\n'
    description = sidecar(root / 'dataset_description.json')
    assert (description['Name'], description['DatasetType']) == (project.name, 'raw')
    assert json.loads((root / 'code/bids_filter_precontrast.json').read_text()) == {'t1w': {'ceagent': None}}
    assert sidecar(root / '.bids_export.json')['contrast_agent'] == 'gadolinium'


def rows_tsv(path):
    lines = Path(path).read_text().splitlines()
    return [dict(zip(lines[0].split('\t'), line.split('\t'))) for line in lines[1:]]


def test_rerun_agent_change_and_refusals(project, tmp_path, converter, capsys):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': bids_exam(tmp_path, post=True)})
    assert select(project, tmp_path, '--bids', '--publish') == 0
    out = project / SESSION
    assert (out / f'anat/{P}_ce-unknown_T1w.nii.gz').exists()
    assert sidecar(out / f'anat/{P}_ce-unknown_T1w.json')['ContrastBolusIngredient'] == 'UNKNOWN'
    published = digest(out)
    capsys.readouterr()
    assert select(project, tmp_path, '--bids', '--publish') == 0
    assert 'unchanged=1' in capsys.readouterr().out and digest(out) == published
    assert select(project, tmp_path, '--bids', '--publish', '--bids-contrast-agent', 'gadolinium') == 0
    assert (out / f'anat/{P}_ce-gadolinium_T1w.nii.gz').exists() and not (out / f'anat/{P}_ce-unknown_T1w.nii.gz').exists()
    capsys.readouterr()
    assert select(project, tmp_path, '--bids', '--publish') == 0  # the recorded agent is reused
    assert 'unchanged=1' in capsys.readouterr().out
    assert select(project, tmp_path, '--output-root', project / 'BIDS') == 2  # the default layout refuses it
    assert select(project, tmp_path, '--bids', '--extras') == 2
    assert select(project, tmp_path, '--bids', '--modalities', 'T1,LOCALIZER') == 2
    assert select(project, tmp_path, '--bids-contrast-agent', 'gadolinium') == 2  # needs --bids
    assert select(project, tmp_path, '--bids', '--bids-contrast-agent', 'gado-linium') == 2
    other = project / 'Other'
    other.mkdir()
    (other / 'notes.txt').write_text('mine')
    assert select(project, tmp_path, '--bids', '--output-root', other) == 2
    assert [f for f in tree(other) if not f.startswith('.')] == ['notes.txt']  # only the root locks were added


def test_report_only_and_dry_run(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': bids_exam(tmp_path)})
    assert select(project, tmp_path, '--bids', '--publish', '--dry-run') == 0
    assert not (project / 'BIDS').exists()
    assert select(project, tmp_path, '--bids') == 0
    root = project / 'BIDS'
    assert not list(root.glob('sub-*')) and (root / '.bids_export.json').exists()
    planned = {v['series_number']: v for v in rows(root / 'selection_volumes.csv')}
    assert (planned['10']['selected_path'], planned['10']['export_status'], planned['10']['published']) == \
        (f'fmap/{P}_acq-dwi_dir-j_epi.nii.gz', 'standard', 'false')
    assert select(project, tmp_path, '--bids', '--publish') == 0  # the root it made itself is accepted
    published = {v['series_number']: v['published'] for v in rows(root / 'selection_volumes.csv')}
    assert (published['10'], published['2']) == ('true', '')  # the localizer is omitted


def test_susceptibility_and_angiography_names(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S002_20260725.zip': susceptibility_exam(tmp_path)})
    assert select(project, tmp_path, '--bids', '--publish') == 0
    out = project / 'BIDS/sub-S002/ses-20260725'
    p = 'sub-S002_ses-20260725'
    assert [f for f in tree(out / 'anat') if f.endswith('.nii.gz')] == sorted(
        f'{p}_{s}.nii.gz' for s in ('T1w', 'acq-swi_part-mag_T2starw', 'acq-swi_part-phase_T2starw', 'acq-swi_swi',
                                    'acq-swi_minIP', 'acq-qsm_QSM', 'angio', 'rec-mip_angio'))
    assert sidecar(out / f'anat/{p}_acq-swi_part-phase_T2starw.json')['Units'] == 'arbitrary'
    scans = [s['filename'] for s in rows_tsv(out / f'{p}_scans.tsv')]
    assert not any(s.endswith(('_swi.nii.gz', '_minIP.nii.gz', '_QSM.nii.gz')) for s in scans)
    status = {v['series_number']: v['export_status'] for v in rows(project / 'BIDS/selection_volumes.csv')}
    assert [status[n] for n in ('20', '21', '22', '23', '24', '30', '31')] == \
        ['standard', 'standard', 'conventional', 'conventional', 'conventional', 'standard', 'standard']


def test_two_sessions_on_one_date_are_one_bids_session(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': first_study(tmp_path),
                                                    'S001_20260725_2.zip': second_study(tmp_path)})
    assert select(project, tmp_path, '--bids', '--publish') == 0
    subject = project / 'BIDS/sub-S001'
    assert sorted(p.name for p in subject.iterdir()) == ['ses-20260725', 'sub-S001_sessions.json',
                                                         'sub-S001_sessions.tsv']
    assert (subject / 'sub-S001_sessions.tsv').read_text() == \
        'session_id\tsource_sessions\nses-20260725\t20260725,20260725_2\n'
    assert {f.split('/')[0] for f in tree(subject / 'ses-20260725') if '/' in f} == {'anat'}


def test_subset_runs_keep_other_participants_and_user_columns(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': bids_exam(tmp_path),
                                                    'S002_20260725.zip': bids_exam(tmp_path)})
    listing = tmp_path / 'ids.csv'
    listing.write_text('subject_id\nS001\n')
    assert select(project, tmp_path, '--bids', '--publish', '--subjects', listing) == 0
    participants = project / 'BIDS/participants.tsv'
    participants.write_text('participant_id\tgroup\nsub-S001\tcontrol\n')
    assert select(project, tmp_path, '--bids', '--publish') == 0
    assert participants.read_text() == 'participant_id\tgroup\nsub-S001\tcontrol\nsub-S002\tn/a\n'
    listing.write_text('subject_id\nS002\n')
    assert select(project, tmp_path, '--bids', '--publish', '--subjects', listing) == 0
    assert participants.read_text() == 'participant_id\tgroup\nsub-S001\tcontrol\nsub-S002\tn/a\n'


def test_complete_only_with_bids(project, tmp_path, converter):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': bids_exam(tmp_path)})
    assert select(project, tmp_path, '--bids', '--publish', '--modalities', 'T1,T1_POST', '--complete-only') == 0
    assert not (project / 'BIDS/sub-S001').exists()
    t1 = next(v for v in rows(project / 'BIDS/selection_volumes.csv') if v['role'] == 'T1')
    assert (t1['export_status'], t1['published']) == ('standard', 'false')  # planned, but the visit was held
    assert select(project, tmp_path, '--bids', '--publish', '--modalities', 'T1,DWI', '--complete-only') == 0
    assert (project / SESSION / 'fmap').is_dir()


@pytest.mark.parametrize('point', ['staged', 'moved-aside', 'swapped'])
def test_interrupted_bids_publication_is_finished_by_the_next_run(project, tmp_path, converter, point):
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': bids_exam(tmp_path, repeat=True)})
    profile = write_profile(tmp_path)
    assert select(project, tmp_path, '--bids', '--publish') == 2  # the repeated T1 needs review
    t1 = next(r for r in rows(project / 'BIDS/selection.csv') if r['role'] == 'T1')
    (project / 'selection_overrides.csv').write_text(
        f'subject_id,session,role,action,candidate_id\nS001,20260725,T1,choose,{t1["rule_recommendation"]}\n')
    assert run_with_crash(project, 'select', point, '--profile', profile, '--bids', '--publish') == 137
    assert select(project, tmp_path, '--bids', '--publish') == 0
    subject = project / 'BIDS/sub-S001'
    assert sorted(p.name for p in subject.iterdir()) == ['ses-20260725', 'sub-S001_sessions.json',
                                                         'sub-S001_sessions.tsv']
    assert (subject / f'ses-20260725/anat/{P}_T1w.nii.gz').exists()


# ---- external checks: the BIDS validator and pybids (CI installs both; locally they skip) ------------------------

def validator():
    found = os.environ.get('MRI_PIPELINE_BIDS_VALIDATOR') or shutil.which('bids-validator-deno')
    if not found:
        pytest.skip('bids-validator-deno is not installed (set MRI_PIPELINE_BIDS_VALIDATOR)')
    return found


def test_published_dataset_passes_the_bids_validator(project, tmp_path, converter):
    exe = validator()
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': bids_exam(tmp_path, post=True),
                                                    'S002_20260725.zip': first_study(tmp_path),
                                                    'S002_20260725_2.zip': second_study(tmp_path),
                                                    'S001_20260801.zip': susceptibility_exam(tmp_path, '20260801')})
    assert select(project, tmp_path, '--bids', '--publish') == 0
    # The pipeline's receipts, manifests, marker and lock files are dotfiles, which the validator skips.
    result = subprocess.run([exe, str(project / 'BIDS'), '--format', 'json'], capture_output=True, text=True,
                            timeout=600)
    report = json.loads(result.stdout)
    issues = report['issues']['issues']
    errors = [i for i in issues if i.get('severity') == 'error']
    assert not errors, json.dumps(errors, indent=1)[:5000]
    assert 'UNKNOWN_BIDS_VERSION' not in {i.get('code') for i in issues}


def test_filter_file_selects_the_precontrast_t1w(project, tmp_path, converter):
    layout_module = pytest.importorskip('bids.layout')
    convert_sessions(project, tmp_path, converter, {'S001_20260725.zip': bids_exam(tmp_path, post=True)})
    assert select(project, tmp_path, '--bids', '--publish', '--bids-contrast-agent', 'gadolinium') == 0
    root = project / 'BIDS'
    layout = layout_module.BIDSLayout(root, validate=False)
    query = {'datatype': 'anat', 'suffix': 'T1w', 'part': ['mag', None], 'extension': ['.nii', '.nii.gz']}
    found = {Path(f).name for f in layout.get(return_type='file', **query)}
    assert found == {f'{P}_T1w.nii.gz', f'{P}_ce-gadolinium_T1w.nii.gz'}  # the default query takes both
    filters = json.loads((root / 'code/bids_filter_precontrast.json').read_text())['t1w']
    from bids.layout import Query
    query.update({k: Query.NONE if v is None else v for k, v in filters.items()})
    assert {Path(f).name for f in layout.get(return_type='file', **query)} == {f'{P}_T1w.nii.gz'}
