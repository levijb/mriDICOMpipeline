"""Synthetic integration tests. Never reads or modifies research archives."""
import csv
import gzip
import json
from pathlib import Path
import struct
import sys
import textwrap
import time
import threading
import zipfile
from datetime import date, timedelta

import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid
import pytest

from mri_dicom_pipeline import pipeline as sp


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    with (root / "sessions.csv").open("w", newline="") as f:
        csv.writer(f).writerows([['subject_id', 'scan_date'], ['S001', '7/25/2026'], ['S001', '7/25/2026'],
                                ['S001', '8/1/2026'], ['S002', '7/25/2026'], ['', ''], ['`', '7/25/2026']])
    return root


def dicom_bytes(tmp_path, value=1, study=None, series=None):
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    ds = FileDataset(None, {}, file_meta=meta, preamble=b'\0' * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = MRImageStorage, meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID, ds.SeriesInstanceUID = study or generate_uid(), series or generate_uid()
    ds.PatientName, ds.PatientID = 'FAKE_PRIVATE_NAME', 'FAKE_PRIVATE_ID'
    ds.StudyDate, ds.Modality, ds.SeriesNumber = '20260725', 'MR', 3
    ds.SeriesDescription = 'AX Diffusion_ADC'
    ds.Rows, ds.Columns, ds.BitsAllocated = 2, 2, 16
    ds.BitsStored, ds.HighBit, ds.SamplesPerPixel, ds.PixelRepresentation = 16, 15, 1, 0
    ds.PhotometricInterpretation = 'MONOCHROME2'
    ds.PixelData = struct.pack('<4H', *([value]*4))
    p = tmp_path / (generate_uid() + '.dcm')
    ds.save_as(p, enforce_file_format=True)
    return p.read_bytes()


def archive(root, relative, data):
    p = root / 'DicomUploads' / 'Uploads' / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('exam/random/extensionless', data)
        z.writestr('ddv/viewer.dll', b'not a dicom')
        z.writestr('exam/preview.jpg', b'preview')
    return p


def call(root, phase, *extra):
    return sp.main(phase, ['--project-root', str(root), '--workers', '2', *map(str, extra)])


def state(root):
    return json.loads((root / 'Reports/.state/index.json').read_text())


@pytest.fixture
def converter(tmp_path):
    script = tmp_path / 'fake_converter.py'
    script.write_text(textwrap.dedent('''
        import json,os,pathlib,struct,sys
        args=sys.argv
        out=pathlib.Path(args[args.index('-o')+1])
        h=bytearray(352)
        struct.pack_into('<i',h,0,348)
        struct.pack_into('<8h',h,40,3,2,2,1,1,1,1,1)
        struct.pack_into('<h',h,70,4)
        struct.pack_into('<h',h,72,16)
        struct.pack_into('<f',h,108,352)
        h[344:348]=b'n+1\\0'
        h[148:165]=b'FAKE_PRIVATE_NAME'
        h[228:243]=b'FAKE_PRIVATE_ID'
        stem='series_1.2.3_3_1'
        (out/(stem+'.nii')).write_bytes(h+struct.pack('<4h',1,2,3,4))
        (out/(stem+'.json')).write_text(json.dumps({'SeriesDescription':'AX Diffusion_ADC','PatientName':'FAKE_PRIVATE_NAME','PatientID':'FAKE_PRIVATE_ID','AccessionNumber':'FAKE_PRIVATE_ACCESSION','SeriesNumber':3,'EchoTime':0.03}))
        (out/(stem+'.bval')).write_text('0 1000')
        (out/(stem+'.bvec')).write_text('0 1\\n0 0\\n0 0\\n')
        print('PatientName=FAKE_PRIVATE_NAME')
        print('Conversion diagnostic: done')
        sys.exit(int(os.environ.get('FAKE_CONVERTER_EXIT','0')))
    '''))
    return script


def test_prepare_incremental_and_suffix(project, tmp_path, monkeypatch):
    data = dicom_bytes(tmp_path)
    archive(project, 'batch/S001/S001_20260725.zip', data)
    archive(project, 'S001_20260725_2.zip', data)
    assert call(project, 'prepare') == 0
    s = state(project)
    assert set(s['sessions']) == {'S001/20260725', 'S001/20260725_2'}
    assert (project/'DicomUploads/SubjectsSorted/S001/S001_20260725.zip').exists()
    assert list((project/'DicomExtracted/Subjects/S001/20260725/dicom').glob('*.dcm'))
    assert not list((project/'DicomExtracted').rglob('*.jpg'))
    def forbidden(*args, **kwargs):
        raise AssertionError('Unchanged run attempted a hash/header read')
    monkeypatch.setattr(sp, 'sha256', forbidden)
    monkeypatch.setattr(sp, 'extract_archive', forbidden)
    assert call(project, 'prepare') == 0


def test_duplicates_and_conflicts_are_cached(project, tmp_path, monkeypatch):
    data = dicom_bytes(tmp_path)
    archive(project, 'S001_20260725.zip', data)
    assert call(project, 'prepare') == 0
    duplicate = archive(project, 'new_batch/S001_20260725.zip', data)
    assert call(project, 'prepare') == 0
    assert duplicate.exists()
    repack = project/'DicomUploads/Uploads/repack/S001_20260725.zip'
    repack.parent.mkdir()
    with zipfile.ZipFile(repack, 'w') as z:
        z.writestr('different_wrapper/new_filename', data)
    assert call(project, 'prepare') == 0
    assert repack.exists()
    changed = archive(project, 'conflict/S001_20260725.zip', dicom_bytes(tmp_path, 7))
    assert call(project, 'prepare') == 2
    assert changed.exists()
    monkeypatch.setattr(sp, 'prepare_one', lambda *a: pytest.fail('Cached conflict retried'))
    assert call(project, 'prepare') == 2


def test_dry_run_writes_nothing_and_bad_names_held(project, tmp_path):
    archive(project, 'S001_072526.zip', dicom_bytes(tmp_path))
    before = {p.relative_to(project) for p in project.rglob('*')}
    assert call(project, 'prepare', '--dry-run') == 2
    assert before == {p.relative_to(project) for p in project.rglob('*')}
    assert call(project, 'prepare') == 2
    assert not list((project/'DicomExtracted').glob('*'))


def test_conversion_privacy_companions_and_fast_rerun(project, tmp_path, converter, monkeypatch):
    archive(project, 'S001_20260725_2.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    output = project/'Niftis/Subjects/S001/20260725_2'
    assert len(list(output.rglob('*.nii.gz'))) == 1
    assert len(list(output.rglob('*.bval'))) == 1
    assert len(list(output.rglob('*.bvec'))) == 1
    meta = json.loads(next((output/'ADC').glob('*.json')).read_text())
    assert meta['scan_type'] == 'ADC' and meta['session'] == '20260725_2'
    for p in output.rglob('*'):
        if p.is_file():
            content = gzip.open(p,'rb').read() if p.name.endswith('.nii.gz') else p.read_bytes()
            assert b'FAKE_PRIVATE' not in content
    assert 'FAKE_PRIVATE' not in (project/'Reports/latest_convert.log').read_text()
    monkeypatch.setattr(sp, 'convert_one', lambda *a: pytest.fail('Unchanged conversion ran'))
    monkeypatch.setattr(pydicom, 'dcmread', lambda *a, **k: pytest.fail('Cached conversion read headers'))
    assert call(project, 'convert', '--dcm2niix-bin', 'missing-on-purpose') == 0


def test_partial_outputs_retained_and_retry(project, tmp_path, converter, monkeypatch):
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    monkeypatch.setenv('FAKE_CONVERTER_EXIT', '1')
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert list((project/'Niftis/.pending').rglob('*.nii.gz'))
    assert not (project/'Niftis/Subjects/S001/20260725').exists()
    monkeypatch.delenv('FAKE_CONVERTER_EXIT')
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert call(project, 'convert', '--dcm2niix-bin', converter, '--retry-failed') == 0
    assert state(project)['sessions']['S001/20260725']['conversion']['status'] == 'converted'


def test_deleted_dicom_and_nifti_detected(project, tmp_path, converter):
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    next((project/'DicomExtracted').rglob('*.dcm')).unlink()
    assert call(project, 'prepare') == 0
    assert list((project/'DicomExtracted').rglob('*.dcm'))
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    next((project/'Niftis/Subjects').rglob('*.nii.gz')).unlink()
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0


def test_output_root_override_does_not_reuse_other_root(project, tmp_path, converter):
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 0
    alternate = project/'AlternativeNiftis'
    assert call(project, 'convert', '--output-root', alternate, '--dcm2niix-bin', converter) == 0
    assert len(list(alternate.rglob('*.nii.gz'))) == 1


def test_lock_and_pending_journal_recovery(project):
    with sp.ProjectLock(project/'.mri_dicom_pipeline.lock'):
        with pytest.raises(RuntimeError):
            with sp.ProjectLock(project/'.mri_dicom_pipeline.lock'):
                pass
    s = sp.State(project/'Reports')
    s.checkpoint({'key':'S001/20260725','session':{'status':'failed'}})
    recovered = sp.State(project/'Reports')
    assert recovered.data['sessions']['S001/20260725']['status'] == 'failed'


def test_path_traversal_member_cannot_escape(project, tmp_path):
    data = dicom_bytes(tmp_path)
    p = archive(project, 'S001_20260725.zip', data)
    with zipfile.ZipFile(p, 'a') as z:
        z.writestr('../../escape.dcm', data)
    assert call(project, 'prepare') == 0
    assert not (project/'escape.dcm').exists()
    assert state(project)['sessions']['S001/20260725']['n_dicom'] == 2


def test_failed_archive_not_retried_until_requested(project, monkeypatch):
    p = project/'DicomUploads/Uploads/S001_20260725.zip'
    p.parent.mkdir(parents=True)
    p.write_bytes(b'broken ZIP')
    assert call(project, 'prepare') == 2
    original = sp.prepare_one
    monkeypatch.setattr(sp, 'prepare_one', lambda *a: pytest.fail('Unchanged failure retried'))
    assert call(project, 'prepare') == 2
    monkeypatch.setattr(sp, 'prepare_one', original)
    assert call(project, 'prepare', '--retry-failed') == 2


def test_zero_output_is_not_success(project, tmp_path):
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    converter = tmp_path/'empty_converter.py'
    converter.write_text('raise SystemExit(0)')
    assert call(project, 'convert', '--dcm2niix-bin', converter) == 2
    assert state(project)['sessions']['S001/20260725']['conversion']['status'] == 'partial'


def test_interrupted_preparation_remains_resumable(project, tmp_path):
    p = archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    cfg = sp.configure(sp.config_parser('prepare').parse_args(['--project-root',str(project)]), 'prepare')
    cfg.work.mkdir()
    stop = threading.Event()
    stop.set()
    result = sp.prepare_one((p,'S001','20260725','20260725',sp.signature(p)), None, cfg, stop)
    assert result['source']['outcome'] == 'interrupted'
    assert result['session']['status'] == 'interrupted'
    assert p.exists()
    assert call(project, 'prepare') == 0


def test_sequence_categories_and_truncated_nifti(tmp_path):
    assert sp.classify('AX Diffusion_ADC') == 'ADC'
    assert sp.classify('SAGITTAL T1 VOLUME POST') == 'T1_POST'
    assert sp.classify('AXIAL SWI',image_type='ORIGINAL\\PRIMARY\\M') == 'SWI'
    assert sp.classify('AXIAL SWI',image_type=['ORIGINAL','PRIMARY','P']) == 'SWI_PHASE'
    invalid = tmp_path/'invalid.nii'
    invalid.write_bytes(b'not an image')
    with pytest.raises(ValueError):
        sp.sanitized_nifti(invalid,tmp_path/'invalid.nii.gz')


def test_new_batch_only_processes_new_session(project, tmp_path, monkeypatch):
    archive(project, 'S001_20260725.zip', dicom_bytes(tmp_path))
    assert call(project, 'prepare') == 0
    archive(project, 'S001_20260801.zip', dicom_bytes(tmp_path))
    original = sp.prepare_one
    calls = []
    def record(candidate, *args):
        calls.append(candidate[3])
        return original(candidate, *args)
    monkeypatch.setattr(sp, 'prepare_one', record)
    assert call(project, 'prepare') == 0
    assert calls == ['20260801']


def test_1000_cached_sessions_do_not_read_scan_contents(project, monkeypatch, capsys):
    """Metadata fixtures deliberately contain no readable image payloads.

    This isolates the scale of repeat-run bookkeeping from first-run processing.
    A content read or converter call is a test failure, independent of timing.
    """
    s = sp.State(project/'Reports')
    dates = [(date(2020, 1, 1) + timedelta(days=i)).strftime('%Y%m%d') for i in range(1000)]
    with (project/'sessions.csv').open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['subject_id','scan_date'])
        writer.writerows(['S001', d] for d in dates)
    for d in dates:
        zip_path = project/'DicomUploads/SubjectsSorted/S001'/f'S001_{d}.zip'
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(b'cached archive; must not be opened')
        data_root = project/'DicomExtracted/Subjects/S001'/d
        out_root = project/'Niftis/Subjects/S001'/d
        for root in (data_root, out_root):
            root.mkdir(parents=True)
            (root/'.pipeline_complete.json').write_text('{}')
        def cached_receipt(root):
            return dict(version=sp.VERSION, root=str(root), receipt=str(root/'.pipeline_complete.json'),
                        receipt_signature=sp.signature(root/'.pipeline_complete.json'),
                        directories={str(root):root.stat().st_mtime_ns}, inventory=str(root/'.inventory.json'))
        conv = cached_receipt(out_root)
        conv.update(status='converted', output=str(out_root), n_volumes=20, source_sha256=d)
        s.data['sessions'][f'S001/{d}'] = dict(subject_id='S001', date=d, label=d, status='ready',
            archive=str(zip_path), archive_signature=sp.signature(zip_path), archive_sha256=d,
            content_sha256=d, extraction=cached_receipt(data_root), conversion=conv, n_dicom=3000, n_series=20)
        s.data['sources'][str(zip_path)] = dict(path=str(zip_path), signature=sp.signature(zip_path),
            key=f'S001/{d}', outcome='prepared', against=d, version=sp.VERSION)
    s.flush()
    def forbidden(*a, **k):
        pytest.fail('Cached run attempted to read/process scan contents')
    monkeypatch.setattr(sp, 'sha256', forbidden)
    monkeypatch.setattr(sp, 'prepare_one', forbidden)
    monkeypatch.setattr(sp, 'convert_one', forbidden)
    monkeypatch.setattr(pydicom, 'dcmread', forbidden)
    start = time.perf_counter()
    assert call(project, 'prepare') == 0
    prepare_time = time.perf_counter()-start
    start = time.perf_counter()
    assert call(project, 'convert', '--dcm2niix-bin', 'not-needed-for-cached-work') == 0
    conversion_time = time.perf_counter()-start
    with capsys.disabled():
        print(f'\n1000 cached sessions: prepare={prepare_time:.3f}s convert={conversion_time:.3f}s; zero archive hashes/header reads/converter calls')
