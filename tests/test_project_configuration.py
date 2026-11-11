"""Study independence and public entry-point regression tests."""
import csv
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from mri_dicom_pipeline import __version__
from mri_dicom_pipeline import config, fsutil, identity
from mri_dicom_pipeline.state import State
from mri_dicom_pipeline.cli import main
from support import archive, call, dicom_bytes, state


@pytest.mark.parametrize('raw', ['20260725', '2026-07-25', '7/25/2026'])
def test_custom_roster_schema_preserves_ids(tmp_path, raw):
    roster = tmp_path / 'visits.csv'
    with roster.open('w', newline='', encoding='utf-8-sig') as f:
        csv.writer(f).writerows([['participant', 'acquired'], ['site-A_001', raw], ['0007', raw]])
    ids, pairs, warnings = identity.load_roster(roster, 'participant', 'acquired')
    assert ids == {'site-A_001', '0007'}
    assert pairs == {('site-A_001', '20260725'), ('0007', '20260725')}
    assert not warnings


def test_custom_schema_used_in_both_stages(tmp_path, converter):
    root = tmp_path / 'study'
    root.mkdir()
    roster = tmp_path / 'external_visits.csv'
    roster.write_text('participant,acquired\nsite-A_001,2026-07-25\n')
    archive(root, 'site-A_001_20260725_2.zip', dicom_bytes(tmp_path))
    args = ['--project-root', str(root), '--reference-csv', str(roster),
            '--subject-column', 'participant', '--date-column', 'acquired']
    assert main(['prepare', *args]) == 0
    assert main(['convert', *args, '--dcm2niix-bin', str(converter)]) == 0
    output = root / 'Niftis/Subjects/site-A_001/20260725_2'
    metadata = json.loads(next((output / 'ADC').glob('*.json')).read_text())
    assert metadata['subject_id'] == 'site-A_001'
    assert (output / '.pipeline_complete.json').is_file()
    with (root / 'Reports/session_status.csv').open(newline='') as f:
        rows = list(csv.DictReader(f))
    assert {row['subject_id'] for row in rows} == {'site-A_001'}


def test_default_project_is_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = config.configure(config.config_parser('prepare').parse_args([]), 'prepare')
    assert cfg.project_root == tmp_path
    assert cfg.reference == tmp_path / 'sessions.csv'


# ---- Storage roots, workers and limits (plan F-07, G1-04, J-01) ----------------------

def configured(phase, project, *options):
    arguments = ['--project-root', str(project), *map(str, options)]
    return config.configure(config.config_parser(phase).parse_args(arguments), phase)


@pytest.mark.parametrize('phase, option, relative', [
    ('prepare', '--output-root', 'DicomUploads'),              # the uploads folder itself
    ('prepare', '--output-root', 'DicomUploads/Extracted'),    # inside the uploads folder
    ('prepare', '--input-root', 'Reports/incoming'),           # inside the reports folder
    ('prepare', '--report-root', '.'),                         # holds every other root
    ('convert', '--output-root', 'DicomExtracted'),
    ('convert', '--staging-root', 'Niftis/.work'),
    ('convert', '--dicom-root', 'Reports/.state'),
    ('convert', '--report-root', 'Niftis/../Niftis/Reports'),  # compared after the path is normalized
])
def test_overlapping_storage_roots_refused(tmp_path, phase, option, relative):
    """A root inside another would let a publish or a cleanup in one reach into the other."""
    with pytest.raises(ValueError, match='Storage roots overlap'):
        configured(phase, tmp_path, option, tmp_path / relative)


def test_separate_storage_roots_accepted(tmp_path):
    cfg = configured('convert', tmp_path / 'study', '--report-root', tmp_path / 'reports', '--staging-root',
                     tmp_path / 'scratch', '--dicom-root', tmp_path / 'dicoms', '--output-root', tmp_path / 'niftis')
    assert (cfg.reports, cfg.work, cfg.dicoms, cfg.niftis) == (
        tmp_path / 'reports', tmp_path / 'scratch', tmp_path / 'dicoms', tmp_path / 'niftis')


@pytest.mark.skipif(os.name != 'nt', reason='Windows paths ignore letter case')
def test_overlap_check_ignores_letter_case_on_windows(tmp_path):
    with pytest.raises(ValueError, match='Storage roots overlap'):
        configured('convert', tmp_path, '--report-root', tmp_path / 'niftis')


def test_overlap_check_follows_symbolic_links(tmp_path):
    (tmp_path / 'Niftis').mkdir()
    alias = tmp_path / 'alias'
    try:
        alias.symlink_to(tmp_path / 'Niftis', target_is_directory=True)
    except OSError:
        pytest.skip('symbolic links cannot be created here')
    with pytest.raises(ValueError, match='Storage roots overlap'):
        configured('convert', tmp_path, '--report-root', alias)


@pytest.mark.parametrize('slurm, option, workers', [
    (None, None, 1), (None, 3, 3), ('8', None, 4), ('2', None, 2), ('4', 4, 4), ('16', 6, 6)])
def test_worker_count_defaults_and_slurm_cap(tmp_path, monkeypatch, slurm, option, workers):
    """README: the default is min(4, SLURM_CPUS_PER_TASK), or one worker outside Slurm."""
    monkeypatch.delenv('SLURM_CPUS_PER_TASK', raising=False)
    if slurm is not None:
        monkeypatch.setenv('SLURM_CPUS_PER_TASK', slurm)
    options = [] if option is None else ['--workers', option]
    assert configured('convert', tmp_path, *options).workers == workers


@pytest.mark.parametrize('options, slurm, message', [
    (['--workers', '5'], '4', 'exceeds SLURM_CPUS_PER_TASK'),
    (['--workers', '0'], None, 'must be positive'),
    (['--workers', '-2'], None, 'must be positive'),
    ([], '0', 'must be positive'),
    ([], 'many', 'invalid literal'),
    (['--limit', '0'], None, 'must be positive'),
    (['--limit', '-1'], None, 'must be positive'),
    (['--timeout', '0'], None, 'timeout must be positive'),
    (['--output-root', '{project}/DicomExtracted'], None, 'Storage roots overlap'),
    (['--session', 'S001-20260725'], None, 'Invalid session key'),
])
def test_invalid_settings_exit_2_before_anything_is_written(project, monkeypatch, capsys, options, slurm, message):
    """README: --workers cannot exceed SLURM_CPUS_PER_TASK; every refused setting stops the run at once."""
    monkeypatch.delenv('SLURM_CPUS_PER_TASK', raising=False)
    if slurm is not None:
        monkeypatch.setenv('SLURM_CPUS_PER_TASK', slurm)
    before = sorted(project.rglob('*'))
    options = [option.format(project=project) for option in options]
    assert main(['convert', '--project-root', str(project), *options]) == 2
    error = capsys.readouterr().err
    assert error.startswith('ERROR: ') and message in error
    assert sorted(project.rglob('*')) == before


def test_limit_selects_new_work_after_cache_checks(tmp_path, converter):
    """README: --limit N selects work after cache checks, so a pilot can reach new scans beyond completed ones."""
    root = tmp_path / 'study'
    root.mkdir()
    sessions = [('S001', '20260725'), ('S001', '20260801'), ('S002', '20260725'), ('S002', '20260801'),
                ('S003', '20260725')]
    (root / 'sessions.csv').write_text('subject_id,scan_date\n' + ''.join(f'{sid},{date}\n' for sid, date in sessions))
    for sid, date in sessions:
        archive(root, f'{sid}_{date}.zip', dicom_bytes(tmp_path, fields={'StudyDate': date}))
    keys = [f'{sid}/{date}' for sid, date in sessions]

    def done(command):
        entries = state(root)['sessions']
        if command == 'prepare':
            return sorted(key for key, entry in entries.items() if entry['status'] == 'ready')
        return sorted(key for key, entry in entries.items() if entry.get('conversion', {}).get('status') == 'converted')

    assert call(root, 'prepare', '--limit', 2) == 0
    assert done('prepare') == keys[:2]
    assert call(root, 'prepare', '--limit', 2) == 0  # the two prepared sessions are cached and do not count
    assert done('prepare') == keys[:4]
    assert call(root, 'prepare', '--limit', 2) == 0
    assert done('prepare') == keys
    # --session narrows the run first; the limit then counts within the named sessions.
    named = [option for key in keys[2:] for option in ('--session', key)]
    assert call(root, 'convert', '--dcm2niix-bin', converter, '--limit', 2, *named) == 0
    assert done('convert') == keys[2:4]
    assert call(root, 'convert', '--dcm2niix-bin', converter, '--limit', 2) == 0
    assert done('convert') == keys[:4]


def test_publish_rolls_back_if_swap_fails(tmp_path, monkeypatch):
    root = tmp_path / 'managed'
    destination = root / 'Subjects/S001/20260725'
    destination.mkdir(parents=True)
    (destination / 'image').write_text('previous')
    stage = tmp_path / 'stage'
    stage.mkdir()
    (stage / 'image').write_text('replacement')
    original = Path.rename

    def fail_final_swap(path, target):
        if path.name.startswith('.incoming_') and target == destination:
            raise OSError('simulated publish failure')
        return original(path, target)

    monkeypatch.setattr(Path, 'rename', fail_final_swap)
    with pytest.raises(OSError, match='simulated publish failure'):
        fsutil.publish_tree(stage, destination, root, *fsutil.swap_names(destination))
    assert (destination / 'image').read_text() == 'previous'
    assert not list(destination.parent.glob('.incoming_*'))
    assert not list(destination.parent.glob('.previous_*'))


@pytest.mark.parametrize('header', ['id,id', 'subject_id,other'])
def test_ambiguous_or_missing_columns_rejected(tmp_path, header):
    roster = tmp_path / 'sessions.csv'
    roster.write_text(header + '\nS001,20260725\n')
    with pytest.raises(ValueError):
        identity.load_roster(roster)


@pytest.mark.parametrize('version', [1, 2])
def test_old_state_rejected_without_writes(tmp_path, version):
    index = tmp_path / '.state/index.json'
    index.parent.mkdir()
    original = '{"version":%d,"sessions":{},"sources":{},"issues":{}}' % version
    index.write_text(original)
    with pytest.raises(ValueError, match='Unsupported state version'):
        State(tmp_path)
    assert index.read_text() == original


@pytest.mark.parametrize('args', [['--help'], ['--version'], ['prepare', '--help'], ['convert', '--help']])
def test_public_cli_help(args, capsys):
    with pytest.raises(SystemExit) as exit:
        main(args)
    assert exit.value.code == 0
    output = capsys.readouterr().out
    assert output.strip()
    if args == ['--version']:
        assert __version__ in output


@pytest.mark.parametrize('script', ['unzip_and_inventory_dicoms.py', 'convert_dicoms_to_nifti.py'])
def test_source_script_launchers(script):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / 'Scripts' / script), '--help'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '--subject-column' in result.stdout
