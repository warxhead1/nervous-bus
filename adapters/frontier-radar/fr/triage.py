"""triage: typed classification through a configurable command. Low confidence goes to the review lane."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

from .store import Store

HERE = Path(__file__).resolve().parent.parent
DEFAULT_CMD = ('claude -p --model haiku --output-format json --tools "" --strict-mcp-config '
               '--disable-slash-commands --no-session-persistence')
DEFAULT_MIN_CONFIDENCE = 0.6
TIMEOUT_S = 180


def load_schema(path: Path = HERE / "triage.schema.json") -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate(obj, schema: dict, path: str = "$") -> list[str]:
    """Small JSON Schema subset: type, enum, minimum, maximum, maxLength, required, properties, additionalProperties."""
    errs = []
    types = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "object": dict, "array": list}
    t = schema.get("type")
    if t:
        ok = isinstance(obj, types[t]) and not (t in ("number", "integer") and isinstance(obj, bool))
        if not ok:
            return [f"{path}: expected {t}"]
    if "enum" in schema and obj not in schema["enum"]:
        errs.append(f"{path}: {obj!r} not in {schema['enum']}")
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if "minimum" in schema and obj < schema["minimum"]:
            errs.append(f"{path}: below minimum")
        if "maximum" in schema and obj > schema["maximum"]:
            errs.append(f"{path}: above maximum")
    if isinstance(obj, str) and "maxLength" in schema and len(obj) > schema["maxLength"]:
        errs.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(obj, dict):
        for r in schema.get("required", []):
            if r not in obj:
                errs.append(f"{path}: missing {r}")
        props = schema.get("properties", {})
        for k, v in obj.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: unexpected {k}")
    return errs


def build_prompt(template: str, profile: str, schema: dict, event: dict, sightings: list[dict]) -> str:
    item = {
        "title": event["title"], "url": event["url"], "summary": event["summary"],
        "published_at": event["published_at"], "registry_domains": event["domains"],
        "seen_by": [{"source": s["source_id"], "role": s["role"], "engagement": s["engagement"]} for s in sightings],
    }
    return (template.replace("{{profile}}", profile.strip())
            .replace("{{schema}}", json.dumps(schema, indent=1))
            .replace("{{item}}", json.dumps(item, indent=1, ensure_ascii=False)))


def extract_json(stdout: str) -> dict:
    """Accept raw JSON, the claude -p envelope ({"result": "..."}), or JSON inside a code fence."""
    data = json.loads(stdout)
    if isinstance(data, list):
        finals = [e for e in data if isinstance(e, dict) and e.get("type") == "result"]
        if not finals:
            raise ValueError("no result event in claude output")
        data = finals[-1]
    if isinstance(data, dict) and "result" in data and isinstance(data["result"], str) and "claim" not in data:
        text = data["result"]
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
        if not m:
            raise ValueError("no JSON object in model result")
        return json.loads(m.group(1))
    if isinstance(data, dict) and isinstance(data.get("structured_output"), dict):
        return data["structured_output"]
    return data


def run_command(cmd: str, prompt: str, timeout: int = TIMEOUT_S) -> str:
    p = subprocess.run(shlex.split(cmd), input=prompt, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"triage command exit {p.returncode}: {p.stderr.strip()[:200]}")
    return p.stdout


def lane_for(result: dict, min_confidence: float) -> str:
    if result["confidence"] < min_confidence:
        return "review"
    return "dropped" if result["action"] == "ignore" else "digest"


def triage(store: Store, now: datetime, cmd: str | None = None, limit: int = 25,
           min_confidence: float = DEFAULT_MIN_CONFIDENCE, profile_path: Path | None = None,
           prompt_path: Path | None = None, runner=run_command, publish=None) -> dict:
    cmd = cmd or os.environ.get("FRONTIER_RADAR_TRIAGE_CMD") or DEFAULT_CMD
    profile = (profile_path or HERE / "profile.md").read_text(encoding="utf-8")
    template = (prompt_path or HERE / "triage_prompt.md").read_text(encoding="utf-8")
    schema = load_schema()
    counts = {"digest": 0, "review": 0, "dropped": 0, "errors": 0}
    for ev in store.events("status='new'", order="score DESC LIMIT %d" % limit):
        key = ev["event_key"]
        prompt = build_prompt(template, profile, schema, ev, store.sightings(key))
        try:
            result = extract_json(runner(cmd, prompt))
            errs = validate(result, schema)
            if errs:
                raise ValueError("; ".join(errs[:3]))
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            counts["errors"] += 1
            store.update_event(key, triage={"error": str(e)[:300]})
            continue
        lane = lane_for(result, min_confidence)
        store.update_event(key, status="triaged", lane=lane, triage=result, triaged_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        counts[lane] += 1
        if publish:
            publish(store.event(key), "triaged")
    return counts
