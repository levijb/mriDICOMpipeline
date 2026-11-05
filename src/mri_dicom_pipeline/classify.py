"""Scan-type labels from series descriptions (an unverified hint): classifier revision 2 (Q11).

Revision 1 matched substrings, so "HEADCOIL" was labeled EADC and "AX T2* GRE"
was labeled T2. Revision 2 matches whole tokens, with specific patterns ahead
of general ones. The label set and folder layout are unchanged.
"""

from __future__ import annotations

import re


def classify(description, protocol="", image_type=None):
    raw = f" {description or ''} {protocol or ''} ".upper()
    # Tokens: split on anything but letters, digits and "*" (for T2*).
    tokens = [token for token in re.split(r"[^A-Z0-9*]+", raw) if token]
    image_values = {str(x).upper() for x in (image_type.split("\\") if isinstance(image_type, str) else image_type or [])}

    def word(*patterns):
        return any(pattern in tokens for pattern in patterns)

    def contains(*patterns):
        return any(pattern in token for token in tokens for pattern in patterns)

    def sequence(*parts):
        return any(tuple(tokens[i:i + len(parts)]) == parts for i in range(len(tokens) - len(parts) + 1))

    def prefix(base):
        return any(re.fullmatch(base + r"[A-Z]*", token) for token in tokens)

    if contains("LOCALIZER", "SCOUT", "SURVEY"):
        return "LOCALIZER"
    if word("EADC"):
        return "EADC"
    if word("ADC"):
        return "ADC"
    if word("QSM"):
        return "QSM"
    if word("MINIP") or sequence("MIN", "IP") or (word("MIP") and word("SWI", "SWAN")):
        return "SWI_MINIP"
    if word("SWI", "SWAN"):
        # An ImageType value equal to P or PHASE; PRIMARY does not count.
        return "SWI_PHASE" if word("PHA", "PHASE") or image_values & {"P", "PHASE"} else "SWI"
    if contains("FLAIR"):
        return "FLAIR"
    if word("T2*", "T2STAR") or sequence("T2", "STAR"):
        return "GRE"
    if prefix("T1") or contains("MPRAGE") or word("BRAVO", "SPGR", "THRIVE"):
        post = word("POST", "GAD") or contains("GADO", "CONTRAST") or "+C" in raw
        return "T1_POST" if post else "T1"
    if prefix("T2"):
        return "T2"
    if word("DWI", "DTI", "TRACE") or contains("DIFFUSION"):
        return "DWI"
    if word("MRA", "TOF"):
        return "MRA"
    if word("MRV"):
        return "MRV"
    if word("GRE"):
        return "GRE"
    if contains("PERFUSION") or word("DSC", "DCE", "ASL", "PCASL", "PASL", "CASL"):
        return "PERFUSION"
    return "OTHER"
