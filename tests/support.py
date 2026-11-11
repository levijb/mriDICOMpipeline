"""Shared synthetic-data helpers for the test suite. Never reads research archives."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import zipfile
import zlib

import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.uid import ExplicitVRLittleEndian, JPEGBaseline8Bit, MRImageStorage, RLELossless, generate_uid

from mri_dicom_pipeline import runner


CSA_NON_IMAGE_STORAGE = '1.3.12.2.1107.5.9.1'

# Classes other than MR image storage that scanner exports carry: name -> (SOP class UID, Modality).
OTHER_SOP_CLASSES = {
    'secondary capture': ('1.2.840.10008.5.1.4.1.1.7', 'OT'),
    'basic text SR': ('1.2.840.10008.5.1.4.1.1.88.11', 'SR'),
    'grayscale presentation state': ('1.2.840.10008.5.1.4.1.1.11.1', 'PR'),
    'key object selection': ('1.2.840.10008.5.1.4.1.1.88.59', 'KO'),
    'raw data': ('1.2.840.10008.5.1.4.1.1.66', 'MR'),
    'MR spectroscopy': ('1.2.840.10008.5.1.4.1.1.4.2', 'MR'),
    'private (GE)': ('1.2.840.113619.4.26', 'MR'),
}


def dicom_bytes(tmp_path, value=1, study=None, series=None, description='AX Diffusion_ADC',
                series_number=3, slice_index=None, rows=2, columns=2, pixel_count=None, sop_uid=None,
                overlay=False, sop_class=MRImageStorage, fields=None):
    """One synthetic image object, MR Image Storage unless ``sop_class`` says otherwise.

    With ``slice_index`` set, the object also carries the slice geometry and
    instance number that a real converter needs to stack slices into a volume.
    ``pixel_count`` writes fewer pixels than ``rows`` x ``columns`` declare.
    ``overlay`` adds graphics overlay planes marking the first pixel: True or 1
    for one plane (group 6000), 2 for a second one (group 6002), and so on.
    ``fields`` sets further elements by DICOM keyword, after the defaults; a
    value of None removes the element.
    """
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = sop_uid or generate_uid()
    ds = FileDataset(None, {}, file_meta=meta, preamble=b'\0' * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = sop_class, meta.MediaStorageSOPInstanceUID
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
    for plane in range(int(overlay)):
        bits = bytearray((rows * columns + 15) // 16 * 2)  # one bit per pixel, padded to a word
        bits[0] = 1
        for element, vr, data in ((0x0010, 'US', rows), (0x0011, 'US', columns), (0x0040, 'CS', 'G'),
                                  (0x0050, 'SS', [1, 1]), (0x0100, 'US', 1), (0x0102, 'US', 0),
                                  (0x3000, 'OW', bytes(bits))):
            ds.add_new((0x6000 + 2 * plane, element), vr, data)
    for keyword, field_value in (fields or {}).items():
        if field_value is None:
            if keyword in ds:
                delattr(ds, keyword)
        else:
            setattr(ds, keyword, field_value)
    count = rows * columns if pixel_count is None else pixel_count
    ds.PixelData = struct.pack(f'<{count}H', *([value]*count))
    p = tmp_path / (generate_uid() + '.dcm')
    ds.save_as(p, enforce_file_format=True)
    return p.read_bytes()


def mr_series(tmp_path, slices=3, study=None, positions=None, fields=None, **kwargs):
    """A stackable synthetic MR series in one study: (SeriesInstanceUID, list of object bytes).

    Pass the same ``study`` to series that share a ZIP, or preparation holds
    the ZIP as a multi-study export (Q20). ``positions`` gives each slice's
    position along the slice axis (default 0, 1, 2, ...), for uneven spacing.
    """
    series, study = generate_uid(), study or generate_uid()
    positions = range(slices) if positions is None else positions
    return series, [dicom_bytes(tmp_path, value=i + 1, study=study, series=series, slice_index=i,
                                fields=dict(fields or {}, ImagePositionPatient=[0.0, 0.0, float(z)]), **kwargs)
                    for i, z in enumerate(positions)]


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


def non_image_bytes(tmp_path, sop_class, study=None, series=None, modality='MR'):
    """A synthetic object of the given SOP class in its own series, with no pixel data."""
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = generate_uid()
    ds = FileDataset(None, {}, file_meta=meta, preamble=b'\0' * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = sop_class, meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID, ds.SeriesInstanceUID = study or generate_uid(), series or generate_uid()
    ds.StudyDate, ds.Modality, ds.SeriesNumber = '20260725', modality, 99
    ds.SeriesDescription = 'PhoenixZIPReport'
    p = tmp_path / (generate_uid() + '.dcm')
    ds.save_as(p, enforce_file_format=True)
    return p.read_bytes()


def csa_non_image_bytes(tmp_path, study=None, series=None):
    """A synthetic Siemens CSA Non-Image object: a SOP class and series, no pixel data."""
    return non_image_bytes(tmp_path, CSA_NON_IMAGE_STORAGE, study, series)


def other_class_objects(tmp_path, study):
    """One object of each class in OTHER_SOP_CLASSES, each in its own series of the study.

    Only the secondary capture has pixels (a screen save, with no slice geometry).
    """
    objects = []
    for name, (sop_class, modality) in OTHER_SOP_CLASSES.items():
        if name == 'secondary capture':
            objects.append(dicom_bytes(tmp_path, study=study, sop_class=sop_class, description='Screen Save',
                                       series_number=90, fields={'Modality': modality}))
        else:
            objects.append(non_image_bytes(tmp_path, sop_class, study=study, modality=modality))
    return objects


def write_nifti(path):
    """A minimal valid single-file NIfTI-1 volume (2 x 2 x 1, int16), as the fake converter writes it."""
    h = bytearray(352)
    struct.pack_into('<i', h, 0, 348)
    struct.pack_into('<8h', h, 40, 3, 2, 2, 1, 1, 1, 1, 1)
    struct.pack_into('<h', h, 70, 4)
    struct.pack_into('<h', h, 72, 16)
    struct.pack_into('<f', h, 108, 352)
    h[344:348] = b'n+1\0'
    Path(path).write_bytes(bytes(h) + struct.pack('<4h', 1, 2, 3, 4))
    return Path(path)


def archive_with_damaged_member(root, relative, name, data, compression=zipfile.ZIP_DEFLATED):
    """A ZIP in Uploads/ whose one member has its last byte changed but still carries the CRC of ``data``.

    The archive is structurally valid, so only a CRC check finds the damage.
    """
    damaged = data[:-1] + bytes([data[-1] ^ 0xFF])
    p = root / 'DicomUploads' / 'Uploads' / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, 'w', compression) as z:
        z.writestr(name, damaged)
    raw = bytearray(p.read_bytes())
    good, bad = (struct.pack('<I', zlib.crc32(content)) for content in (data, damaged))
    # The CRC sits 14 bytes into the local file header and 16 bytes into the central directory entry.
    for at in (raw.index(b'PK\x03\x04') + 14, raw.rindex(b'PK\x01\x02') + 16):
        assert raw[at:at + 4] == bad
        raw[at:at + 4] = good
    p.write_bytes(bytes(raw))
    return p


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


FAKE_CONVERTER = '''
"""Fake dcm2niix for tests: one volume per input image series, named by its SeriesInstanceUID.

Switches (environment variables):
  FAKE_CONVERTER_EXIT     exit code (default 0)
  FAKE_CONVERTER_SKIP     comma-separated SeriesInstanceUIDs to leave out
  FAKE_CONVERTER_PRINTED  file to create after printing the identifying diagnostic line
  FAKE_CONVERTER_SLEEP    seconds to sleep before exiting
  FAKE_CONVERTER_CHILD    file to write the PID of a sleeping child process into
  FAKE_CONVERTER_KEEP     folder to copy the raw .nii output into
  FAKE_CONVERTER_OVERLAY  also write <volume>_ROI1.nii, a mask with no sidecar, as dcm2niix does
                          for a DICOM overlay plane
"""
import json, os, pathlib, shutil, struct, subprocess, sys, time
import pydicom

args = sys.argv
out, source = pathlib.Path(args[args.index('-o') + 1]), pathlib.Path(args[-1])
skip = set(filter(None, os.environ.get('FAKE_CONVERTER_SKIP', '').split(',')))
series = {}
for path in sorted(source.iterdir()):
    ds = pydicom.dcmread(path, stop_before_pixels=True, force=True)
    if 'Rows' in ds and str(ds.get('SeriesInstanceUID', '')) not in skip:
        series.setdefault(str(ds.SeriesInstanceUID), ds)
for uid, ds in series.items():
    stem = 'series_%s_%s_1' % (uid, ds.get('SeriesNumber', 1))
    h = bytearray(352)
    struct.pack_into('<i', h, 0, 348)
    struct.pack_into('<8h', h, 40, 3, 2, 2, 1, 1, 1, 1, 1)
    struct.pack_into('<h', h, 70, 4)
    struct.pack_into('<h', h, 72, 16)
    struct.pack_into('<f', h, 108, 352)
    h[344:348] = b'n+1\\0'
    h[148:165] = b'FAKE_PRIVATE_NAME'
    h[228:243] = b'FAKE_PRIVATE_ID'
    (out / (stem + '.nii')).write_bytes(bytes(h) + struct.pack('<4h', 1, 2, 3, 4))
    if os.environ.get('FAKE_CONVERTER_OVERLAY'):
        struct.pack_into('<hh', h, 70, 2, 8)  # uint8
        (out / (stem + '_ROI1.nii')).write_bytes(bytes(h) + bytes([1, 0, 0, 0]))
    sidecar = {'SeriesDescription': str(ds.get('SeriesDescription', '')), 'PatientName': 'FAKE_PRIVATE_NAME',
               'PatientID': 'FAKE_PRIVATE_ID', 'AccessionNumber': 'FAKE_PRIVATE_ACCESSION',
               'SeriesNumber': int(ds.get('SeriesNumber', 1)), 'EchoTime': 0.03,
               'ConversionSoftware': 'fake', 'ConversionSoftwareVersion': 'fake-1.0'}
    if 'ImageType' in ds:
        sidecar['ImageType'] = list(ds.ImageType)
    (out / (stem + '.json')).write_text(json.dumps(sidecar))
    (out / (stem + '.bval')).write_text('0 1000')
    (out / (stem + '.bvec')).write_text('0 1\\n0 0\\n0 0\\n')
keep = os.environ.get('FAKE_CONVERTER_KEEP')
if keep:
    pathlib.Path(keep).mkdir(parents=True, exist_ok=True)
    for path in out.glob('*.nii'):
        shutil.copyfile(path, pathlib.Path(keep) / path.name)
print('PatientName=FAKE_PRIVATE_NAME', flush=True)
print('Conversion diagnostic: done', flush=True)
if os.environ.get('FAKE_CONVERTER_PRINTED'):
    pathlib.Path(os.environ['FAKE_CONVERTER_PRINTED']).write_text('')
if os.environ.get('FAKE_CONVERTER_CHILD'):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    pathlib.Path(os.environ['FAKE_CONVERTER_CHILD']).write_text(str(child.pid))
time.sleep(float(os.environ.get('FAKE_CONVERTER_SLEEP', '0')))
sys.exit(int(os.environ.get('FAKE_CONVERTER_EXIT', '0')))
'''


def fake_converter_script(path):
    """Write the fake dcm2niix (FAKE_CONVERTER) to path and return the path."""
    Path(path).write_text(FAKE_CONVERTER)
    return Path(path)


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


def child_env(**extra):
    """Environment for a child Python process: the package and test helpers first on PYTHONPATH."""
    tests = Path(__file__).resolve().parent
    paths = [str(tests.parent / 'src'), str(tests)] + [p for p in os.environ.get('PYTHONPATH', '').split(os.pathsep) if p]
    return dict(os.environ, PYTHONPATH=os.pathsep.join(paths), **extra)


def run_with_crash(root, phase, point, *extra):
    """Run a command in a child process that is killed at the named crash point.

    Returns the exit code: 137 when the crash point was reached.
    """
    env = child_env()
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
