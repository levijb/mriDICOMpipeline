"""Per-root lock tests (T10, local part; P6)."""
import errno
import importlib
import os
import subprocess
import sys
import time

import pytest

from mri_dicom_pipeline import config, locks
from support import call, child_env

# Takes the locks a `prepare` run with these options would take, then waits
# until its standard input closes.
HOLD = '''
import sys
from mri_dicom_pipeline import config, locks
cfg = config.configure(config.config_parser('prepare').parse_args(sys.argv[1:]), 'prepare')
with locks.RootLocks(cfg.lock_roots, 'prepare', 'HOLDER-RUN'):
    print('locked', flush=True)
    sys.stdin.read()
'''


def test_shared_report_root_excludes_different_projects(project, tmp_path, capsys):
    """P6: two projects that share a report root cannot run at the same time."""
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'sessions.csv').write_bytes((project / 'sessions.csv').read_bytes())
    shared = tmp_path / 'SharedReports'
    holder = subprocess.Popen([sys.executable, '-c', HOLD, '--project-root', str(project), '--report-root', str(shared)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                              env=child_env())
    try:
        assert holder.stdout.readline().strip() == 'locked'
        assert call(other, 'prepare', '--report-root', shared) == 2
        assert 'HOLDER-RUN' in capsys.readouterr().err
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
    assert call(other, 'prepare', '--report-root', shared) == 0


def test_lock_failure_names_holder(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    with locks.RootLocks([b], 'convert', 'RUN-B'):
        with pytest.raises(locks.LockBusy, match='RUN-B') as error:
            with locks.RootLocks([a, b], 'prepare', 'RUN-A'):
                pass
        assert str(b) in str(error.value)
        # The lock on a, taken before b was found busy, has been released.
        with locks.RootLocks([a], 'prepare', 'RUN-C'):
            pass
        (b / '.mri_dicom_pipeline.lock.owner').unlink()
        with pytest.raises(locks.LockBusy, match='unknown'):
            with locks.RootLocks([b], 'prepare', 'RUN-D'):
                pass
    assert not list(tmp_path.rglob('*.lock.owner'))


def test_dry_run_takes_no_lock(project):
    cfg = config.configure(config.config_parser('prepare').parse_args(['--project-root', str(project)]), 'prepare')
    with locks.RootLocks(cfg.lock_roots, 'prepare', 'HOLDER'):
        assert call(project, 'prepare', '--dry-run') == 0
        assert call(project, 'prepare') == 2


def test_killed_holder_releases_its_locks(project):
    """The operating system holds the lock, so it ends with its process; the owner file left behind is replaced (plan G1-01)."""
    holder = subprocess.Popen([sys.executable, '-c', HOLD, '--project-root', str(project)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=child_env())
    try:
        assert holder.stdout.readline().strip() == 'locked'
        assert call(project, 'prepare') == 2
    finally:
        holder.kill()
        holder.wait(timeout=30)
        holder.stdin.close()
        holder.stdout.close()
    owner = project / 'Reports' / locks.OWNER_NAME
    assert 'HOLDER-RUN' in owner.read_text()  # the killed holder could not remove it
    cfg = config.configure(config.config_parser('prepare').parse_args(['--project-root', str(project)]), 'prepare')
    # wait() can return before the lock is free: on Windows a venv's python.exe is a launcher whose child holds the
    # lock and is still ending when the launcher has exited. So the second attempt is retried for up to 5 seconds;
    # a lock that is never released still fails the test (round 4 finding R4-F-033, owner's approval 2026-11-25).
    deadline = time.monotonic() + 5
    while True:
        try:
            with locks.RootLocks(cfg.lock_roots, 'prepare', 'NEXT-RUN'):
                assert 'NEXT-RUN' in owner.read_text()
            break
        except locks.LockBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    assert not owner.exists()
    assert call(project, 'prepare') == 0


def test_lock_error_other_than_busy_is_reported_as_such(tmp_path, monkeypatch):
    """SB-9 (plan G1-03): storage that cannot lock at all is not reported as "locked by unknown"."""
    def no_locks_available(*args):
        raise OSError(errno.ENOLCK, 'No locks available')

    module, function = ('msvcrt', 'locking') if os.name == 'nt' else ('fcntl', 'flock')
    monkeypatch.setattr(importlib.import_module(module), function, no_locks_available)
    with pytest.raises((RuntimeError, OSError)) as error:
        with locks.RootLocks([tmp_path / 'root'], 'prepare', 'RUN-A'):
            pass
    assert 'No locks available' in str(error.value) and 'locked by' not in str(error.value)


# Takes the locks the named command would take for the project, then waits until its standard input closes.
HOLD_COMMAND = '''
import sys
from mri_dicom_pipeline import config, locks
phase, root = sys.argv[1], sys.argv[2]
cfg = config.configure(config.config_parser(phase).parse_args(['--project-root', root]), phase)
with locks.RootLocks(cfg.lock_roots, phase, 'HOLDER-RUN'):
    print('locked', flush=True)
    sys.stdin.read()
'''


@pytest.mark.parametrize('holder, other', [('convert', 'prepare'), ('prepare', 'convert')])
def test_commands_exclude_each_other(project, tmp_path, capsys, holder, other):
    """Plan G1-02: prepare and convert on one project share roots, so each blocks the other; dry runs do not."""
    options = ('--dcm2niix-bin', tmp_path / 'unused') if other == 'convert' else ()
    child = subprocess.Popen([sys.executable, '-c', HOLD_COMMAND, holder, str(project)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, env=child_env())
    try:
        assert child.stdout.readline().strip() == 'locked'
        assert call(project, other, *options) == 2
        assert f'({holder}, run HOLDER-RUN' in capsys.readouterr().err
        assert call(project, other, *options, '--dry-run') == 0
    finally:
        child.stdin.close()
        child.wait(timeout=30)
        child.stdout.close()
