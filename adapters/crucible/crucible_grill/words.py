"""The reserved answer words, matched by exact rule with no model."""
from . import cfg


def classify(text):
    """Return (word row, argument) for a reserved phrase, or None. Matching ignores case,
    spacing and trailing punctuation. A prefix word takes the words after it as its argument."""
    raw = " ".join((text or "").split())
    norm = raw.lower().rstrip(".!?, ")
    for w in cfg.cards().get("word", []):
        for ph in w["phrases"]:
            if w["match"] == "exact" and norm == ph:
                return w, ""
            if w["match"] == "prefix" and (norm == ph or (norm.startswith(ph) and not norm[len(ph)].isalnum())):
                return w, raw[len(ph):].lstrip(" :,-")
    return None
