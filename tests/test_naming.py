"""Naming unnamed upload ZIPs from their subject folder and StudyDate (owner request, 2026-11-18)."""
import csv
import json

from pydicom.uid import generate_uid
import pytest

from mri_dicom_pipeline import naming
from support import archive_objects, call, mr_series, state

# The project fixture's roster: S001 on 2026-07-25 and 2026-08-01, S002 on 2026-07-25.


def exam(tmp_path, date='20260725', time='090000', study=None):
    return mr_series(tmp_path, study=study or generate_uid(), fields={'StudyDate': date, 'StudyTime': time})[1]


def upload(project, relative, objects):
    return archive_objects(project, relative, objects)


def renames(project):
    path = project / 'Reports' / naming.NAME_LOG
    with path.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def issues(project):
    return state(project)['issues']


def sorted_zip(project, name):
    return project / 'DicomUploads/SubjectsSorted' / name.split('_')[0] / name


def test_unnamed_zip_gets_its_name_from_folder_and_study_date(project, tmp_path):
    source = upload(project, '11_133_upload/S001/x7f3k.zip', exam(tmp_path))
    assert call(project, 'prepare') == 0
    assert not source.exists() and sorted_zip(project, 'S001_20260725.zip').exists()
    assert state(project)['sessions']['S001/20260725']['status'] == 'ready'
    [row] = renames(project)
    assert (row['old_path'], row['new_path']) == ('Uploads/11_133_upload/S001/x7f3k.zip',
                                                  'Uploads/11_133_upload/S001/S001_20260725.zip')
    assert (row['study_date'], row['listed_date'], row['note']) == ('20260725', '20260725', '')
    inventory = json.loads((project / 'DicomExtracted/Subjects/S001/20260725/.inventory.json').read_text())
    assert len(inventory['study_keys']) == 1


def test_study_date_near_a_listed_date_takes_the_listed_date(project, tmp_path):
    upload(project, 'batch/S001/a.zip', exam(tmp_path, date='20260727'))
    assert call(project, 'prepare') == 0
    assert sorted_zip(project, 'S001_20260725.zip').exists()
    assert '2 days from the listed date' in renames(project)[0]['note']
    assert 'date:S001/20260725' in issues(project)  # the usual nonblocking warning


def test_unlisted_study_date_is_held_until_allowed(project, tmp_path):
    source = upload(project, 'batch/S001/a.zip', exam(tmp_path, date='20260710'))
    assert call(project, 'prepare') == 2
    assert source.exists()
    reason = issues(project)['prepare:' + str(source)]['reason']
    assert 'not listed' in reason and '15 days' in reason
    assert call(project, 'prepare', '--allow-unlisted-dates') == 0
    assert sorted_zip(project, 'S001_20260710.zip').exists()
    assert 'prepare:' + str(source) not in issues(project)


def test_study_date_close_to_two_listed_dates_is_held(project, tmp_path):
    source = upload(project, 'batch/S001/a.zip', exam(tmp_path, date='20260728'))
    assert call(project, 'prepare', '--date-tolerance', 4) == 2  # 3 days from 07-25 and 4 from 08-01
    assert source.exists() and 'within 4 days of 2 listed dates' in issues(project)['prepare:' + str(source)]['reason']
    assert call(project, 'prepare') == 2  # neither is within the default 2 days
    assert 'not listed' in issues(project)['prepare:' + str(source)]['reason']


def test_folder_that_is_not_a_subject_is_held(project, tmp_path):
    source = upload(project, 'batch/S999/a.zip', exam(tmp_path))
    loose = upload(project, 'b.zip', exam(tmp_path))
    assert call(project, 'prepare') == 2
    assert source.exists() and 'not a subject ID' in issues(project)['prepare:' + str(source)]['reason']
    assert loose.exists() and 'ID_YYYYMMDD' in issues(project)['prepare:' + str(loose)]['reason']


def test_second_session_on_the_same_day_gets_the_next_suffix(project, tmp_path, capsys):
    upload(project, 'batch/S001/late.zip', exam(tmp_path, time='140000'))
    upload(project, 'batch/S001/early.zip', exam(tmp_path, time='090000'))
    assert call(project, 'prepare') == 0
    sessions = state(project)['sessions']
    assert sessions['S001/20260725']['status'] == sessions['S001/20260725_2']['status'] == 'ready'
    by_old = {r['old_path'].rsplit('/', 1)[1]: r for r in renames(project)}
    assert by_old['early.zip']['session'] == '20260725' and by_old['late.zip']['session'] == '20260725_2'
    assert 'second session' in by_old['late.zip']['note']
    assert 'second session' in capsys.readouterr().out


def test_later_upload_of_a_new_study_on_a_prepared_date_gets_the_next_suffix(project, tmp_path):
    upload(project, 'batch1/S001/a.zip', exam(tmp_path))
    assert call(project, 'prepare') == 0
    upload(project, 'batch2/S001/b.zip', exam(tmp_path, time='150000'))
    assert call(project, 'prepare') == 0
    assert sorted_zip(project, 'S001_20260725_2.zip').exists()


def test_re_export_of_a_prepared_study_is_named_alike_and_found_duplicate(project, tmp_path):
    objects = exam(tmp_path)
    upload(project, 'batch1/S001/a.zip', objects)
    assert call(project, 'prepare') == 0
    again = upload(project, 'batch2/S001/b.zip', objects)
    assert call(project, 'prepare') == 0
    named = again.with_name('S001_20260725.zip')
    assert named.exists() and 'S001/20260725_2' not in state(project)['sessions']
    assert 'same study as session 20260725' in renames(project)[-1]['note']


def test_dry_run_renames_nothing(project, tmp_path, capsys):
    source = upload(project, 'batch/S001/a.zip', exam(tmp_path))
    assert call(project, 'prepare', '--dry-run') == 0
    assert source.exists() and 'WOULD RENAME' in capsys.readouterr().out
    assert not (project / 'Reports' / naming.NAME_LOG).exists()


def test_dry_run_ends_with_a_summary_of_held_zips_and_names_to_check(project, tmp_path, capsys):
    upload(project, 'S002_20260725.zip', exam(tmp_path))                     # already named
    upload(project, 'batch/S001/a.zip', exam(tmp_path))                      # exact date
    upload(project, 'batch/S001/b.zip', exam(tmp_path, date='20260731'))     # 1 day from 08-01
    held = upload(project, 'batch/S002/c.zip', exam(tmp_path, date='20260710'))
    assert call(project, 'prepare', '--dry-run') == 2
    out = capsys.readouterr().out
    assert '(plus 2 unnamed ZIPs once named)' in out
    summary = out[out.index('DRY RUN SUMMARY'):]
    assert '2  unnamed ZIPs would be named and prepared' in summary
    assert '1  named ZIPs would be prepared' in summary and '1  HELD' in summary
    assert f'S002  {held}' in summary
    assert ('StudyDate 20260710 is not listed for S002 in the reference CSV; the nearest listed date is 20260725 '
            '(15 days away)') in summary
    assert 'CHECK THESE NAMES (1:' in summary
    assert '-> S001_20260801.zip: StudyDate 20260731 is 1 day from the listed date 20260801' in summary


def test_held_zip_headers_are_read_once(project, tmp_path, monkeypatch):
    upload(project, 'batch/S001/a.zip', exam(tmp_path, date='20260710'))
    assert call(project, 'prepare') == 2
    monkeypatch.setattr(naming, 'zip_facts', lambda path: pytest.fail('headers read again for an unchanged ZIP'))
    assert call(project, 'prepare') == 2


def test_resolve_date_table():
    listed = {'20260725', '20260801'}
    assert naming.resolve_date(['20260725'], listed, False) == ('20260725', '')
    assert naming.resolve_date(['20260723'], listed, False)[0] == '20260725'  # 2 days: the default tolerance
    assert naming.resolve_date(['20260722'], listed, False)[0] is None
    assert naming.resolve_date(['20260721'], listed, False, tolerance=4)[0] == '20260725'
    assert naming.resolve_date(['20260729'], listed, False, tolerance=4)[0] is None  # 4 from 07-25 and 3 from 08-01
    assert naming.resolve_date(['20260726'], listed, False, tolerance=0)[0] is None
    assert naming.resolve_date(['20260720'], listed, True) == ('20260720', 'StudyDate not in the reference CSV '
                                                                           '(--allow-unlisted-dates)')
    assert naming.resolve_date([], listed, False)[0] is None
    assert naming.resolve_date(['20260725', '20260726'], listed, False)[0] is None
    assert naming.resolve_date(['20261340'], listed, False)[0] is None
