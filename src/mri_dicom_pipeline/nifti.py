"""NIfTI validation and gzip, and JSON sidecar filtering."""

from __future__ import annotations

import gzip
import json
import math
from pathlib import Path
import struct


# Free-text descriptions are used in memory for classification, never copied
# to public metadata. Technical numeric fields are selected explicitly.
TECH_FIELDS = {"Modality", "Manufacturer", "ManufacturersModelName", "MagneticFieldStrength",
    "SeriesNumber", "EchoNumber", "EchoTime", "RepetitionTime", "InversionTime", "FlipAngle",
    "ImageType", "ImageOrientationPatientDICOM", "PixelSpacing", "SliceThickness",
    "SpacingBetweenSlices", "MRAcquisitionType", "ScanningSequence", "SequenceVariant",
    "PhaseEncodingDirection", "TotalReadoutTime", "EffectiveEchoSpacing", "DiffusionBValue",
    "ConversionSoftware", "ConversionSoftwareVersion"}


def validate_and_gzip(source, destination):
    """Check a NIfTI file's structure, then gzip it with every byte unchanged (Q10).

    The header, the extension area and the voxel data are written exactly as
    the converter wrote them, so the output decompresses to the converter's
    bytes. Header text therefore reaches the output (a documented privacy
    channel). Gzip settings (no file name, mtime 0, level 6) keep the output
    repeatable. The destination is deleted on any failure.
    """
    destination = Path(destination)
    try:
        opener = gzip.open if str(source).endswith(".gz") else open
        with opener(source, "rb") as src:
            first = src.read(4)
            if len(first) != 4:
                raise ValueError("Truncated NIfTI header")
            endian = "<" if struct.unpack("<i", first)[0] in (348, 540) else ">"
            size = struct.unpack(endian + "i", first)[0]
            if size not in (348, 540):
                raise ValueError("Invalid NIfTI header size")
            header = first + src.read(size - 4)
            if len(header) != size:
                raise ValueError("Truncated NIfTI header")
            if size == 348:
                if header[344:348] != b"n+1\0":
                    raise ValueError("Expected single-file NIfTI-1")
                dims = struct.unpack_from(endian + "8h", header, 40)
                bitpix = struct.unpack_from(endian + "h", header, 72)[0]
                raw_offset = struct.unpack_from(endian + "f", header, 108)[0]
                if not math.isfinite(raw_offset) or raw_offset != int(raw_offset):
                    raise ValueError("Invalid NIfTI voxel offset")
                offset = int(raw_offset)
            else:
                if header[4:12] != b"n+2\0\r\n\x1a\n":
                    raise ValueError("Expected single-file NIfTI-2")
                dims = struct.unpack_from(endian + "8q", header, 16)
                bitpix = struct.unpack_from(endian + "h", header, 14)[0]
                offset = struct.unpack_from(endian + "q", header, 168)[0]
            if not 1 <= dims[0] <= 7 or any(d <= 0 for d in dims[1:dims[0]+1]) or bitpix <= 0:
                raise ValueError("Invalid NIfTI dimensions/bit depth")
            if not size + 4 <= offset <= 16 * 1024 * 1024:
                raise ValueError("Invalid or unusually large NIfTI extension area")
            extensions = src.read(offset - size)
            if len(extensions) != offset - size:
                raise ValueError("Truncated NIfTI extension area")
            required = (math.prod(dims[1:dims[0]+1]) * bitpix + 7) // 8
            count = 0
            with destination.open("wb") as raw_out:
                with gzip.GzipFile(filename="", fileobj=raw_out, mode="wb", mtime=0, compresslevel=6) as out:
                    out.write(header)
                    out.write(extensions)
                    for block in iter(lambda: src.read(1024 * 1024), b""):
                        count += len(block)
                        out.write(block)
            if count < required:
                raise ValueError("Truncated NIfTI pixel data")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def check_companion(path):
    """Diffusion companions (.bval, .bvec) must contain only finite numbers."""
    values = Path(path).read_text().split()
    if not values or not all(math.isfinite(float(x)) for x in values):
        raise ValueError("Invalid diffusion companion values")


def clean_metadata(metadata):
    """Keep technical fields, never patient identifiers, dates or free text."""
    result = {}
    for key in TECH_FIELDS:
        value = metadata.get(key)
        if value is None:
            continue
        # No nested dictionaries/unknown metadata containers are propagated.
        if isinstance(value, (str, int, float, bool)) or (isinstance(value, list) and all(isinstance(x, (str, int, float, bool)) for x in value)):
            try:
                json.dumps(value, allow_nan=False)
                result[key] = value
            except ValueError:
                pass
    return result
