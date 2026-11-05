"""Shared synthetic-data helpers for the test suite. Never reads research archives."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
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


# Crash points for run_with_crash: (kind, rule). A "rename" point kills the
# process right after a Path.rename that matches the rule (source, target).
CRASH_POINTS = {
    # The upload ZIP was renamed into SubjectsSorted; no publish intent yet.
    'zip-moved': ('rename', lambda src, dst: src.suffix.lower() == '.zip'),
    # The stage became .incoming_*; the old destination has not moved aside.
    'staged': ('rename', lambda src, dst: dst.name.startswith('.incoming_')),
    # The old destination became .previous_*; .incoming_* has not replaced it.
    'moved-aside': ('rename', lambda src, dst: dst.name.startswith('.previous_')),
    # .incoming_* became the destination; the result event is not saved yet.
    'swapped': ('rename', lambda src, dst: src.name.startswith('.incoming_')),
    # Reconciliation renamed .previous_* back to the destination; its intent remains.
    'rolling-back': ('rename', lambda src, dst: src.name.startswith('.previous_')),
    # A cross-device publish, killed after two files were copied into .incoming_*.
    'copying': ('copy', 2),
    # The result event's journal is written; its intent is not cleared yet.
    'journaled': ('clear-intents', None),
    # index.json became index.json.bak; the new index is not in place yet.
    'index-rotated': ('replace', lambda src, dst: Path(dst).name == 'index.json.bak'),
}


def run_with_crash(root, phase, point, *extra):
    """Run a command in a child process that is killed at the named crash point.

    Returns the exit code: 137 when the crash point was reached.
    """
    tests = Path(__file__).resolve().parent
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tests.parent / 'src'), str(tests)]))
    code = 'import sys, support; support._crash_child(sys.argv[1], sys.argv[2], sys.argv[3:])'
    args = [sys.executable, '-c', code, point, phase, '--project-root', str(root), '--workers', '1', *map(str, extra)]
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    if result.returncode != 137:
        print(f'crash point {point!r} not reached; child output:\n{result.stdout[-3000:]}\n{result.stderr[-3000:]}')
    return result.returncode


def _crash_child(point, phase, argv):
    """Body of the run_with_crash child: patch the crash point, then run the command."""
    import errno
    import shutil
    from mri_dicom_pipeline import runner
    kind, rule = CRASH_POINTS[point]

    def die():
        os._exit(137)

    original_rename = Path.rename
    if kind == 'rename':
        def rename(self, target):
            result = original_rename(self, target)
            if rule(Path(self), Path(target)):
                die()
            return result
        Path.rename = rename
    elif kind == 'copy':
        def rename(self, target):
            if Path(target).name.startswith('.incoming_'):
                raise OSError(errno.EXDEV, 'Invalid cross-device link (simulated)')
            return original_rename(self, target)
        Path.rename = rename
        original_copytree, copied = shutil.copytree, []

        def copy_then_die(src, dst, *args, **kwargs):
            if len(copied) == rule:
                die()
            copied.append(src)
            return shutil.copy2(src, dst, *args, **kwargs)

        def copytree(src, dst, *args, **kwargs):
            # copytree calls itself for subfolders, passing copy_function positionally.
            if len(args) < 3 and 'copy_function' not in kwargs:
                kwargs['copy_function'] = copy_then_die
            return original_copytree(src, dst, *args, **kwargs)
        shutil.copytree = copytree
    elif kind == 'clear-intents':
        from mri_dicom_pipeline import recovery
        recovery.clear_intents = lambda *args, **kwargs: die()
    elif kind == 'replace':
        original_replace = os.replace

        def replace(src, dst, *args, **kwargs):
            original_replace(src, dst, *args, **kwargs)
            if rule(src, dst):
                die()
        os.replace = replace
    sys.exit(runner.main(phase, argv))
