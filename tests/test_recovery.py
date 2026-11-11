"""Crash-recovery, stop-signal and state-index tests (T07-T09; P5, P11)."""
import _thread
from collections import namedtuple
import csv
import errno
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time

from pydicom.uid import generate_uid
import pytest

from mri_dicom_pipeline import convert, fsutil, nifti
from mri_dicom_pipeline.state import State
from support import archive, archive_objects, call, child_env, dicom_bytes, run_with_crash
from support import state as read_state

KEY, OTHER = 'S001/20260725', 'S002/20260725'


def fail_if_called(*args, **kwargs):
    pytest.fail('unexpected call')


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


def lock_index(monkeypatch, times):
    """Reading index.json fails with Access denied `times` times, as while a virus scanner has it open."""
    original, refused = Path.read_text, []

    def busy(self, *args, **kwargs):
        if self.name == 'index.json' and len(refused) < times:
            refused.append(self)
            raise PermissionError(13, 'Access is denied')
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', busy)
    return refused


def test_briefly_locked_index_is_read_not_recovered(tmp_path, monkeypatch):
    two_flushes_then_journal(tmp_path)
    refused = lock_index(monkeypatch, 2)
    loaded = State(tmp_path)
    assert len(refused) == 2 and not loaded.recovered_from_backup
    assert status(loaded) == 'v3'


def test_locked_index_stops_instead_of_recovering(tmp_path, monkeypatch):
    """Only unparsable contents count as corruption; an index that stays locked stops the run."""
    index, backup = two_flushes_then_journal(tmp_path)
    before = (index.read_bytes(), backup.read_bytes())
    monkeypatch.setattr(fsutil, 'LOCKED_RETRY_SECONDS', 0.3)
    lock_index(monkeypatch, 1000)
    with pytest.raises(PermissionError):
        State(tmp_path)
    monkeypatch.undo()
    assert (index.read_bytes(), backup.read_bytes()) == before


def history_rows(project):
    with (project / 'Reports/run_history.csv').open(newline='') as f:
        return list(csv.DictReader(f))


def test_history_row_written_on_early_abort(project):
    """R16: a locked run that stops early still writes a history row, with outcome aborted."""
    state_dir = project / 'Reports/.state'
    state_dir.mkdir(parents=True)
    (state_dir / 'index.json').write_text('not json')
    (state_dir / 'index.json.bak').write_text('not json either')
    assert call(project, 'prepare') == 2
    assert history_rows(project)[-1]['outcome'] == 'aborted'


def test_recovered_index_noted_in_history(project, tmp_path):
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    assert call(project, 'prepare') == 0  # the second save keeps index.json.bak
    (project / 'Reports/.state/index.json').write_text('{"truncated')
    assert call(project, 'prepare') == 0
    assert 'recovered' in history_rows(project)[-1]['notes']


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
    # The remaining rows of the design's crash matrix (plan H-01).
    ('copying', 'prepare', 'new', (), 'redone'),
    ('moved-aside', 'prepare', 'prepared', ('--force-reextract',), 'rolled-back'),
    ('journaled', 'prepare', 'new', (), 'kept'),
    ('index-rotated', 'convert', 'prepared', (), 'kept'),
    # Killed before the old folder moved aside: the old folder stays, and the new copy is removed.
    ('staged', 'prepare', 'prepared', ('--force-reextract',), 'rolled-back'),
    ('staged', 'convert', 'converted', ('--force-reconvert',), 'rolled-back'),
]


def crash_case_id(case):
    point, phase, start, extra, expected = case
    return f'{phase}-{point}' + ('-over-existing' if point == 'staged' and extra else '')


@pytest.mark.parametrize('point, phase, start, extra, expected', CRASH_CASES,
                         ids=[crash_case_id(case) for case in CRASH_CASES])
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


def test_history_counts_reconciled_intents(project, tmp_path, converter):
    project_at(project, tmp_path, converter, 'prepared')
    options = ('--dcm2niix-bin', converter)
    assert run_with_crash(project, 'convert', 'swapped', *options) == 137
    assert call(project, 'convert', *options) == 0
    assert history_rows(project)[-1]['reconciled'] == '1'


def test_enospc_during_publish_keeps_old_output(project, tmp_path, converter, monkeypatch):
    """T09: a full disk during publication leaves the complete folder and its record in place."""
    import errno
    project_at(project, tmp_path, converter, 'converted')
    folder, options = published(project, 'convert'), ('--dcm2niix-bin', converter)
    before = receipt_id(folder)
    original_rename = Path.rename

    def disk_full(self, target):
        if Path(target).name.startswith('.incoming_'):
            raise OSError(errno.ENOSPC, 'No space left on device (simulated)')
        return original_rename(self, target)

    monkeypatch.setattr(Path, 'rename', disk_full)
    assert call(project, 'convert', *options, '--force-reconvert') == 2
    monkeypatch.undo()
    assert receipt_id(folder) == before
    entry = read_state(project)['sessions']['S001/20260725']
    assert entry['conversion']['receipt_id'] == before and entry['last_attempt']['status'] == 'failed'
    assert not [p for p in folder.parent.iterdir() if p.name.startswith(('.incoming_', '.previous_'))]


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


def folder_contents(folder):
    return {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}


@pytest.mark.parametrize('change', ['other-receipt', 'no-receipt'])
def test_unexpected_receipt_after_a_crash_waits_for_a_person(project, tmp_path, converter, change):
    """Q12 table, last row, then the runbook's recover: procedure: delete the intent file and rerun (plan H-03)."""
    project_at(project, tmp_path, converter, 'prepared')
    options = ('--dcm2niix-bin', converter)
    assert run_with_crash(project, 'convert', 'swapped', *options) == 137
    folder = published(project, 'convert')
    receipt = folder / '.pipeline_complete.json'
    if change == 'other-receipt':
        receipt.write_text(json.dumps(dict(json.loads(receipt.read_text()), receipt_id='not-the-recorded-one')))
    else:
        receipt.unlink()
    contents = folder_contents(folder)
    assert call(project, 'convert', *options) == 2
    assert folder_contents(folder) == contents
    [issue] = [key for key in read_state(project)['issues'] if key.startswith('recover:')]
    assert read_state(project)['issues'][issue]['path'] == str(folder)
    assert not read_state(project)['sessions'][KEY].get('conversion')
    intent = project / 'Reports/.state/intents' / (issue[len('recover:'):] + '.json')
    intent.unlink()  # the runbook step, after inspecting the folder
    if change == 'other-receipt':
        # The folder still passes the adoption check (complete, same input, every file present), so it is adopted.
        assert call(project, 'convert', *options) == 0
        assert read_state(project)['sessions'][KEY]['conversion']['status'] == 'converted'
        assert not read_state(project)['issues'] and folder_contents(folder) == contents
    else:
        # A folder without a receipt is not the pipeline's to replace; it has to be moved away first.
        assert call(project, 'convert', *options) == 2
        issues = read_state(project)['issues']
        assert list(issues) == [f'convert:{KEY}'] and 'not owned by this pipeline' in issues[f'convert:{KEY}']['reason']
        assert folder_contents(folder) == contents
        folder.rename(tmp_path / 'moved_aside')
        assert call(project, 'convert', *options, '--retry-failed') == 0
        assert not read_state(project)['issues']


# ---- Publishing across filesystems and cleanup failures (plan F-01, F-02, F-06) ---------

@pytest.mark.parametrize('kind', ['exdev', 'winerror-17'])
def test_cross_device_publish_completes(project, tmp_path, converter, monkeypatch, kind):
    """Staging on another filesystem, such as node-local scratch: the folder is copied, then swapped in."""
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    scratch, original = tmp_path / 'scratch', Path.rename

    def rename_fails_across_filesystems(self, target):
        if Path(target).name.startswith('.incoming_'):
            if kind == 'exdev':
                raise OSError(errno.EXDEV, 'Invalid cross-device link (simulated)')
            error = OSError('The system cannot move the file to a different disk drive (simulated)')
            error.winerror = 17
            raise error
        return original(self, target)

    monkeypatch.setattr(Path, 'rename', rename_fails_across_filesystems)
    convert_options = ('--dcm2niix-bin', converter, '--staging-root', scratch)
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', *convert_options) == 0
    assert_recovered(project)
    assert not list((project / '.work').glob('prepare_*')) and not list(scratch.glob('convert_*'))
    session = read_state(project)['sessions'][KEY]
    assert session['status'] == 'ready' and session['conversion']['status'] == 'converted'
    # The receipts recorded after the copy are valid: a rerun finds nothing to do.
    receipts_before = [receipt_id(published(project, phase)) for phase in ('prepare', 'convert')]
    assert call(project, 'prepare') == 0 and call(project, 'convert', *convert_options) == 0
    assert [receipt_id(published(project, phase)) for phase in ('prepare', 'convert')] == receipts_before


def test_retry_locked_retries_only_permission_errors(monkeypatch):
    """fsutil.retry_locked: Access denied is retried until the deadline; any other error is raised at once."""
    monkeypatch.setattr(fsutil, 'LOCKED_RETRY_SECONDS', 0.5)
    calls = []

    def busy_twice():
        calls.append('busy')
        if len(calls) < 3:
            raise PermissionError(13, 'Access is denied')
        return 'done'

    assert fsutil.retry_locked(busy_twice) == 'done' and len(calls) == 3

    def always(error):
        def action():
            calls.append('again')
            raise error
        return action

    calls.clear()
    started = time.monotonic()
    with pytest.raises(PermissionError):
        fsutil.retry_locked(always(PermissionError(13, 'Access is denied')))
    assert len(calls) > 1 and time.monotonic() - started < 5
    # Pin (SB-6): "directory not empty", which rmtree can raise while another program holds a
    # just-deleted file, is not retried.
    for error in (FileNotFoundError(errno.ENOENT, 'No such file'), OSError(errno.ENOTEMPTY, 'Directory not empty')):
        calls.clear()
        with pytest.raises(type(error)):
            fsutil.retry_locked(always(error))
        assert len(calls) == 1


FORCED = {'prepare': '--force-reextract', 'convert': '--force-reconvert'}


def previous_folder_cannot_be_removed(monkeypatch):
    """Make deleting .previous_* fail, as when the old folder is read-only or held open (SB-6)."""
    original = shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if Path(path).name.startswith('.previous_'):
            raise PermissionError(13, 'Access is denied (simulated)')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(fsutil, 'LOCKED_RETRY_SECONDS', 0.2)
    monkeypatch.setattr(shutil, 'rmtree', rmtree)


def recorded_receipt(project, phase):
    session = read_state(project)['sessions'][KEY]
    return session['extraction' if phase == 'prepare' else 'conversion']['receipt_id']


def leftover_folders(folder):
    return [p for p in folder.parent.iterdir() if p.name.startswith('.previous_')]


@pytest.mark.parametrize('phase', ['prepare', 'convert'])
def test_cleanup_failure_after_the_swap_is_not_a_failed_attempt(project, tmp_path, converter, monkeypatch, phase):
    """SB-6 (plan F-02): the new folder is recorded, and later runs retry the cleanup instead of aborting."""
    project_at(project, tmp_path, converter, 'converted')
    folder = published(project, phase)
    options = ('--dcm2niix-bin', converter) if phase == 'convert' else ()
    before = receipt_id(folder)
    with monkeypatch.context() as patch:
        previous_folder_cannot_be_removed(patch)
        assert call(project, phase, *options, FORCED[phase]) == 0
        swapped_in = receipt_id(folder)
        assert swapped_in != before and recorded_receipt(project, phase) == swapped_in
        assert len(leftover_folders(folder)) == 1 and not read_state(project)['issues']
        # While the old folder still cannot be deleted, a plain run is not stopped by it.
        assert call(project, phase, *options) == 0
        assert history_rows(project)[-1]['outcome'] != 'aborted' and len(leftover_folders(folder)) == 1
    # Once it can be deleted, the next plain run removes it and leaves nothing open.
    assert call(project, phase, *options) == 0
    assert_recovered(project)
    assert receipt_id(folder) == recorded_receipt(project, phase) == swapped_in
    assert not read_state(project)['issues']


def test_failed_forced_reextraction_issue_clears_on_the_next_plain_run(project, tmp_path, monkeypatch):
    """SB-12, found while writing F-02: a failed --force-reextract of a valid session left a blocking issue for good.

    The extraction on disk is still the valid one. After a failed
    --force-reconvert, convert clears the matching convert: issue on its next
    plain run.
    """
    project_at(project, tmp_path, None, 'prepared')
    usage = namedtuple('usage', 'total used free')
    with monkeypatch.context() as patch:
        patch.setattr(shutil, 'disk_usage', lambda path: usage(10**12, 10**12 - 1000, 1000))
        assert call(project, 'prepare', '--force-reextract') == 2
    assert call(project, 'prepare') == 0
    assert not read_state(project)['issues']


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


@pytest.mark.parametrize('stop_at', [1, 2], ids=['between-volumes', 'before-publish'])
def test_stop_while_organizing_publishes_nothing(project, tmp_path, converter, monkeypatch, stop_at):
    """SB-3: a Ctrl+C that arrives after the converter exited, while volumes are gzipped, publishes nothing."""
    study = generate_uid()
    archive_objects(project, 'S001_20260725.zip', [dicom_bytes(tmp_path, study=study), dicom_bytes(tmp_path, study=study)])
    assert call(project, 'prepare') == 0
    original, gzipped = nifti.validate_and_gzip, []

    def gzip_then_stop(*args, **kwargs):
        result = original(*args, **kwargs)
        gzipped.append(args[0])
        if len(gzipped) == stop_at:
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)  # as if Ctrl+C arrived now
        return result

    monkeypatch.setattr(nifti, 'validate_and_gzip', gzip_then_stop)
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 130
    assert len(gzipped) == stop_at
    assert not list((project / 'Niftis').rglob('.pipeline_complete.json'))
    assert read_state(project)['sessions'][KEY]['conversion']['status'] == 'interrupted'


def ctrl_c_after_first_gzip(monkeypatch):
    """Deliver Ctrl+C to the run just after it gzips its first volume, before anything is published."""
    original = nifti.validate_and_gzip

    def gzip_then_stop(*args, **kwargs):
        result = original(*args, **kwargs)
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        return result

    monkeypatch.setattr(nifti, 'validate_and_gzip', gzip_then_stop)


def test_interrupted_conversion_is_retried_by_the_next_plain_run(project, tmp_path, converter, monkeypatch):
    """README: interrupted, unfinished work is retried automatically on the next invocation (plan H-08)."""
    project_at(project, tmp_path, converter, 'prepared')
    options = ('--dcm2niix-bin', converter)
    with monkeypatch.context() as patch:
        ctrl_c_after_first_gzip(patch)
        assert call(project, 'convert', *options) == 130
    assert read_state(project)['sessions'][KEY]['conversion']['status'] == 'interrupted'
    assert call(project, 'convert', *options) == 0
    assert read_state(project)['sessions'][KEY]['conversion']['status'] == 'converted'
    assert not read_state(project)['issues']


def test_interrupted_explicit_reconversion_is_not_retried(project, tmp_path, converter, monkeypatch):
    """README: ...except an interrupted explicit reconversion; the complete folder stays as it was (plan H-08)."""
    project_at(project, tmp_path, converter, 'converted')
    folder, options = published(project, 'convert'), ('--dcm2niix-bin', converter)
    before = receipt_id(folder)
    with monkeypatch.context() as patch:
        ctrl_c_after_first_gzip(patch)
        assert call(project, 'convert', *options, '--force-reconvert') == 130
    entry = read_state(project)['sessions'][KEY]
    assert entry['conversion']['receipt_id'] == before and entry['last_attempt']['status'] == 'interrupted'
    monkeypatch.setattr(convert, 'convert_one', fail_if_called)
    assert call(project, 'convert', *options) == 0
    assert receipt_id(folder) == before


def process_alive(pid):
    """True while pid runs; a zombie waiting to be reaped counts as stopped (Linux /proc)."""
    try:
        return '\nState:\tZ' not in Path(f'/proc/{pid}/status').read_text()
    except FileNotFoundError:
        return False


@pytest.mark.skipif(os.name == 'nt', reason='process groups and real SIGTERM delivery are POSIX-only')
def test_sigterm_stops_converter_group_and_exits_143(project, tmp_path, converter):
    """R11: a real SIGTERM stops the converter and the process it started, and the run exits 143."""
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    child_file = tmp_path / 'child.pid'
    env = child_env(FAKE_CONVERTER_CHILD=str(child_file), FAKE_CONVERTER_SLEEP='60')
    code = 'import sys; from mri_dicom_pipeline import runner; sys.exit(runner.main("convert", sys.argv[1:]))'
    run = subprocess.Popen([sys.executable, '-c', code, '--project-root', str(project),
                            '--dcm2niix-bin', str(converter)], env=env)
    try:
        deadline = time.monotonic() + 60
        while not (child_file.exists() and child_file.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.1)
        child = int(child_file.read_text())
        run.send_signal(signal.SIGTERM)
        assert run.wait(timeout=60) == 143
    finally:
        if run.poll() is None:
            run.kill()
    deadline = time.monotonic() + 10
    while process_alive(child) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not process_alive(child)
    assert read_state(project)['sessions']['S001/20260725']['conversion']['status'] == 'interrupted'
