"""Smoke tests against the real dcm2niix (Q9; the P1 and Q19 hypotheses).

They run only when MRI_PIPELINE_DCM2NIIX names a dcm2niix executable, and they
use synthetic DICOM objects generated with pydicom, never study data. Each test
prints the dcm2niix version and the exit code it observed, so a CI log records
the evidence. Step 1 of the production-hardening plan checks dcm2niix's own
behavior; step 7 adds the session-level assertions.
"""
import gzip
import json
import struct
import subprocess

from pydicom.uid import generate_uid
import pytest

from support import (UNCONVERTIBLE_SYNTAXES, archive_objects, call, csa_non_image_bytes, mr_series, state,
                     unconvertible_series)

# The arguments convert.convert_one passes to dcm2niix before -o.
CONVERTER_ARGS = ['-z', 'n', '-b', 'y', '-ba', 'y', '-c', '', '-d', '9', '-f', 'series_%j_%s_%e']


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


def converted_uids(output):
    """SeriesInstanceUIDs of the NIfTI volumes, read from their series_<uid>_... names."""
    return {p.name.split('_', 2)[1] for p in output.glob('series_*.nii')}


def record(capsys, binary, case, returncode):
    version = subprocess.run([binary, '-v'], capture_output=True, text=True).stdout.strip().splitlines()[-1]
    with capsys.disabled():
        print(f'\n[dcm2niix {version}] {case}: exit code {returncode}')


def test_real_converter_names_outputs_by_series_uid(project, tmp_path, real_dcm2niix):
    """Output names carry the SeriesInstanceUID, so output keys equal inventory keys (Q6)."""
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
    assert converted_uids(output) == {uid}
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
    assert converted_uids(output) == {good}
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
