"""detectors/skill_opportunity.py — Tier-1 skill_opportunity detector.

A run "should have" loaded a skill when it invoked a CLI that skill_map.toml
maps to that skill, but runs.features.skills (populated by skill_usage.py's
fold_skill_features from bus.agent.activity.v1 tool_call events — see that
module's docstring) contains none of the mapped skill's names.

skill_map.toml (adapters/reflex-recorder/skill_map.toml, sibling of this
detectors/ dir) is the single source of CLI->skill mappings; this module is
generic over that table and never hardcodes a mapping itself, so extending
coverage is a config edit, not a code change.

Algorithm
=========
1. Load skill_map.toml -> list of (skill, pattern, context_pattern|None).
2. For runs in [since_ts, now), pull every Bash tool_call's extracted command
   string (same `_extract_command`-shape helper as rebuild_cache_miss.py:
   tool_summary may be a bare string or a JSON object with a "command" key).
3. For each run, evaluate every mapping's pattern (+ context_pattern, if
   present, must ALSO match) against the run's Bash commands. A mapping
   "used" a CLI if ANY command in the run matches.
4. Parse runs.features (JSON) once per run; extract the set of skill names
   under features["skills"] (mechanism-agnostic — skill_tool OR file_read
   both count as "the skill was used", matching skill_usage.py's own
   feature shape).
5. For every mapping whose CLI was used but whose skill name is NOT in that
   run's skill-name set -> bypass hit for (project, skill).

Signature = the skill name alone (no project prefix, no run_id) — mirrors
user_correction's convention (issue #39's own AC: "Signature = skill name").
detector_hits.project still carries the project column for per-project
querying; the issues-table PK aggregates cross-project the same way
user_correction's does (both are themed/config-driven ground-truth signals,
not per-project pattern instances like worktree_leak).

Bypass rate reporting: occurrences / (CLI-used run count) is the number
`nightly_analysis.py`'s digest and the PR body report — computed by the
caller from detector_hits + a denominator query, not stored on the
candidate itself (the candidate only knows ITS OWN window's numerator).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Optional

from detectors.base import BaseDetector, PatternCandidate

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - CI pins 3.11+
    import tomli as tomllib  # type: ignore[no-redef]

DEFAULT_SKILL_MAP_PATH = Path(__file__).resolve().parent.parent / "skill_map.toml"


class SkillMapping:
    __slots__ = ("skill", "pattern", "context_pattern")

    def __init__(self, skill: str, pattern: str, context_pattern: Optional[str]):
        self.skill = skill
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.context_pattern = re.compile(context_pattern, re.IGNORECASE) if context_pattern else None

    def matches(self, command: str) -> bool:
        if not self.pattern.search(command):
            return False
        if self.context_pattern is not None and not self.context_pattern.search(command):
            return False
        return True


def load_skill_map(path: Path = DEFAULT_SKILL_MAP_PATH) -> list[SkillMapping]:
    """Load skill_map.toml. Returns [] if the file is missing/malformed —
    never raises, so a config typo degrades to "no mappings" rather than
    breaking the whole nightly synthesis pass.
    """
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    out: list[SkillMapping] = []
    for entry in data.get("map", []):
        skill = entry.get("skill")
        pattern = entry.get("pattern")
        if not skill or not pattern:
            continue
        try:
            out.append(SkillMapping(skill, pattern, entry.get("context_pattern")))
        except re.error:
            continue
    return out


def _extract_bash_command(raw_json: str) -> Optional[str]:
    """Extract the bash command string from a raw_json tool_call event.

    tool_summary may be a JSON object with a "command" key, or a plain
    string (mirrors detectors/rebuild_cache_miss.py::_extract_command).
    """
    try:
        d = json.loads(raw_json)
        data = d.get("data", d)
        if data.get("event") != "tool_call" or data.get("tool_name") != "Bash":
            return None
        ts_sum = data.get("tool_summary") or ""
        if not ts_sum:
            return None
        if isinstance(ts_sum, str) and ts_sum.startswith("{"):
            try:
                obj = json.loads(ts_sum)
                return obj.get("command") if isinstance(obj, dict) else ts_sum
            except json.JSONDecodeError:
                return ts_sum
        return str(ts_sum)
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return None


def _skill_names(features_json: str) -> set[str]:
    try:
        features = json.loads(features_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return set()
    skills = features.get("skills") if isinstance(features, dict) else None
    if not isinstance(skills, dict):
        return set()
    return set(skills.keys())


class SkillOpportunityDetector(BaseDetector):
    """See module docstring for the full algorithm."""

    DETECTOR_NAME = "skill_opportunity"

    # Overridable in tests so a fixture never touches the real filesystem.
    skill_map_path: Path = DEFAULT_SKILL_MAP_PATH

    def detect(
        self, conn: sqlite3.Connection, since_ts: Optional[str] = None
    ) -> list[PatternCandidate]:
        mappings = load_skill_map(self.skill_map_path)
        if not mappings:
            return []

        since_clause = "AND r.started >= ?" if since_ts else ""
        params: list = [since_ts] if since_ts else []
        rows = conn.execute(
            f"""
            SELECT re.run_id, r.project, r.features, re.raw_json
            FROM run_events re
            JOIN runs r ON r.run_id = re.run_id
            WHERE json_extract(re.raw_json, '$.data.event') = 'tool_call'
              AND json_extract(re.raw_json, '$.data.tool_name') = 'Bash'
              {since_clause}
            """,
            params,
        ).fetchall()
        if not rows:
            return []

        # run_id -> (project, features_json, set(command strings))
        by_run: dict[str, tuple[str, str, list[str]]] = {}
        for run_id, project, features_json, raw_json in rows:
            cmd = _extract_bash_command(raw_json)
            if not cmd:
                continue
            entry = by_run.get(run_id)
            if entry is None:
                by_run[run_id] = (project, features_json, [cmd])
            else:
                entry[2].append(cmd)

        # project -> skill -> set(run_id)  [bypass hits]
        hits: dict[str, dict[str, set[str]]] = {}
        # project -> skill -> set(run_id)  [any use of the mapped CLI, hit or not —
        # this is the denominator nightly_analysis / the PR body's bypass-rate
        # table divides by]
        cli_used: dict[str, dict[str, set[str]]] = {}

        for run_id, (project, features_json, commands) in by_run.items():
            skill_names = _skill_names(features_json)
            for mapping in mappings:
                used = any(mapping.matches(cmd) for cmd in commands)
                if not used:
                    continue
                cli_used.setdefault(project, {}).setdefault(mapping.skill, set()).add(run_id)
                if mapping.skill not in skill_names:
                    hits.setdefault(project, {}).setdefault(mapping.skill, set()).add(run_id)

        candidates: list[PatternCandidate] = []
        for project, by_skill in hits.items():
            for skill, run_ids in by_skill.items():
                run_ids_sorted = sorted(run_ids)
                denom = len(cli_used.get(project, {}).get(skill, set())) or len(run_ids_sorted)
                bypass_rate = round(len(run_ids_sorted) / denom, 4) if denom else None
                candidates.append(
                    PatternCandidate(
                        project=project,
                        pattern_name="skill_opportunity",
                        signature=skill,
                        detector=self.DETECTOR_NAME,
                        occurrences=len(run_ids_sorted),
                        evidence=[
                            f"skill={skill}",
                            f"project={project}",
                            f"bypass_runs={len(run_ids_sorted)}",
                            f"cli_used_runs={denom}",
                            f"bypass_rate={bypass_rate}",
                            f"run_ids={','.join(run_ids_sorted[:5])}",
                        ],
                        run_ids=run_ids_sorted,
                        extra={
                            "skill": skill,
                            "bypass_rate": bypass_rate,
                            "cli_used_runs": denom,
                            "remediation_rung": "inform",
                            "remediation_rung_justification": (
                                "skill_opportunity flags a bypass rate for a human/"
                                "reflex-ladder decision (raise the skill's own "
                                "discoverability, or fold its steps into a hook) — "
                                "it does not itself propose an automatable fix."
                            ),
                        },
                    )
                )
        return candidates
