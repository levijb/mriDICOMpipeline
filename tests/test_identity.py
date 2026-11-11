"""Identity grammar and roster tests (T01, T02)."""
import pytest

from mri_dicom_pipeline import identity
from support import call


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


# ---- ZIP name grammar (plan A-01) ---------------------------------------------------

@pytest.mark.parametrize('name, parsed', [
    ('S001_20260725.zip', ('S001', '20260725', '20260725')),
    ('S001_20260725.ZIP', ('S001', '20260725', '20260725')),
    ('S001_20260725.Zip', ('S001', '20260725', '20260725')),
    ('site-A_001_20240229.zip', ('site-A_001', '20240229', '20240229')),  # hyphen, underscore, 29 February
    ('0007_20260725_2.zip', ('0007', '20260725', '20260725_2')),          # leading zeros are kept
    ('7_20260725.zip', ('7', '20260725', '20260725')),
])
def test_accepted_zip_names(name, parsed):
    assert identity.parse_name(name) == parsed


@pytest.mark.parametrize('name', [
    # Suffixes: an integer of 2 or more, without leading zeros.
    'S001_20260725_0.zip', 'S001_20260725_1.zip', 'S001_20260725_01.zip', 'S001_20260725_2_3.zip',
    # Dates: eight digits that form a real calendar date.
    'S001_20240230.zip', 'S001_20230229.zip', 'S001_20261301.zip', 'S001_20260700.zip', 'S001_2026072.zip',
    'S001_202607255.zip', 'S001_072526.zip', 'S001_2026-07-25.zip', '20260725.zip', '_20260725.zip',
    # Names that copying and downloading produce.
    'S001_20260725 (1).zip', 'S001_20260725 - Copy.zip', 'S001_20260725.zip.zip', 'S001_20260725.zip.part',
    'S001_20260725', '._S001_20260725.zip', '.S001_20260725.zip',
    # IDs: letters, digits, single hyphens and underscores between them.
    'S 001_20260725.zip', ' S001_20260725.zip', 'S001_20260725 .zip', 'S.001_20260725.zip', 'S001__20260725.zip',
    '-S001_20260725.zip', 'S001-_20260725.zip', 'Sé01_20260725.zip', 'S００１_20260725.zip',
    'S001_２０２６０７２５.zip',
    # Windows device names, in any letter case (the four the earlier test does not cover).
    'PRN_20260725.zip', 'aux_20260725.zip', 'Com0_20260725.zip', 'lpt0_20260725.zip',
])
def test_rejected_zip_names(name):
    with pytest.raises(ValueError):
        identity.parse_name(name)


@pytest.mark.parametrize('key, parsed', [('S001/20260725', ('S001', '20260725')),
                                         ('site-A_001/20260725_2', ('site-A_001', '20260725_2'))])
def test_accepted_session_keys(key, parsed):
    assert identity.parse_key(key) == parsed


@pytest.mark.parametrize('key', ['S001', 'S001-20260725', 'S001_20260725', 'S001/', '/20260725', 'S001/2026072',
                                 'S001/20260725_1', 'S001/20260725/2', 'CON/20260725',
                                 # The name S001_20260725_20260801.zip reads as ID "S001_20260725".
                                 'S001/20260725_20260801'])
def test_rejected_session_keys(key):
    with pytest.raises(ValueError, match='Invalid session key'):
        identity.parse_key(key)


# ---- Roster contents (plan A-06) ----------------------------------------------------

def roster_text(tmp_path, text):
    roster = tmp_path / 'sessions.csv'
    roster.write_bytes(text.encode())
    return roster


def test_roster_date_formats_and_row_shapes(tmp_path):
    """README "Input and identity": the accepted date formats, and what each kind of unusable row does."""
    roster = roster_text(tmp_path, '\n'.join([
        'subject_id,scan_date,notes',
        'S001,5/7/2026,month first: 7 May',     # row 2
        ' S002 , 2026-07-25 ,padded cells',     # row 3
        'S003,,an ID without a date',           # row 4
        'S004,7/25/26,two-digit year',          # row 5
        'S005,25/07/2026,day first',            # row 6
        'S006,2026/07/25,year first',           # row 7
        'S007,2026-07-25 10:30,date and time',  # row 8
        'S008,2026-02-30,no such day',          # row 9
        '7,20260725,',                          # row 10
        '0007,20260725,',                       # row 11
        'S009,20260725,more,cells,than,headers',  # row 12
        'S010',                                 # row 13: fewer cells than headers
        'bad id,20260725,',                     # row 14
    ]) + '\n')
    ids, pairs, warnings = identity.load_roster(roster)
    assert pairs == {('S001', '20260507'), ('S002', '20260725'), ('7', '20260725'), ('0007', '20260725'),
                     ('S009', '20260725')}
    # A row with a valid ID registers the ID even when its date is missing or invalid.
    assert ids == {'S001', 'S002', 'S003', 'S004', 'S005', 'S006', 'S007', 'S008', '7', '0007', 'S009', 'S010'}
    assert warnings == [f'Reference row {row}: invalid scan_date; row ignored' for row in (5, 6, 7, 8, 9)] + [
        'Reference row 14: invalid subject_id; row ignored']


@pytest.mark.parametrize('text, message', [
    ('subject_id;scan_date\nS001;20260725\n', 'must contain'),                # semicolons (Excel in some locales)
    ('Subject_ID,Scan_Date\nS001,20260725\n', 'must contain'),                # header letter case
    ('subject_id,scan_date,,\nS001,20260725,,\n', 'duplicate column names'),  # two empty trailing header cells
    ('subject_id,scan_date\nS001,\nS002,7/25/26\n', 'no valid subject/date pairs'),
    ('', 'must contain'),
], ids=['semicolons', 'header-case', 'two-empty-header-cells', 'no-valid-pairs', 'empty-file'])
def test_roster_that_cannot_be_used_is_refused(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        identity.load_roster(roster_text(tmp_path, text))


def test_one_empty_trailing_header_cell_is_accepted(tmp_path):
    """A spreadsheet export with one empty column at the right (two are refused, above)."""
    ids, pairs, warnings = identity.load_roster(roster_text(tmp_path, 'subject_id,scan_date,\nS001,20260725,\n'))
    assert (ids, pairs, warnings) == ({'S001'}, {('S001', '20260725')}, [])


def test_unclosed_quote_swallows_the_rows_after_it(tmp_path):
    """Pin (plan G-15): the csv module reads everything after an unclosed quote as one cell.

    S003 is lost, and the only sign is one invalid-date warning for row 3.
    """
    roster = roster_text(tmp_path, 'subject_id,scan_date\nS001,20260725\nS002,"2026-07-25\nS003,20260725\n')
    ids, pairs, warnings = identity.load_roster(roster)
    assert (ids, pairs) == ({'S001', 'S002'}, {('S001', '20260725')})
    assert warnings == ['Reference row 3: invalid scan_date; row ignored']


@pytest.mark.parametrize('content', [b'subject_id,scan_date\nS\xe9001,20260725\n', b'subject_id,scan_date\nS001,\n', None],
                         ids=['not-utf-8', 'no-valid-pairs', 'missing-file'])
def test_unusable_roster_exits_2_before_anything_is_written(project, capsys, content):
    roster = project / 'sessions.csv'
    roster.unlink()
    if content is not None:
        roster.write_bytes(content)
    assert call(project, 'prepare') == 2
    assert capsys.readouterr().err.startswith('ERROR: ')
    assert not (project / 'Reports').exists()
