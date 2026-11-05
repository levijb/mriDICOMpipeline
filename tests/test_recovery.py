"""Crash-recovery, stop-signal and state-index tests (T07-T09; P5, P11)."""
import _thread
import csv
import json
from pathlib import Path
import signal
import textwrap
import threading
import time

import pytest

from mri_dicom_pipeline.state import State
from support import archive, call, dicom_bytes, run_with_crash
from support import state as read_state

KEY, OTHER = 'S001/20260725', 'S002/20260725'


def status(state, key=KEY):
    return state.data['sessions'][key]['status']


def two_flushes_then_journal(root):
    """index.json holds v2, index.json.bak holds v1, and v3 exists only as a journal."""
    s = State(root)
    s.checkpoint({'key': OTHER, 'session': {'status': 'only-in-backup'}})
    s.checkpoint({'key': KEY, 'session': {'status': 'v1'}})
    s.flush()
    s.checkpoint({'key': KEY, 'session': {'status': 'v2'}})
    s.flush()
    s.checkpoint({'key': KEY, 'session': {'status': 'v3'}})
    return root / '.state/index.json', root / '.state/index.json.bak'


def test_partial_journal_deletion_does_not_roll_back(tmp_path, monkeypatch):
    """P11: a kill partway through journal deletion must not roll a session back.

    The listing order is forced newest-first, because NTFS's alphabetical
    order hides the defect.
    """
    s = State(tmp_path)
    s.checkpoint({'key': KEY, 'session': {'status': 'v1'}})
    s.flush()
    s.checkpoint({'key': KEY, 'session': {'status': 'v2'}})
    s.checkpoint({'key': KEY, 'session': {'status': 'v3'}})

    class Killed(BaseException):
        pass

    original_glob, original_unlink = Path.glob, Path.unlink
    deleted = []

    def newest_first(self, pattern, *args, **kwargs):
        found = list(original_glob(self, pattern, *args, **kwargs))
        return iter(sorted(found, reverse=True) if self.name == 'pending' else found)

    def unlink_then_die(self, *args, **kwargs):
        if self.parent.name == 'pending':
            if deleted:
                raise Killed()
            deleted.append(self.name)
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'glob', newest_first)
    monkeypatch.setattr(Path, 'unlink', unlink_then_die)
    try:
        s.flush()
    except Killed:
        pass
    monkeypatch.undo()
    assert status(State(tmp_path)) == 'v3'


def test_corrupt_index_recovers_from_backup(tmp_path):
    index, backup = two_flushes_then_journal(tmp_path)
    index.write_text('{"truncated')
    recovered = State(tmp_path, 'RUN1')
    assert recovered.recovered_from_backup
    assert status(recovered) == 'v3' and status(recovered, OTHER) == 'only-in-backup'
    good_backup = backup.read_bytes()
    recovered.flush()
    # The unreadable file is moved aside and never replaces the good backup.
    assert (tmp_path / '.state/index.json.corrupt-RUN1').read_text() == '{"truncated'
    assert backup.read_bytes() == good_backup
    assert status(State(tmp_path)) == 'v3'


def test_missing_index_uses_backup_silently(tmp_path):
    """A crash between the two replaces of a flush leaves only index.json.bak."""
    index, _ = two_flushes_then_journal(tmp_path)
    index.unlink()
    recovered = State(tmp_path)
    assert not recovered.recovered_from_backup
    assert status(recovered) == 'v3' and status(recovered, OTHER) == 'only-in-backup'


def test_dry_run_with_corrupt_index_writes_nothing(project):
    """The unreadable index is moved aside only at a save, and a dry run never saves."""
    index, _ = two_flushes_then_journal(project / 'Reports')
    index.write_text('{"truncated')
    before = {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
    assert call(project, 'prepare', '--dry-run') == 0
    assert before == {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}


def test_both_index_files_corrupt_stops_run(project, capsys):
    state_dir = project / 'Reports/.state'
    state_dir.mkdir(parents=True)
    (state_dir / 'index.json').write_text('not json')
    (state_dir / 'index.json.bak').write_text('not json either')
    with pytest.raises(ValueError) as error:
        State(project / 'Reports')
    assert 'index.json' in str(error.value) and 'index.json.bak' in str(error.value)
    assert call(project, 'prepare') == 2
    assert 'index.json.bak' in capsys.readouterr().err


# ---- Crash injection (Q12) ----------------------------------------------------

def project_at(project, tmp_path, converter, start):
    """Bring the project to the starting point of a crash case."""
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    if start in ('prepared', 'converted', 'second-upload'):
        assert call(project, 'prepare') == 0
    if start == 'converted':
        assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    if start == 'second-upload':
        archive(project, 'S001_20260801.zip', dicom_bytes(tmp_path))


def published(project, phase, label='20260725'):
    return project / ('DicomExtracted' if phase == 'prepare' else 'Niftis') / 'Subjects/S001' / label


def receipt_id(folder):
    if not folder.exists():
        return None
    return json.loads((folder / '.pipeline_complete.json').read_text())['receipt_id']


def assert_recovered(project):
    """No swap folders or open intents remain, and every published folder is complete."""
    for root in (project / 'DicomExtracted', project / 'Niftis'):
        assert not [p for p in root.rglob('*') if p.name.startswith(('.incoming_', '.previous_'))]
        for receipt in root.rglob('.pipeline_complete.json'):
            for item in json.loads((receipt.parent / '.inventory.json').read_text())['files']:
                assert (receipt.parent / item['name']).stat().st_size == item['size']
    assert not list((project / 'Reports/.state/intents').glob('*.json'))


CRASH_CASES = [
    # crash point, command, starting point, extra options, expected result
    ('zip-moved', 'prepare', 'new', (), 'redone'),
    ('staged', 'prepare', 'new', (), 'redone'),
    ('swapped', 'prepare', 'new', (), 'kept'),
    ('index-rotated', 'prepare', 'second-upload', (), 'kept'),
    ('staged', 'convert', 'prepared', (), 'redone'),
    ('copying', 'convert', 'prepared', (), 'redone'),
    ('moved-aside', 'convert', 'converted', ('--force-reconvert',), 'rolled-back'),
    ('swapped', 'convert', 'prepared', (), 'kept'),
    ('journaled', 'convert', 'prepared', (), 'kept'),
]


@pytest.mark.parametrize('point, phase, start, extra, expected', CRASH_CASES,
                         ids=[f'{case[1]}-{case[0]}' for case in CRASH_CASES])
def test_crash_at_boundary_reconciles(project, tmp_path, converter, point, phase, start, extra, expected):
    """P5: after a kill at any publication step, a normal rerun recovers.

    "kept" means the folder published before the kill is adopted or kept, not
    rebuilt; "rolled-back" means the folder from before the run is restored.
    """
    project_at(project, tmp_path, converter, start)
    label = '20260801' if start == 'second-upload' else '20260725'
    folder = published(project, phase, label)
    options = ('--dcm2niix-bin', converter) if phase == 'convert' else ()
    before = receipt_id(folder)
    assert run_with_crash(project, phase, point, *options, *extra) == 137
    crashed = receipt_id(folder)
    assert call(project, phase, *options) == 0
    assert_recovered(project)
    session = read_state(project)['sessions'][f'S001/{label}']
    assert (session['status'] if phase == 'prepare' else session['conversion']['status']) in ('ready', 'converted')
    if expected == 'kept':
        assert crashed is not None and receipt_id(folder) == crashed
    if expected == 'rolled-back':
        assert before is not None and receipt_id(folder) == before
    assert (project / 'DicomUploads/SubjectsSorted/S001' / f'S001_{label}.zip').exists()


def test_crash_during_reconciliation_is_recoverable(project, tmp_path, converter):
    project_at(project, tmp_path, converter, 'converted')
    folder, options = published(project, 'convert'), ('--dcm2niix-bin', converter)
    before = receipt_id(folder)
    assert run_with_crash(project, 'convert', 'moved-aside', *options, '--force-reconvert') == 137
    assert run_with_crash(project, 'convert', 'rolling-back', *options) == 137
    assert call(project, 'convert', *options) == 0
    assert_recovered(project)
    assert receipt_id(folder) == before


def test_adoption_refused_when_volume_deleted(project, tmp_path, converter):
    """A published folder missing a file is never adopted, deleted or replaced."""
    project_at(project, tmp_path, converter, 'prepared')
    options = ('--dcm2niix-bin', converter)
    assert run_with_crash(project, 'convert', 'swapped', *options) == 137
    folder = published(project, 'convert')
    crashed = receipt_id(folder)
    next(folder.rglob('*.nii.gz')).unlink()
    assert call(project, 'convert', *options) == 2
    assert receipt_id(folder) == crashed
    assert list((project / 'Reports/.state/intents').glob('*.json'))
    assert any(key.startswith('recover:') for key in read_state(project)['issues'])


def test_dry_run_reports_reconciliation_without_acting(project, tmp_path, converter, caplog):
    project_at(project, tmp_path, converter, 'prepared')
    options = ('--dcm2niix-bin', converter)
    assert run_with_crash(project, 'convert', 'swapped', *options) == 137
    before = {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
    assert call(project, 'convert', '--dry-run', *options) == 0
    assert 'WOULD RECONCILE convert S001/20260725' in caplog.text
    assert before == {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}


def test_reconcile_skips_other_commands_intents(project, tmp_path, converter):
    project_at(project, tmp_path, converter, 'prepared')
    options = ('--dcm2niix-bin', converter)
    assert run_with_crash(project, 'convert', 'staged', *options) == 137
    intents = sorted((project / 'Reports/.state/intents').glob('*.json'))
    leftovers = list((project / 'Niftis').rglob('.incoming_*'))
    assert intents and leftovers
    assert call(project, 'prepare') == 0
    assert sorted((project / 'Reports/.state/intents').glob('*.json')) == intents
    assert all(p.exists() for p in leftovers)
    assert call(project, 'convert', *options) == 0
    assert_recovered(project)


# ---- Stop signals (Q12, P5) ---------------------------------------------------

SLOW_CONVERTER = textwrap.dedent("""
    import pathlib, sys, time
    out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
    (out / 'started').write_text('')
    time.sleep(60)
""")


@pytest.mark.parametrize('signum, code, outcome', [(signal.SIGTERM, 143, 'terminated'), (signal.SIGINT, 130, 'interrupted')],
                         ids=['sigterm', 'ctrl-c'])
def test_stop_signal_during_conversion(project, tmp_path, signum, code, outcome):
    """A stop ends the converter, keeps the stage, records `interrupted`, and publishes nothing.

    The signal is raised inside the process (_thread.interrupt_main), so the
    test runs on Windows too; step 7 adds the Linux process-group test.
    """
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    slow = tmp_path / 'slow_converter.py'
    slow.write_text(SLOW_CONVERTER)

    def signal_once_converting():
        deadline = time.monotonic() + 60
        while not list((project / '.work').glob('convert_*/raw/started')) and time.monotonic() < deadline:
            time.sleep(0.05)
        _thread.interrupt_main(signum)

    threading.Thread(target=signal_once_converting, daemon=True).start()
    assert call(project, 'convert', '--dcm2niix-bin', slow) == code
    assert not list((project / 'Niftis').rglob('.pipeline_complete.json'))
    assert read_state(project)['sessions']['S001/20260725']['conversion']['status'] == 'interrupted'
    assert list((project / '.work').glob('convert_*/converter.log'))
    with (project / 'Reports/run_history.csv').open(newline='') as f:
        assert list(csv.DictReader(f))[-1]['outcome'] == outcome
