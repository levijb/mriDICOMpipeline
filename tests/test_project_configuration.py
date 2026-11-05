"""Study independence and public entry-point regression tests."""
import csv
import json
from pathlib import Path
import subprocess
import sys

import pytest

from mri_dicom_pipeline import __version__
from mri_dicom_pipeline import config, fsutil, identity
from mri_dicom_pipeline.state import State
from mri_dicom_pipeline.cli import main
from support import archive, dicom_bytes


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
