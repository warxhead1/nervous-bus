"""Small JSON Schema subset validator (stdlib only), plus record validation."""
import json
import os
import re

CONTRACT = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "contract")
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is(v, t):
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, _TYPES[t])


def validate(v, s, path="$"):
    """Return a list of error strings."""
    errs = []
    if "const" in s and v != s["const"]:
        errs.append(f"{path}: expected {s['const']!r}")
    if "enum" in s and v not in s["enum"]:
        errs.append(f"{path}: {v!r} not in enum")
    if "type" in s:
        ts = s["type"] if isinstance(s["type"], list) else [s["type"]]
        if not any(_is(v, t) for t in ts):
            return errs + [f"{path}: expected {'|'.join(ts)}"]
    if isinstance(v, str):
        if "pattern" in s and not re.search(s["pattern"], v):
            errs.append(f"{path}: {v!r} does not match {s['pattern']}")
        if len(v) < s.get("minLength", 0):
            errs.append(f"{path}: too short")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in s and v < s["minimum"]:
            errs.append(f"{path}: below minimum")
    if isinstance(v, dict):
        for k in s.get("required", []):
            if k not in v:
                errs.append(f"{path}: missing {k}")
        props = s.get("properties", {})
        for k, sub in props.items():
            if k in v:
                errs += validate(v[k], sub, f"{path}.{k}")
        if s.get("additionalProperties") is False:
            for k in v:
                if k not in props:
                    errs.append(f"{path}: unexpected {k}")
        elif isinstance(s.get("additionalProperties"), dict):
            for k in v:
                if k not in props:
                    errs += validate(v[k], s["additionalProperties"], f"{path}.{k}")
    if isinstance(v, list) and "items" in s:
        for i, x in enumerate(v):
            errs += validate(x, s["items"], f"{path}[{i}]")
    return errs


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


_cache = {}


def envelope_schema():
    if "env" not in _cache:
        _cache["env"] = _load(os.path.join(CONTRACT, "envelope.v1.json"))
    return _cache["env"]


def body_schema(rtype):
    key = "body:" + rtype
    if key not in _cache:
        p = os.path.join(CONTRACT, "bodies", rtype + ".json")
        _cache[key] = _load(p) if os.path.isfile(p) else None
    return _cache[key]


def validate_record(rec):
    errs = validate(rec, envelope_schema())
    if not errs:
        bs = body_schema(rec["type"])
        if bs:
            errs += validate(rec["body"], bs, "$.body")
    return errs
