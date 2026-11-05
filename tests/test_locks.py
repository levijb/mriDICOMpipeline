"""Per-root lock tests (T10, local part; P6)."""
import subprocess
import sys

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
