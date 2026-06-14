"""detectors/langpacks.py — per-language struct/type field extractors.

The struct-field-overlap signal in ``dual_source`` needs to recover, for every
record type in a source file, its NAME and the set of its FIELD NAMES. The probe
proved a single ``struct Name{}`` regex misses Go and Python entirely:

  - Rust / C / GLSL / Slang : ``struct Name { ... }``        (brace body)
  - Go                      : ``type Name struct { ... }``    (type keyword)
  - Python                  : ``@dataclass class Name:`` /     (indent body,
                              ``class Name:`` with annotated fields)        annotations)

Each language pack is a callable ``extract(text) -> list[(name, fields)]`` where
``fields`` is the raw set of declared field names (noise-filtering happens in the
detector, so packs stay dumb). ``extract_all`` dispatches by file extension and
unions the results, so a polyglot repo is handled with zero config.

These are deliberately heuristic regex/indent parsers, not real grammars — the
detector only needs field-name SETS for overlap, and false structs are harmless
(they just won't overlap with anything meaningful). Keeping them regex-based
means no per-language toolchain dependency in the engine.
"""
from __future__ import annotations

import re
from typing import Callable


# ── Rust / C / GLSL / Slang : struct Name { body } ────────────────────────────

# struct Name {  ...  }  — captures name + body up to the first '}'. We use
# [^}]* (matching the validated tengine detector) rather than [^{}]* so a body
# containing an inline initializer brace (e.g. `= {0}`) still yields its fields;
# nested struct DEFINITIONS are rare in the flat data structs this targets, and a
# truncated body only ever drops fields (never invents them), which is safe for
# the overlap signal.
_C_STRUCT = re.compile(r"\bstruct\s+(\w+)\s*\{([^}]*)\}", re.DOTALL)
# Field NAME inside a C/GLSL/Slang struct body:  [public] type name[...];
_C_FIELD = re.compile(r"^\s*(?:public\s+)?[\w:<>*&\s]+?\b(\w+)\s*(?:\[[^\]]*\])?\s*[;,]", re.M)
# Field NAME inside a Rust struct body:  [pub] name: Type,
_RS_FIELD = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(\w+)\s*:\s*[\w:<>\[\]&' ]+,", re.M)


def extract_c_like(text: str) -> list[tuple[str, set[str]]]:
    """Rust / C / GLSL / Slang ``struct Name { ... }`` records."""
    out: list[tuple[str, set[str]]] = []
    for m in _C_STRUCT.finditer(text):
        name, body = m.group(1), m.group(2)
        fields = set(_C_FIELD.findall(body)) | set(_RS_FIELD.findall(body))
        out.append((name, fields))
    return out


# ── Go : type Name struct { body } ────────────────────────────────────────────

_GO_STRUCT = re.compile(r"\btype\s+(\w+)\s+struct\s*\{([^{}]*)\}", re.DOTALL)
# Go field:  Name Type  (exported or not) OR  Name, Other Type  — one per line.
# Embedded fields (just a type) are ignored; we want named fields for overlap.
_GO_FIELD_LINE = re.compile(r"^\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s+[\w\.\*\[\]<>{}]+")


def extract_go(text: str) -> list[tuple[str, set[str]]]:
    """Go ``type Name struct { ... }`` records."""
    out: list[tuple[str, set[str]]] = []
    for m in _GO_STRUCT.finditer(text):
        name, body = m.group(1), m.group(2)
        fields: set[str] = set()
        for line in body.splitlines():
            ls = line.strip()
            if not ls or ls.startswith("//"):
                continue
            fm = _GO_FIELD_LINE.match(line)
            if fm:
                for part in fm.group(1).split(","):
                    fields.add(part.strip())
        out.append((name, fields))
    return out


# ── Python : @dataclass / class with annotated fields ─────────────────────────

# class Name(...):  — header. We then read the indented body for `field: Type`
# annotations (and dataclass-style `field: Type = default`).
_PY_CLASS = re.compile(r"^([ \t]*)class\s+(\w+)\s*(?:\([^)]*\))?\s*:", re.M)
# An annotated attribute inside a class body:  name: Type  [= default]
_PY_ANNOT = re.compile(r"^\s*([A-Za-z_]\w*)\s*:\s*[^=\n]+(?:=.*)?$")


def extract_python(text: str) -> list[tuple[str, set[str]]]:
    """Python classes (incl. ``@dataclass``) with annotated fields.

    Field NAMES are the annotated class attributes (``name: Type``). Methods and
    non-annotated assignments are ignored — annotations are the structural shape.
    """
    lines = text.splitlines()
    out: list[tuple[str, set[str]]] = []
    for m in _PY_CLASS.finditer(text):
        indent = m.group(1)
        name = m.group(2)
        # Find the line index of this class header.
        start_line = text.count("\n", 0, m.start())
        fields: set[str] = set()
        body_indent_len: int | None = None
        for j in range(start_line + 1, len(lines)):
            raw = lines[j]
            if not raw.strip():
                continue
            cur_indent = len(raw) - len(raw.lstrip())
            # Dedent back to/under the class header => class body ended.
            if cur_indent <= len(indent) and raw.strip():
                break
            if body_indent_len is None:
                body_indent_len = cur_indent
            # Only consider statements at the class body's own indent level
            # (skip nested blocks like method bodies).
            if cur_indent != body_indent_len:
                continue
            if raw.lstrip().startswith(("def ", "async def ", "@", "class ")):
                continue
            am = _PY_ANNOT.match(raw)
            if am:
                fields.add(am.group(1))
        out.append((name, fields))
    return out


# ── dispatch ──────────────────────────────────────────────────────────────────

# Extension -> extractor. C-like is the catch-all for brace-struct languages.
_BY_EXT: dict[str, Callable[[str], list[tuple[str, set[str]]]]] = {
    ".rs": extract_c_like,
    ".c": extract_c_like, ".h": extract_c_like,
    ".cc": extract_c_like, ".cpp": extract_c_like, ".hpp": extract_c_like,
    ".glsl": extract_c_like, ".slang": extract_c_like,
    ".comp": extract_c_like, ".vert": extract_c_like, ".frag": extract_c_like,
    ".go": extract_go,
    ".py": extract_python,
}


def extract_all(text: str, ext: str) -> list[tuple[str, set[str]]]:
    """Extract (name, field-names) records from *text* for file extension *ext*.

    Unknown extensions fall back to the C-like brace-struct parser (harmless on
    languages without that shape — it simply finds nothing).
    """
    fn = _BY_EXT.get(ext.lower(), extract_c_like)
    return fn(text)
