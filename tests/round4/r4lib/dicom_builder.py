"""Build synthetic DICOM series and session ZIPs from catalog entries (plan 2.1 item 3).

Written for round 4 from DICOM PS3.3/PS3.5/PS3.10 and pydicom's documented API. It is
independent of ``tests/support.py`` and imports no pipeline module.

A catalog series (see ``r4lib/catalog.py`` for the schema) becomes one classic MR Image
Storage object per (volume, echo, part, slice). UIDs are built by ``study_uid``, ``series_uid``,
``sop_uid`` and ``frame_uid`` below, under the root ``UID_ROOT`` (pydicom's root plus 4242), as
round 4 plan 3.2 requires.
"""

from __future__ import annotations

import hashlib
import io
import os
import struct
import zipfile

import numpy as np
import pydicom
from pydicom.datadict import dictionary_VR, tag_for_keyword
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage
from pydicom.valuerep import format_number_as_ds

UID_ROOT = "1.2.826.0.1.3680043.8.498.4242."
FIXED_ZIP_TIME = (2026, 7, 25, 12, 0, 0)

SENTINEL_FIELDS = (
    "PatientName", "PatientID", "PatientBirthDate", "OtherPatientIDs",
    "AccessionNumber", "StudyID", "StudyDescription", "StudyComments",
    "InstitutionName", "InstitutionAddress", "ReferringPhysicianName", "PerformingPhysicianName",
    "OperatorsName", "StationName", "DeviceSerialNumber",
    "SeriesDescription", "ProtocolName", "ImageComments",
)
SENTINEL_DATE = "19010101"
SENTINEL_TIME = "010101.000"
PART_TOKEN = {"mag": "M", "phase": "P", "real": "R", "imag": "I"}
GE_PART_VALUE = {"mag": 0, "phase": 1, "real": 2, "imag": 3}


def study_uid(exam):
    return f"{UID_ROOT}1.{exam}"


def series_uid(exam, series):
    return f"{UID_ROOT}2.{exam}.{series}"


def sop_uid(exam, series, instance):
    return f"{UID_ROOT}3.{exam}.{series}.{instance}"


def frame_uid(exam, frame):
    return f"{UID_ROOT}4.{exam}.{frame}"


# SH elements hold at most 16 characters (PS3.5 6.2), so their sentinels are shortened; every
# sentinel still starts with "R4SENT_", which is what the scanner searches for (plan 3.2).
SHORT_SENTINELS = {"AccessionNumber": "R4SENT_AccNum", "StationName": "R4SENT_Station"}


def sentinel_value(keyword):
    if keyword == "PatientBirthDate":
        return SENTINEL_DATE
    value = SHORT_SENTINELS.get(keyword, f"R4SENT_{keyword}")
    if dictionary_VR(tag_for_keyword(keyword)) == "SH" and len(value) > 16:
        raise ValueError(f"sentinel for SH element {keyword} is longer than 16 characters")
    return value


def _ds_value(value):
    if isinstance(value, (list, tuple)):
        return [format_number_as_ds(float(v)) for v in value]
    return format_number_as_ds(float(value))


def set_keyword(ds, keyword, value):
    """Set a standard element by keyword, formatting DS and IS values within their limits."""
    vr = dictionary_VR(tag_for_keyword(keyword))
    if value is None:
        setattr(ds, keyword, None)
        return
    if vr == "DS":
        value = _ds_value(value)
    elif vr == "IS":
        value = [str(int(v)) for v in value] if isinstance(value, (list, tuple)) else str(int(value))
    setattr(ds, keyword, value)


def _parse_tag(text):
    group, element = text.replace("(", "").replace(")", "").split(",")
    return int(group, 16), int(element, 16)


def csa_header(items):
    """A Siemens CSA2 ('SV10') header holding string items {name: value}. Layout as documented
    by NiBabel's CSA reader (nibabel.nicom.csareader): 'SV10', 4 unused bytes, tag count, 77,
    then per tag a 64-byte name, VM, 4-byte VR, SyngoDT, item count, 77, and per item four
    int32 lengths followed by the value padded to 4 bytes."""
    out = b"SV10" + bytes([4, 3, 2, 1]) + struct.pack("<II", len(items), 77)
    for name, value in items.items():
        out += name.encode("ascii").ljust(64, b"\0") + struct.pack("<i", 1) + b"IS\0\0" + struct.pack("<iii", 0, 1, 77)
        v = str(value).encode("ascii") + b"\0"
        out += struct.pack("<iiii", len(v), len(v), 77, len(v)) + v + b"\0" * ((4 - len(v) % 4) % 4)
    return out


def add_private(ds, spec, value=None):
    """Write one private element with its creator. ``spec`` keys: tag 'gggg,eeee', creator,
    vr, value. The creator goes to (gggg,00xx) where xx is the element's block byte. A spec
    with ``csa`` ({name: value}) writes a CSA header built by ``csa_header``."""
    group, element = _parse_tag(spec["tag"])
    block = element >> 8
    creator_tag = (group << 16) | block
    if creator_tag not in ds:
        ds.add_new(creator_tag, "LO", spec["creator"])
    if value is None:
        value = csa_header(spec["csa"]) if "csa" in spec else spec["value"]
    ds.add_new((group << 16) | element, spec["vr"], value)


def pixel_array(rows, cols, seed):
    """Deterministic uint16 image: random values 100-3999 from a seeded generator, so that
    mirrored positions rarely share a value (pixel sampler self-test, plan 3.6)."""
    rng = np.random.default_rng(seed)
    return rng.integers(100, 4000, size=(rows, cols), dtype=np.uint16)


def _seed(*parts):
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:12], 16)


def _time_plus(tm, seconds):
    """TM 'HHMMSS[.ffffff]' plus seconds, as 'HHMMSS.ffffff'."""
    h, m = int(tm[0:2]), int(tm[2:4])
    s = float(tm[4:]) if len(tm) > 4 else 0.0
    total = h * 3600 + m * 60 + s + seconds
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}{int(m):02d}{s:09.6f}"


def stack_specs(series):
    """The series itself, or one merged spec per entry of ``stacks`` (a mixed series: several
    stacks under one SeriesInstanceUID, each with its own geometry, tags or slice count)."""
    if not series.get("stacks"):
        return [series]
    out = []
    for stack in series["stacks"]:
        spec = dict(series)
        spec.pop("stacks")
        spec["geometry"] = {**series["geometry"], **stack.get("geometry", {})}
        spec["tags"] = {**series["tags"], **{k: v[0] for k, v in stack.get("tags", {}).items()}}
        for key in ("slices", "echoes", "parts"):
            if key in stack:
                spec[key] = stack[key]
        out.append(spec)
    return out


def series_instances(exam, session, series, sentinels=False):
    """Yield (relative member path, pydicom Dataset) for every instance of one series.

    ``exam`` is the catalog exam dict, ``session`` its session dict, ``series`` the expanded
    series dict (protocol merged, see catalog.expand_series). Instance numbers run on across
    the stacks of a mixed series.
    """
    start = 0
    for spec in stack_specs(series):
        for member, ds in _stack_instances(exam, session, spec, sentinels, start):
            start += 1
            yield member, ds


def _stack_instances(exam, session, series, sentinels, start):
    g = series["geometry"]
    rows, cols = int(g["rows"]), int(g["cols"])
    iop = [float(v) for v in g["iop"]]
    x = np.array(iop[0:3])
    y = np.array(iop[3:6])
    normal = np.cross(x, y)
    normal = normal / np.linalg.norm(normal)
    first = np.array([float(v) for v in g["first_position"]])
    step = float(g["step_mm"])
    n_slices = int(series["slices"])
    slice_positions = g.get("positions_mm")  # explicit offsets along the normal (uneven spacing)
    echoes = series.get("echoes") or [None]
    parts = series.get("parts") or ["mag"]
    part_encoding = series.get("part_encoding", "imagetype")
    grads = series.get("gradients")
    n_volumes = len(grads["bvals"]) if grads else int(series.get("volumes", 1))
    tags = series["tags"]
    uid_e, uid_s = exam["uid_exam"], series["uid_series"]
    frame = int(g.get("frame", uid_s))
    time_step = float(series.get("instance_time_step_s", 0.0))
    member_dir = series.get("member_dir", f"DICOM/{uid_s:04d}")
    if sentinels:
        member_dir = f"R4SENT_MEMBER/{member_dir}"

    instance = start
    for vol in range(n_volumes):
        for e_index, te in enumerate(echoes):
            for part in parts:
                for k in range(n_slices):
                    instance += 1
                    offset = float(slice_positions[k]) if slice_positions else k * step
                    ipp = first + offset * normal
                    ds = _base_dataset(exam, session, series, sentinels)
                    ds.SOPInstanceUID = sop_uid(uid_e, uid_s, instance)
                    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
                    ds.InstanceNumber = str(instance)
                    ds.FrameOfReferenceUID = frame_uid(uid_e, frame)
                    set_keyword(ds, "ImageOrientationPatient", iop)
                    set_keyword(ds, "ImagePositionPatient", [float(v) for v in ipp])
                    set_keyword(ds, "SliceLocation", float(np.dot(ipp, normal)))
                    set_keyword(ds, "PixelSpacing", [float(v) for v in g["pixel_spacing"]])
                    ds.Rows, ds.Columns = rows, cols
                    if te is not None:
                        set_keyword(ds, "EchoTime", te)
                        if len(echoes) > 1 or series.get("echo_numbers_always"):
                            ds.EchoNumbers = str(e_index + 1)
                    # Parts.
                    image_type = list(tags.get("ImageType", ["ORIGINAL", "PRIMARY", "OTHER"]))
                    token = PART_TOKEN[part]
                    image_type = [token if t == "{part}" else t for t in image_type]
                    if part_encoding == "ge_private":
                        add_private(ds, {"tag": "0043,102F", "creator": "GEMS_PARM_01", "vr": "SS",
                                         "value": GE_PART_VALUE[part]})
                    ds.ImageType = image_type
                    if "ImageTypeText" in series:
                        itt = [token if t == "{part}" else t for t in series["ImageTypeText"]]
                        add_private(ds, {"tag": "0021,1175", "creator": "SIEMENS MR SDI 02",
                                         "vr": "CS", "value": itt})
                    # Times: per-instance step (2D Espree-like) when the catalog asks for it.
                    acq = tags.get("AcquisitionTime")
                    if acq is not None:
                        ds.AcquisitionTime = _time_plus(acq, time_step * (instance - 1)) if time_step else acq
                    # Diffusion.
                    if grads:
                        _diffusion_tags(ds, series, float(grads["bvals"][vol]),
                                        [float(v) for v in grads["bvecs"][vol]])
                    # References (ReferencedImageSequence).
                    refs = series.get("references")
                    if refs:
                        seq = Sequence()
                        for ref in refs:
                            item = Dataset()
                            item.ReferencedSOPClassUID = MRImageStorage
                            item.ReferencedSOPInstanceUID = sop_uid(uid_e, int(ref["series"]), int(ref["instance"]))
                            seq.append(item)
                        ds.ReferencedImageSequence = seq
                    arr = pixel_array(rows, cols, _seed(uid_e, uid_s, vol, e_index, part, k))
                    ds.PixelData = arr.tobytes()
                    yield f"{member_dir}/{instance:05d}", ds


def _base_dataset(exam, session, series, sentinels):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = MRImageStorage
    ds.Modality = "MR"
    ds.StudyInstanceUID = study_uid(series.get("uid_study", session.get("uid_study", exam["uid_exam"])))
    ds.SeriesInstanceUID = series_uid(exam["uid_exam"], series["uid_series"])
    ds.StudyDate = session["study_date"]
    ds.StudyTime = session.get("study_time", "080000.000000")
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 12
    ds.HighBit = 11
    ds.PixelRepresentation = 0
    # Non-sentinel identifying fields carry harmless synthetic values.
    ds.PatientName = "R4^SYNTHETIC"
    ds.PatientID = session.get("patient_id", "R4PATIENT")
    ds.PatientPosition = "HFS"  # real exports carry it; dcm2niix warns without it (its issue 642)
    for keyword, value in series["tags"].items():
        if keyword in ("ImageType", "AcquisitionTime"):
            continue
        set_keyword(ds, keyword, value)
    for spec in series.get("private_tags", []):
        add_private(ds, spec)
    if sentinels:
        for keyword in SENTINEL_FIELDS:
            set_keyword(ds, keyword, sentinel_value(keyword))
    return ds


def _diffusion_tags(ds, series, bval, bvec):
    """Standard (PS3.3 C.8.13.5.9) and vendor diffusion tags, so dcm2niix can write .bval/.bvec."""
    ds.add_new(0x00189087, "FD", bval)                       # DiffusionBValue
    ds.add_new(0x00189089, "FD", bvec)                       # DiffusionGradientOrientation
    directional = "NONE" if bval <= 0 else ("DIRECTIONAL" if any(bvec) else "ISOTROPIC")
    ds.add_new(0x00189075, "CS", directional)                # DiffusionDirectionality
    vendor = series["tags"].get("Manufacturer", "").upper()
    if vendor.startswith("SIEMENS"):
        add_private(ds, {"tag": "0019,100C", "creator": "SIEMENS MR HEADER", "vr": "IS", "value": str(int(round(bval)))})
        add_private(ds, {"tag": "0019,100D", "creator": "SIEMENS MR HEADER", "vr": "CS", "value": directional})
        if any(bvec):
            add_private(ds, {"tag": "0019,100E", "creator": "SIEMENS MR HEADER", "vr": "FD", "value": bvec})
    elif vendor.startswith("GE"):
        add_private(ds, {"tag": "0043,1039", "creator": "GEMS_PARM_01", "vr": "IS",
                         "value": [str(int(round(bval))), "0", "0", "0"]})
        for element, comp in zip(("0019,10BB", "0019,10BC", "0019,10BD"), bvec):
            add_private(ds, {"tag": element, "creator": "GEMS_ACQU_01", "vr": "DS",
                             "value": format_number_as_ds(float(comp))})


def write_series(directory, exam, session, series, sentinels=False):
    """Write one series as extensionless files below ``directory``; returns the paths."""
    paths = []
    for member, ds in series_instances(exam, session, series, sentinels):
        path = os.path.join(directory, *member.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ds.save_as(path, enforce_file_format=True)
        paths.append(path)
    return paths


def session_zip_bytes(exam, session, series_list, sentinels=False, member_order=None):
    """A session ZIP in memory: one extensionless member per instance, deterministic bytes.

    ``member_order`` may be 'reverse' (R4-CAC-04: same content, different member order).
    """
    members = []
    for series in series_list:
        for member, ds in series_instances(exam, session, series, sentinels):
            buf = io.BytesIO()
            ds.save_as(buf, enforce_file_format=True)
            members.append((member, buf.getvalue()))
    if member_order == "reverse":
        members.reverse()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for member, data in members:
            info = zipfile.ZipInfo(member, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return out.getvalue()
