"""Scan-type labels from series descriptions (an unverified hint)."""

from __future__ import annotations


def classify(description, protocol="", image_type=None):
    text = f" {description or ''} {protocol or ''} ".upper()
    image_tokens = set(str(x).upper() for x in (image_type.split("\\") if isinstance(image_type, str) else image_type or []))
    if any(x in text for x in ("LOCALIZER", "SCOUT", "SURVEY")):
        return "LOCALIZER"
    if "EADC" in text:
        return "EADC"
    if "ADC" in text:
        return "ADC"
    if "QSM" in text:
        return "QSM"
    if any(x in text for x in ("SWI", "SWAN")):
        if any(x in text for x in ("PHA", "PHASE")) or image_tokens & {"P", "PHASE"}:
            return "SWI_PHASE"
        return "SWI"
    if any(x in text for x in ("MIN IP", "MINIP")):
        return "SWI_MINIP"
    for label, words in [("FLAIR", ("FLAIR",)), ("T1", ("T1", "MPRAGE", "BRAVO", "SPGR", "THRIVE")),
            ("T2", ("T2",)), ("DWI", ("DWI", "DIFFUSION", "TRACE", "DTI")),
            ("MRA", ("MRA", "TOF")), ("MRV", ("MRV",)), ("GRE", ("GRE",)),
            ("PERFUSION", ("PERFUSION", "DSC", "DCE", "ASL"))]:
        if any(word in text for word in words):
            return "T1_POST" if label == "T1" and any(w in text for w in ("POST", "GAD", "+C", "CONTRAST")) else label
    return "OTHER"
