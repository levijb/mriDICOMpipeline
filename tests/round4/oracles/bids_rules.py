"""BIDS naming rules for the files this export uses (plan 2.1 item 16; oracle class S).

Written from the BIDS specification 1.11 (entity table in the appendix; anat, dwi and fmap
sections) and bids-output-research.md 2.1-2.4. Cross-checked by a test against the schema that
bidsschematools ships. Imports no pipeline module.

``check_path(relpath)`` returns a list of problems (empty for a valid standard name) and the
parsed parts. Conventional names (bids-output-research.md 4.2, 4.1: ``_swi``, ``_minIP``,
``_QSM``) are reported as conventional, not as errors.
"""

from __future__ import annotations

import re

# BIDS entity order (appendix, entity table), as key names used in filenames.
ENTITY_ORDER = ("sub", "ses", "sample", "task", "tracksys", "acq", "nuc", "voi", "ce", "trc", "stain",
                "rec", "dir", "run", "mod", "echo", "flip", "inv", "mt", "part", "proc", "hemi",
                "space", "split", "recording", "chunk", "seg", "res", "den", "label", "desc")
INDEX_ENTITIES = {"run", "echo", "flip", "inv", "split", "chunk"}

ANAT_NONPARAMETRIC = {"sub", "ses", "task", "acq", "ce", "rec", "run", "echo", "part", "chunk"}
# suffix -> (datatype, allowed entities, required entities)
SUFFIXES = {
    "T1w": ("anat", ANAT_NONPARAMETRIC, {"sub"}),
    "T2w": ("anat", ANAT_NONPARAMETRIC, {"sub"}),
    "FLAIR": ("anat", ANAT_NONPARAMETRIC, {"sub"}),
    "T2starw": ("anat", ANAT_NONPARAMETRIC, {"sub"}),
    "PDw": ("anat", ANAT_NONPARAMETRIC, {"sub"}),
    "angio": ("anat", ANAT_NONPARAMETRIC, {"sub"}),
    "MEGRE": ("anat", ANAT_NONPARAMETRIC, {"sub", "echo"}),
    "dwi": ("dwi", {"sub", "ses", "acq", "rec", "dir", "run", "part", "chunk"}, {"sub"}),
    # dir follows the BIDS schema's entity rule: optional on epi (owner decision at G1, 2026-11-24).
    # That this export always writes dir on its field maps is a policy expectation of R4-BEX-02.
    "epi": ("fmap", {"sub", "ses", "acq", "ce", "rec", "dir", "run", "part", "chunk"}, {"sub"}),
    # Scanner-derived diffusion maps (BIDS 1.10.1, 'ScannerDerivatives'): no part entity.
    "ADC": ("dwi", {"sub", "ses", "acq", "rec", "dir", "run", "chunk"}, {"sub"}),
    "expADC": ("dwi", {"sub", "ses", "acq", "rec", "dir", "run", "chunk"}, {"sub"}),
    "trace": ("dwi", {"sub", "ses", "acq", "rec", "dir", "run", "chunk"}, {"sub"}),
}
DWI_DERIVED = ("ADC", "expADC", "trace")
CONVENTIONAL = {"swi": "anat", "minIP": "anat", "QSM": "anat"}
EXTENSIONS = (".nii.gz", ".nii", ".json", ".bval", ".bvec", ".tsv")
LABEL = re.compile(r"^[0-9A-Za-z+]+$")
INDEX = re.compile(r"^[0-9]+$")


def parse_name(filename):
    """(entities as an ordered list of (key, value), suffix, extension) or raises ValueError."""
    ext = next((e for e in EXTENSIONS if filename.endswith(e)), None)
    if ext is None:
        raise ValueError("unknown extension")
    stem = filename[: -len(ext)]
    parts = stem.split("_")
    suffix = parts[-1]
    entities = []
    for p in parts[:-1]:
        if "-" not in p:
            raise ValueError(f"entity without '-': {p!r}")
        key, value = p.split("-", 1)
        entities.append((key, value))
    return entities, suffix, ext


def check_path(relpath):
    """Problems with one path relative to the dataset root, and its parsed parts."""
    problems = []
    segs = relpath.replace("\\", "/").split("/")
    filename = segs[-1]
    try:
        entities, suffix, _ext = parse_name(filename)
    except ValueError as exc:
        return [f"unparseable name: {exc}"], None
    keys = [k for k, _ in entities]
    values = dict(entities)
    status = "standard"
    if suffix in CONVENTIONAL:
        status = "conventional"
        datatype, allowed, required = CONVENTIONAL[suffix], ANAT_NONPARAMETRIC, {"sub"}
    elif suffix in SUFFIXES:
        datatype, allowed, required = SUFFIXES[suffix]
    else:
        return [f"suffix not in this export's rule table: {suffix}"], None
    if len(keys) != len(set(keys)):
        problems.append("an entity appears twice")
    unknown = [k for k in keys if k not in ENTITY_ORDER]
    if unknown:
        problems.append(f"unknown entities {unknown}")
    order = [ENTITY_ORDER.index(k) for k in keys if k in ENTITY_ORDER]
    if order != sorted(order):
        problems.append("entities out of order")
    not_allowed = [k for k in keys if k not in allowed]
    if not_allowed:
        problems.append(f"entities not allowed for {suffix}: {not_allowed}")
    missing = [k for k in required if k not in values]
    if missing:
        problems.append(f"required entities missing: {missing}")
    for key, value in entities:
        pattern = INDEX if key in INDEX_ENTITIES else LABEL
        if not pattern.match(value):
            problems.append(f"bad {key} value {value!r}")
    # Folder structure: sub-<label>/[ses-<label>/]<datatype>/<file>.
    if len(segs) not in (3, 4):
        problems.append("path is not sub-<label>/[ses-<label>/]<datatype>/<file>")
    else:
        if segs[0] != f"sub-{values.get('sub')}":
            problems.append("subject folder and filename disagree")
        if len(segs) == 4 and segs[1] != f"ses-{values.get('ses')}":
            problems.append("session folder and filename disagree")
        if len(segs) == 3 and "ses" in values:
            problems.append("ses entity without a session folder")
        if segs[-2] != datatype:
            problems.append(f"datatype folder {segs[-2]!r} should be {datatype!r}")
    return problems, {"entities": entities, "suffix": suffix, "datatype": datatype, "status": status}


def valid_subject_label(label):
    """BIDS labels are letters and digits (and '+'); '-' and '_' separate entities."""
    return bool(LABEL.match(label))


def bids_uri(relpath):
    """IntendedFor value as a BIDS URI relative to the dataset (bids-output-research.md 2.4)."""
    return "bids::" + relpath.replace("\\", "/")
