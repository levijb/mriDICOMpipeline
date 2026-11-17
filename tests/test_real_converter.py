"""Smoke tests against the real dcm2niix (Q9; the P1 and Q19 hypotheses).

They run only when MRI_PIPELINE_DCM2NIIX names a dcm2niix executable, and they
use synthetic DICOM objects generated with pydicom, never study data. Each test
prints the dcm2niix version and the exit code it observed, so a CI log records
the evidence. Step 1 of the production-hardening plan checks dcm2niix's own
behavior; step 7 adds the session-level assertions.
"""
import gzip
import io
import json
import struct
import subprocess

import pydicom
from pydicom.uid import generate_uid
import pytest

from mri_dicom_pipeline.convert import CONVERTER_OPTIONS as CONVERTER_ARGS  # what convert_one passes before -o
from support import (CSA_NON_IMAGE_STORAGE, OTHER_SOP_CLASSES, UNCONVERTIBLE_SYNTAXES, archive_objects, call,
                     csa_non_image_bytes, dicom_bytes, mr_series, non_image_bytes, other_class_objects, state,
                     unconvertible_series)


def run_converter(binary, objects, tmp_path):
    """Run dcm2niix on the objects as the pipeline would; return the result and output folder."""
    source, output = tmp_path / 'dicom', tmp_path / 'raw'
    source.mkdir()
    output.mkdir()
    for i, data in enumerate(objects, 1):
        (source / f'{i:08d}.dcm').write_bytes(data)
    result = subprocess.run([binary, *CONVERTER_ARGS, '-o', str(output), str(source)],
                            capture_output=True, text=True)
    return result, output


def converted_uids(output, objects):
    """SeriesInstanceUIDs of the NIfTI volumes; a name series_<uid>_... carries the UID of the volume's first image."""
    series_of = {}
    for data in objects:
        ds = pydicom.dcmread(io.BytesIO(data), stop_before_pixels=True, force=True)
        series_of[str(ds.SOPInstanceUID)] = str(ds.SeriesInstanceUID)
    return {series_of.get(p.name.split('_', 2)[1]) for p in output.glob('series_*.nii')}


def record(capsys, binary, case, returncode):
    version = subprocess.run([binary, '-v'], capture_output=True, text=True).stdout.strip().splitlines()[-1]
    with capsys.disabled():
        print(f'\n[dcm2niix {version}] {case}: exit code {returncode}')


def test_real_converter_names_outputs_by_series_uid(project, tmp_path, real_dcm2niix):
    """Output names carry their first image's UID, which maps to its series, so output keys equal inventory keys (Q6)."""
    study = generate_uid()
    _, first = mr_series(tmp_path, study=study, description='T1 MPRAGE', series_number=2)
    _, second = mr_series(tmp_path, study=study, description='AX FLAIR', series_number=4)
    archive_objects(project, 'S001_20260725.zip', first + second)
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', real_dcm2niix) == 0
    inventory = json.loads((project / 'DicomExtracted/Subjects/S001/20260725/.inventory.json').read_text())
    sidecars = [p for p in (project / 'Niftis/Subjects/S001/20260725').rglob('*.json') if not p.name.startswith('.')]
    output_keys = {json.loads(p.read_text())['source_series_key'] for p in sidecars}
    assert output_keys == {row['series_key'] for row in inventory['series']}
    assert len(output_keys) == 2


def session_conversion(project):
    return state(project)['sessions']['S001/20260725']['conversion']


def test_real_converter_with_non_image_object_converts(project, tmp_path, real_dcm2niix, capsys):
    """Q19 hypothesis: a non-image object in the input does not make dcm2niix exit nonzero.

    Session level (step 7): the pipeline converts the session and excuses the
    non-image series.
    """
    study = generate_uid()
    uid, image = mr_series(tmp_path, study=study, description='T1 MPRAGE', series_number=2)
    non_image = csa_non_image_bytes(tmp_path, study=study)
    result, output = run_converter(real_dcm2niix, image + [non_image], tmp_path)
    record(capsys, real_dcm2niix, 'image series plus a CSA Non-Image object', result.returncode)
    assert result.returncode == 0, ('dcm2niix exited nonzero with a non-image object present; '
                                    'Q19 must be reopened\n' + result.stdout + result.stderr)
    assert converted_uids(output, image + [non_image]) == {uid}
    archive_objects(project, 'S001_20260725.zip', image + [non_image])
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', real_dcm2niix) == 0
    conversion = session_conversion(project)
    assert conversion['status'] == 'converted' and len(conversion['coverage']['excused']) == 1


@pytest.mark.parametrize('kind', sorted(UNCONVERTIBLE_SYNTAXES))
def test_real_converter_unconvertible_series_makes_session_partial(project, tmp_path, real_dcm2niix, capsys, kind):
    """P1 evidence: dcm2niix converts the good series and skips the one it cannot decode.

    The exit code is recorded, not asserted. With dcm2niix v1.0.20261029 it is
    0 for 16-bit JPEG Baseline (a skipped series with a success code, as P1
    describes) and 8 ("converted some but not all") for undecodable RLE.
    Session level (step 7): either way the session is partial, not converted.
    """
    study = generate_uid()
    good, image = mr_series(tmp_path, study=study, description='T1 MPRAGE', series_number=2)
    _, bad = unconvertible_series(tmp_path, kind, study=study, description='AX FLAIR', series_number=4)
    result, output = run_converter(real_dcm2niix, image + bad, tmp_path)
    record(capsys, real_dcm2niix, f'good series plus an unconvertible series ({kind})', result.returncode)
    assert converted_uids(output, image + bad) == {good}
    archive_objects(project, 'S001_20260725.zip', image + bad)
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', real_dcm2niix) == 2
    conversion = session_conversion(project)
    assert conversion['status'] == 'partial' and len(conversion['coverage']['missing']) == 1


def test_real_converter_output_byte_identical(project, tmp_path, real_dcm2niix):
    """Invariant 8: the tested dcm2niix writes no NIfTI header extensions.

    Session level (step 7, Q10): the published .nii.gz decompresses to the
    bytes dcm2niix writes for the same input.
    """
    _, image = mr_series(tmp_path, description='T1 MPRAGE', series_number=2)
    result, output = run_converter(real_dcm2niix, image, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    volumes = sorted(output.glob('*.nii'))
    assert volumes
    for path in volumes:
        header = path.read_bytes()[:352]
        assert struct.unpack('<i', header[:4])[0] == 348, f'{path.name} is not a little-endian NIfTI-1 file'
        assert header[348] == 0, f'{path.name} carries a NIfTI header extension'
    archive_objects(project, 'S001_20260725.zip', image)
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', real_dcm2niix) == 0
    [published] = (project / 'Niftis/Subjects/S001/20260725').rglob('*.nii.gz')
    assert gzip.decompress(published.read_bytes()) == volumes[0].read_bytes()


def test_real_converter_overlay_published_with_its_volume(project, tmp_path, real_dcm2niix, capsys):
    """dcm2niix saves a DICOM overlay plane as <volume>_ROI1.nii, with no sidecar of its own.

    Seen in production (2026-11-10). The pipeline publishes the mask next to
    its volume, and the session converts.
    """
    _, image = mr_series(tmp_path, description='AX FLAIR', series_number=6, overlay=True)
    result, output = run_converter(real_dcm2niix, image, tmp_path)
    record(capsys, real_dcm2niix, 'series with an overlay plane', result.returncode)
    [volume] = [p for p in output.glob('*.nii') if '_ROI' not in p.name]
    assert sorted(p.name for p in output.iterdir() if '_ROI' in p.name) == [volume.stem + '_ROI1.nii']
    archive_objects(project, 'S001_20260725.zip', image)
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', real_dcm2niix) == 0
    [mask] = (project / 'Niftis/Subjects/S001/20260725').rglob('*_ROI1.nii.gz')
    assert gzip.decompress(mask.read_bytes()) == (output / (volume.stem + '_ROI1.nii')).read_bytes()
    assert session_conversion(project)['n_volumes'] == 1


# ---- Output names beyond one volume per series (plan D-04, D-05, D-07) ---------------

def output_names(output):
    """The converter's output file names, with the series UID shortened so they can be printed."""
    return sorted(p.name.replace(p.name.split('_')[1], 'UID') for p in output.iterdir())


def test_real_converter_two_overlay_planes(project, tmp_path, real_dcm2niix, capsys):
    """README: an overlay plane's mask is published as _ROI1, and _ROI2 and so on for further planes."""
    _, image = mr_series(tmp_path, description='AX FLAIR', series_number=6, overlay=2)
    result, output = run_converter(real_dcm2niix, image, tmp_path)
    record(capsys, real_dcm2niix, f'series with two overlay planes {output_names(output)}', result.returncode)
    [volume] = [p for p in output.glob('*.nii') if '_ROI' not in p.name]
    assert sorted(p.name for p in output.iterdir() if '_ROI' in p.name) == [volume.stem + '_ROI1.nii',
                                                                             volume.stem + '_ROI2.nii']
    archive_objects(project, 'S001_20260725.zip', image)
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', real_dcm2niix) == 0
    for plane in ('ROI1', 'ROI2'):
        [mask] = (project / 'Niftis/Subjects/S001/20260725').rglob(f'*_{plane}.nii.gz')
        assert gzip.decompress(mask.read_bytes()) == (output / f'{volume.stem}_{plane}.nii').read_bytes()
    assert session_conversion(project)['n_volumes'] == 1


SPLIT_SERIES = {
    # Each entry: the element values that differ between the parts of one series.
    'two-echoes': [{'EchoTime': 5.0, 'EchoNumbers': 1}, {'EchoTime': 10.0, 'EchoNumbers': 2}],
    'magnitude-and-phase': [{'ImageType': ['ORIGINAL', 'PRIMARY', 'M', 'ND']},
                            {'ImageType': ['ORIGINAL', 'PRIMARY', 'P', 'ND']}],
    'three-plane-localizer': [{'ImageOrientationPatient': [1, 0, 0, 0, 1, 0], 'axis': 2},
                              {'ImageOrientationPatient': [1, 0, 0, 0, 0, -1], 'axis': 1},
                              {'ImageOrientationPatient': [0, 1, 0, 0, 0, -1], 'axis': 0}],
}


@pytest.mark.parametrize('case', sorted(SPLIT_SERIES))
def test_real_converter_series_split_into_several_volumes(project, tmp_path, real_dcm2niix, capsys, case):
    """One series that dcm2niix saves as several volumes: echoes, magnitude and phase, or orientations.

    Each output has its own sidecar (names end in _1 and _2, in _ph, or in
    _i00001 and so on), so the session converts and every volume carries the
    one series key.
    """
    series, study, objects = generate_uid(), generate_uid(), []
    for part, fields in enumerate(SPLIT_SERIES[case]):
        fields = dict(fields)
        axis = fields.pop('axis', 2)
        for i in range(3):
            position = [float(i) if n == axis else 0.0 for n in range(3)]
            objects.append(dicom_bytes(tmp_path, value=3 * part + i + 1, study=study, series=series, slice_index=i,
                                       description='AX T2* GRE', series_number=7,
                                       fields=dict(fields, InstanceNumber=3 * part + i + 1,
                                                   ImagePositionPatient=position)))
    result, output = run_converter(real_dcm2niix, objects, tmp_path)
    record(capsys, real_dcm2niix, f'{case} in one series {output_names(output)}', result.returncode)
    parts = len(SPLIT_SERIES[case])
    assert result.returncode == 0 and len(list(output.glob('*.nii'))) == len(list(output.glob('*.json'))) == parts
    archive_objects(project, 'S001_20260725.zip', objects)
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', real_dcm2niix) == 0
    conversion = session_conversion(project)
    assert (conversion['status'], conversion['n_volumes']) == ('converted', parts)
    sidecars = [p for p in (project / 'Niftis/Subjects/S001/20260725').rglob('*.json') if not p.name.startswith('.')]
    assert len(sidecars) == parts
    assert {json.loads(p.read_text())['source_series_key'] for p in sidecars} == set(conversion['coverage']['required'])


def uneven_series(tmp_path):
    """A series with a gap in its slice positions, with signed pixels as GE scanners write them."""
    return mr_series(tmp_path, positions=[0, 1, 2, 4, 5], description='T1 MPRAGE', series_number=5,
                     fields={'PixelRepresentation': 1})[1]


def test_real_converter_adds_equalized_copy_without_sidecar(tmp_path, real_dcm2niix, capsys):
    """dcm2niix's own behavior (plan D-04): uneven slice spacing adds <volume>_Eq_1.nii, which has no sidecar."""
    result, output = run_converter(real_dcm2niix, uneven_series(tmp_path), tmp_path)
    record(capsys, real_dcm2niix, f'series with uneven slice spacing {output_names(output)}', result.returncode)
    [sidecar] = output.glob('*.json')
    assert result.returncode == 0
    assert sorted(p.name for p in output.iterdir()) == [sidecar.stem + '.json', sidecar.stem + '.nii',
                                                        sidecar.stem + '_Eq_1.nii']


def test_real_converter_uneven_slice_spacing_session_converts(project, tmp_path, real_dcm2niix):
    """SB-11: the equalized copy has no sidecar; it is published next to its volume, like _ROI1 since 9aadda0."""
    archive_objects(project, 'S001_20260725.zip', uneven_series(tmp_path))
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', real_dcm2niix) == 0
    assert (session_conversion(project)['status'], session_conversion(project)['n_volumes']) == ('converted', 1)
    [volume] = [p for p in (project / 'Niftis/Subjects/S001/20260725').rglob('*.nii.gz') if '_Eq_' not in p.name]
    assert volume.with_name(volume.name[:-len('.nii.gz')] + '_Eq_1.nii.gz').exists()
    # SB-17: a nonblocking warning says a slice may be missing; the exit code stays 0.
    assert list(state(project)['issues']) == ['spacing:S001/20260725']
    assert 'A slice may be missing' in state(project)['issues']['spacing:S001/20260725']['reason']


# ---- Other object classes and identifying fields (plan C-05, K-01) --------------------

def test_real_converter_with_other_sop_classes_converts(project, tmp_path, real_dcm2niix, capsys):
    """Q19: reports, presentation states, raw data and the like do not stop a session from converting.

    The secondary capture is an image, so dcm2niix converts it; it is published
    although its series is not required.
    """
    study = generate_uid()
    uid, image = mr_series(tmp_path, study=study, description='T1 MPRAGE', series_number=2)
    others = other_class_objects(tmp_path, study)
    result, output = run_converter(real_dcm2niix, image + others, tmp_path)
    record(capsys, real_dcm2niix, f'MR series plus {len(others)} objects of other classes', result.returncode)
    assert result.returncode == 0 and uid in converted_uids(output, image + others)
    archive_objects(project, 'S001_20260725.zip', image + others)
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', real_dcm2niix) == 0
    conversion, coverage = session_conversion(project), session_conversion(project)['coverage']
    assert (conversion['status'], conversion['n_volumes']) == ('converted', 2)
    assert (len(coverage['required']), len(coverage['excused']), coverage['missing']) == (1, len(OTHER_SOP_CLASSES), [])
    assert len(coverage['converted']) == 2


# A distinct value in each identifying element; the text before "^" is what the outputs are searched for.
SENTINELS = {
    'PatientName': 'ZQ01NAME^PATIENT', 'PatientID': 'ZQ02PATIENTID', 'AccessionNumber': 'ZQ03ACCESSION',
    'PatientBirthDate': '19010203', 'InstitutionName': 'ZQ05INSTITUTION', 'ReferringPhysicianName': 'ZQ06REFERRING^DOC',
    'PerformingPhysicianName': 'ZQ07PERFORMING^DOC', 'OperatorsName': 'ZQ08OPERATOR^TECH', 'StationName': 'ZQ09STATION',
    'DeviceSerialNumber': 'ZQ10SERIAL', 'StudyDescription': 'ZQ11STUDYDESC', 'ProtocolName': 'ZQ12PROTOCOL',
    'ImageComments': 'ZQ13IMAGECOMMENT', 'PatientComments': 'ZQ14PATIENTCOMMENT', 'StudyID': 'ZQ15STUDYID',
    'InstitutionAddress': 'ZQ16ADDRESS', 'OtherPatientIDs': 'ZQ17OTHERID', 'InstitutionalDepartmentName': 'ZQ18DEPARTMENT',
    'PatientAddress': 'ZQ19PATIENTADDRESS', 'RequestingPhysician': 'ZQ20REQUESTING^DOC', 'SeriesDescription': 'ZQ21SERIES T1',
    'AcquisitionTime': '101112.000',
}


def sentinels_in(data):
    """Names of the SENTINELS elements whose value appears in the bytes."""
    return {name for name, value in SENTINELS.items() if value.split('^')[0].split('.')[0].split()[0].encode() in data}


def test_real_converter_identifying_fields_stay_out_of_outputs(project, tmp_path, real_dcm2niix, capsys):
    """README "Privacy", measured with the real converter (plan K-01).

    No identifying element reaches a sidecar, receipt, inventory, report, log
    or state file. The NIfTI header text is the documented exception: with the
    pinned dcm2niix it carries the acquisition time ("TE=...;Time=...") and
    nothing else.
    """
    fields = dict(SENTINELS, EchoTime=3.0, RepetitionTime=2000.0, Manufacturer='GE MEDICAL SYSTEMS')
    description = fields.pop('SeriesDescription')
    _, image = mr_series(tmp_path, description=description, series_number=2, fields=fields)
    archive_objects(project, 'S001_20260725.zip', image)
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', real_dcm2niix) == 0
    volumes = list((project / 'Niftis').rglob('*.nii.gz'))
    checked = [p for folder in ('Niftis', 'Reports', '.work') for p in (project / folder).rglob('*')
               if p.is_file() and p not in volumes]
    assert volumes and len(checked) > 8
    for path in checked:
        assert not sentinels_in(path.read_bytes()), path
    in_header = set().union(*(sentinels_in(gzip.decompress(p.read_bytes())) for p in volumes))
    record(capsys, real_dcm2niix, f'identifying elements found in NIfTI header text: {sorted(in_header)}', 0)
    assert in_header == {'AcquisitionTime'}


# ---- Round 2: GE diffusion, physio logs, incomplete series (SB-14, SB-15, SB-20) ------------

GE = {'Manufacturer': 'GE MEDICAL SYSTEMS', 'ManufacturerModelName': 'SIGNA Premier'}
GE_CREATORS = [((0x0019, 0x0010), 'LO', 'GEMS_ACQU_01'), ((0x0021, 0x0010), 'LO', 'GEMS_RELA_01'),
               ((0x0043, 0x0010), 'LO', 'GEMS_PARM_01')]
ACQUISITION = {'ProtocolName': 'test', 'EchoTime': 30, 'RepetitionTime': 2000, 'MRAcquisitionType': '2D',
               'ImageType': ['ORIGINAL', 'PRIMARY', 'M', 'ND'], 'PixelRepresentation': 1}


def with_private(data, private):
    """The object with these private elements added: [(tag, VR, value), ...]."""
    ds = pydicom.dcmread(io.BytesIO(data))
    for tag, vr, value in private:
        ds.add_new(tag, vr, value)
    out = io.BytesIO()
    ds.save_as(out, enforce_file_format=True)
    return out.getvalue()


def volumes_of(tmp_path, volumes, slices=3, common=None, private=None, per_volume=None):
    """One series of volumes x slices objects; per_volume(v) gives that volume's private elements."""
    study, series, objects = generate_uid(), generate_uid(), []
    for v in range(volumes):
        for s in range(slices):
            n = len(objects) + 1
            fields = dict(ACQUISITION, **(common or {}), InstanceNumber=n, ImagePositionPatient=[0.0, 0.0, float(s)])
            data = dicom_bytes(tmp_path, value=n, study=study, series=series, slice_index=s, fields=fields)
            extra = list(private or []) + list(per_volume(v) if per_volume else [])
            objects.append(with_private(data, extra) if extra else data)
    return objects


def test_real_converter_ge_dwi_with_combined_image_converts(project, tmp_path, real_dcm2niix, capsys):
    """SB-14: a GE diffusion series with T2, the three direction images and the combined image.

    dcm2niix writes the combined images as <volume>_ADC.nii with no sidecar; it
    is published next to the volume. GE tags follow dcm2niix's nii_dicom.cpp
    comments; not checked against a real export.
    """
    flags, b_values = [14, 3, 4, 5, 15], [0, 1000, 1000, 1000, 1000]

    def diffusion(v):
        return [((0x0043, 0x1039), 'IS', [b_values[v], 8, 0, 0]), ((0x0019, 0x10BB), 'DS', 0),
                ((0x0019, 0x10BC), 'DS', 0), ((0x0019, 0x10BD), 'DS', 0), ((0x0019, 0x10E0), 'DS', 0),
                ((0x0043, 0x1030), 'SS', flags[v])]

    objects = volumes_of(tmp_path, 5, common=dict(GE, ScanningSequence='EP'), private=GE_CREATORS,
                         per_volume=diffusion)
    result, output = run_converter(real_dcm2niix, objects, tmp_path)
    record(capsys, real_dcm2niix, f'GE diffusion with the combined image {output_names(output)}', result.returncode)
    assert result.returncode == 0 and len(list(output.glob('*_ADC.nii'))) == 1
    assert not list(output.glob('*_ADC.json'))
    archive_objects(project, 'S001_20260725.zip', objects)
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', real_dcm2niix) == 0
    assert (session_conversion(project)['status'], session_conversion(project)['n_volumes']) == ('converted', 1)
    folder = project / 'Niftis/Subjects/S001/20260725'
    [volume] = [p for p in folder.rglob('*.nii.gz') if not p.name.endswith('_ADC.nii.gz')]
    stem = volume.name[:-len('.nii.gz')]
    assert all((volume.parent / (stem + suffix)).exists() for suffix in ('_ADC.nii.gz', '.bval', '.bvec', '.json'))


def physio_log_object(tmp_path, study):
    """A Siemens CMRR physio log (cardiac) stored in a CSA Non-Image object, as the PhysioLog series holds it."""
    lines = ['UUID        = 0f8fad5b-d9cb-469f-a165-70867728950e', 'ScanDate    = 20260725_101112',
             'LogVersion  = EJA_1', 'LogDataType = PULS', 'SampleTime  = 2', '',
             ' ACQ_TIME_TICS  CHANNEL  VALUE  SIGNAL'] + [f'  {14000000 + 2 * i}  PULS  {1600 + i}' for i in range(40)]
    body = ('\n'.join(lines) + '\n').encode()
    name = b'Physio_20260725_101112_x_PULS.log'
    blob = ((len(body).to_bytes(4, 'little') + len(name).to_bytes(4, 'little') + name).ljust(1024, b'\0')
            + body.ljust(3 * 1024, b'\0'))
    ds = pydicom.dcmread(io.BytesIO(non_image_bytes(tmp_path, CSA_NON_IMAGE_STORAGE, study=study)))
    ds.Manufacturer, ds.AcquisitionNumber, ds.SeriesDescription = 'SIEMENS', 4, 'fmri_PhysioLog'
    ds.add_new((0x7FE1, 0x0010), 'LO', 'SIEMENS CSA NON-IMAGE')
    ds.add_new((0x7FE1, 0x1010), 'OB', blob)
    out = io.BytesIO()
    ds.save_as(out, enforce_file_format=True)
    return out.getvalue()


def test_real_converter_physio_log_object_converts(project, tmp_path, real_dcm2niix, capsys):
    """SB-15: dcm2niix turns a physio log into two files and no image; they no longer make the session partial."""
    study = generate_uid()
    _, image = mr_series(tmp_path, study=study, description='T1 MPRAGE', series_number=2,
                         fields={'Manufacturer': 'SIEMENS'})
    objects = image + [physio_log_object(tmp_path, study)]
    result, output = run_converter(real_dcm2niix, objects, tmp_path)
    record(capsys, real_dcm2niix, f'image series plus a physio log {output_names(output)}', result.returncode)
    assert result.returncode == 0 and len(list(output.glob('*_physio.tsv.gz'))) == 1
    archive_objects(project, 'S001_20260725.zip', objects)
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', real_dcm2niix) == 0
    assert session_conversion(project)['status'] == 'converted'
    assert not list((project / 'Niftis').rglob('*physio*'))


def test_real_converter_incomplete_4d_series_needs_acceptance(project, tmp_path, real_dcm2niix, capsys):
    """SB-20: an aborted run (last volume incomplete) makes dcm2niix exit 1 and drop the sidecar.

    The session is partial until a person accepts it with --accept-partial.
    """
    objects = volumes_of(tmp_path, 3)[:-1]
    result, output = run_converter(real_dcm2niix, objects, tmp_path)
    record(capsys, real_dcm2niix, f'4D series with its last volume incomplete {output_names(output)}',
           result.returncode)
    assert result.returncode == 1
    archive_objects(project, 'S001_20260725.zip', objects)
    assert call(project, 'prepare') == 0
    assert call(project, 'convert', '--dcm2niix-bin', real_dcm2niix) == 2
    reason = state(project)['issues']['convert:S001/20260725']['reason']
    assert 'dcm2niix exited with code 1' in reason and 'Missing/invalid JSON sidecar' in reason
    assert call(project, 'convert', '--dcm2niix-bin', real_dcm2niix, '--accept-partial', 'S001/20260725') == 0
    conversion = session_conversion(project)
    assert conversion['status'] == 'converted' and 'dcm2niix exited with code 1' in conversion['accepted_errors']
    assert list((project / 'Niftis/Subjects/S001/20260725/OTHER').glob('*.nii.gz'))
    assert not state(project)['issues']
