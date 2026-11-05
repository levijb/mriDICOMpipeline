"""Shared synthetic-data helpers for the test suite. Never reads research archives."""
import json
import struct
import zipfile

import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import ExplicitVRLittleEndian, JPEGBaseline8Bit, MRImageStorage, RLELossless, generate_uid

from mri_dicom_pipeline import runner


CSA_NON_IMAGE_STORAGE = '1.3.12.2.1107.5.9.1'


def dicom_bytes(tmp_path, value=1, study=None, series=None, description='AX Diffusion_ADC',
                series_number=3, slice_index=None, rows=2, columns=2, pixel_count=None):
    """One synthetic MR Image Storage object.

    With ``slice_index`` set, the object also carries the slice geometry and
    instance number that a real converter needs to stack slices into a volume.
    ``pixel_count`` writes fewer pixels than ``rows`` x ``columns`` declare.
    """
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    ds = FileDataset(None, {}, file_meta=meta, preamble=b'\0' * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = MRImageStorage, meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID, ds.SeriesInstanceUID = study or generate_uid(), series or generate_uid()
    ds.PatientName, ds.PatientID = 'FAKE_PRIVATE_NAME', 'FAKE_PRIVATE_ID'
    ds.StudyDate, ds.Modality, ds.SeriesNumber = '20260725', 'MR', series_number
    ds.SeriesDescription = description
    ds.Rows, ds.Columns, ds.BitsAllocated = rows, columns, 16
    ds.BitsStored, ds.HighBit, ds.SamplesPerPixel, ds.PixelRepresentation = 16, 15, 1, 0
    ds.PhotometricInterpretation = 'MONOCHROME2'
    if slice_index is not None:
        ds.InstanceNumber = slice_index + 1
        ds.ImagePositionPatient = [0.0, 0.0, float(slice_index)]
        ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        ds.PixelSpacing = [1.0, 1.0]
        ds.SliceThickness = 1.0
    count = rows * columns if pixel_count is None else pixel_count
    ds.PixelData = struct.pack(f'<{count}H', *([value]*count))
    p = tmp_path / (generate_uid() + '.dcm')
    ds.save_as(p, enforce_file_format=True)
    return p.read_bytes()


def mr_series(tmp_path, slices=3, study=None, **kwargs):
    """A stackable synthetic MR series: (SeriesInstanceUID, list of object bytes)."""
    series = generate_uid()
    return series, [dicom_bytes(tmp_path, value=i + 1, study=study, series=series, slice_index=i, **kwargs)
                    for i in range(slices)]


UNCONVERTIBLE_SYNTAXES = {
    # dcm2niix refuses 16-bit JPEG Baseline before decoding anything.
    'jpeg-baseline-16bit': JPEGBaseline8Bit,
    # The pixel data is not valid RLE, so decoding fails.
    'undecodable-rle': RLELossless,
}


def unconvertible_series(tmp_path, kind, slices=3, study=None, **kwargs):
    """A series that preparation accepts but dcm2niix cannot convert.

    The objects declare a compressed transfer syntax and carry pixel data that
    the syntax cannot decode. Preparation skips its size check for compressed
    syntaxes, so the series reaches the converter.
    """
    series, objects = mr_series(tmp_path, slices=slices, study=study, **kwargs)
    result = []
    for data in objects:
        path = tmp_path / (generate_uid() + '.dcm')
        path.write_bytes(data)
        ds = pydicom.dcmread(path)
        ds.file_meta.TransferSyntaxUID = UNCONVERTIBLE_SYNTAXES[kind]
        ds.PixelData = encapsulate([b'\xff\xd8 not decodable pixel data \xff\xd9'])
        ds['PixelData'].VR = 'OB'
        ds.save_as(path, enforce_file_format=True)
        result.append(path.read_bytes())
    return series, result


def csa_non_image_bytes(tmp_path, study=None, series=None):
    """A synthetic Siemens CSA Non-Image object: a SOP class and series, no pixel data."""
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = CSA_NON_IMAGE_STORAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    ds = FileDataset(None, {}, file_meta=meta, preamble=b'\0' * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = CSA_NON_IMAGE_STORAGE, meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID, ds.SeriesInstanceUID = study or generate_uid(), series or generate_uid()
    ds.StudyDate, ds.Modality, ds.SeriesNumber = '20260725', 'MR', 99
    ds.SeriesDescription = 'PhoenixZIPReport'
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


def archive_objects(root, relative, objects):
    """A ZIP in Uploads/ holding each object as its own extensionless member."""
    p = root / 'DicomUploads' / 'Uploads' / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, 'w', zipfile.ZIP_DEFLATED) as z:
        for i, data in enumerate(objects, 1):
            z.writestr(f'exam/{i:05d}', data)
    return p


def call(root, phase, *extra):
    return runner.main(phase, ['--project-root', str(root), '--workers', '2', *map(str, extra)])


def state(root):
    return json.loads((root / 'Reports/.state/index.json').read_text())
