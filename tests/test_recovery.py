"""State-index recovery tests (T09; P11). Crash-injection tests join in step 5."""
from pathlib import Path

import pytest

from mri_dicom_pipeline.state import State
from support import call

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
