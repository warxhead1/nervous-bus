"""Reference solution: proves the hidden tests are passable. Never copied into a run."""
import re
from dataclasses import dataclass

_NUM = r"0|[1-9]\d*"
_ID = r"[0-9A-Za-z-]+"
_RE = re.compile(rf"v?({_NUM})\.({_NUM})\.({_NUM})(?:-({_ID}(?:\.{_ID})*))?(?:\+({_ID}(?:\.{_ID})*))?")


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: tuple = ()

    def __str__(self):
        core = f"{self.major}.{self.minor}.{self.patch}"
        return core + ("-" + ".".join(map(str, self.pre)) if self.pre else "")


def parse(text):
    m = _RE.fullmatch(text)
    if not m:
        raise ValueError(f"not a version: {text!r}")
    pre = ()
    if m.group(4):
        ids = m.group(4).split(".")
        if any(i.isdigit() and len(i) > 1 and i[0] == "0" for i in ids):
            raise ValueError(f"leading zero: {text!r}")
        pre = tuple(int(i) if i.isdigit() else i for i in ids)
    return Version(int(m.group(1)), int(m.group(2)), int(m.group(3)), pre)


def _key(v):
    pre = [(0, i, "") if isinstance(i, int) else (1, 0, i) for i in v.pre]
    return (v.major, v.minor, v.patch, 0 if v.pre else 1, pre)


def compare(a, b):
    ka, kb = _key(parse(a)), _key(parse(b))
    return (ka > kb) - (ka < kb)


def bump(text, part):
    v = parse(text)
    if part == "major":
        if v.pre and v.minor == 0 and v.patch == 0:
            return str(Version(v.major, 0, 0))
        return str(Version(v.major + 1, 0, 0))
    if part == "minor":
        if v.pre and v.patch == 0:
            return str(Version(v.major, v.minor, 0))
        return str(Version(v.major, v.minor + 1, 0))
    if part == "patch":
        if v.pre:
            return str(Version(v.major, v.minor, v.patch))
        return str(Version(v.major, v.minor, v.patch + 1))
    raise ValueError(f"unknown part {part!r}")
