"""Shared synthetic-data helpers for the test suite. Never reads research archives."""
import json
import struct
import zipfile

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid

from mri_dicom_pipeline import runner


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
    return runner.main(phase, ['--project-root', str(root), '--workers', '2', *map(str, extra)])


def state(root):
    return json.loads((root / 'Reports/.state/index.json').read_text())
