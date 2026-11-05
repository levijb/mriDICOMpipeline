"""Identity grammar and roster tests (T01, T02)."""
import pytest

from mri_dicom_pipeline import identity


def write_roster(tmp_path, rows):
    roster = tmp_path / 'sessions.csv'
    roster.write_text('subject_id,scan_date\n' + ''.join(f'{sid},{date}\n' for sid, date in rows))
    return roster


def test_case_only_id_collision_ignored_with_warning(tmp_path):
    roster = write_roster(tmp_path, [('S001', '2026-07-25'), ('s001', '2026-07-26'),
                                     ('S002', '2026-07-25'), ('S001', '2026-08-01')])
    ids, pairs, warnings = identity.load_roster(roster)
    assert ids == {'S002'}
    assert pairs == {('S002', '20260725')}
    [warning] = warnings
    assert "'S001' (row 2)" in warning and "'s001' (row 3)" in warning


@pytest.mark.parametrize('name', ['CON', 'nul', 'Com1', 'LPT9'])
def test_reserved_device_names_rejected(tmp_path, name):
    roster = write_roster(tmp_path, [(name, '2026-07-25'), ('S002', '2026-07-25')])
    ids, pairs, warnings = identity.load_roster(roster)
    assert ids == {'S002'}
    assert warnings == ['Reference row 2: invalid subject_id; row ignored']
    with pytest.raises(ValueError):
        identity.parse_name(f'{name}_20260725.zip')


def test_session_key_grammar():
    for bad in ('S001_20260725_1.zip', 'S001_20260725_02.zip'):
        with pytest.raises(ValueError):
            identity.parse_name(bad)
    assert identity.parse_name('S001_20260725_2.zip') == ('S001', '20260725', '20260725_2')
    assert identity.parse_name('S001_20260725_10.zip') == ('S001', '20260725', '20260725_10')
