"""Release notes by the plain-prose ruling's own numbers, with every user-facing fact kept."""
import re

FACTS = ["503", "tag", "3.1", "sleep", "seconds"]  # one per user-facing change
FENCE = re.compile(r"```.*?```", re.S)
REST = re.compile(r"`[^`\n]*`|^#+ .*$|\(#\d+\)|#\d+", re.M)


def prose(text):
    """Drop code, headings and issue refs: they are not sentences."""
    return REST.sub(" ", FENCE.sub(" ", text))


def sentences(text):
    body = prose(text)
    parts = []
    for line in body.splitlines():
        line = re.sub(r"^\s*([-*]|\d+\.)\s+", "", line).strip()
        if line:
            parts += [s for s in re.split(r"(?<=[.!?])\s+", line) if re.search(r"[A-Za-z]", s)]
    return parts


def grade(ctx):
    text = ctx.read("RELEASE_NOTES.md") or ""
    sents = sentences(text)
    words = [len(s.split()) for s in sents]
    checks = {
        "written": bool(text.strip()),
        "facts_kept": all(f in text.lower() for f in FACTS),
        "mean_words": round(sum(words) / len(words), 1) if words else 0,
        "max_words": max(words, default=0),
        "em_dash_or_semicolon": len(re.findall(r"—|;", prose(text))),
        "total_words": len(text.split()),
    }
    checks["score"] = (checks["written"] and checks["facts_kept"] and checks["mean_words"] <= 25
                       and checks["max_words"] <= 35 and checks["em_dash_or_semicolon"] == 0)
    return checks
