"""workflow_dispatch.py — reconstruct per-agent dispatch events for Workflow-tool agents.

THE GAP this closes
===================
Agent/Task-tool dispatches fire Claude Code's PreToolUse hook, so claude-hook-fast
emits ``bus.agent.activity.v1`` with the dispatch prompt in ``tool_summary``. But the
Workflow tool's ``agent()`` spawns run under the workflow RUNTIME — they do NOT fire
PreToolUse or SubagentStart hooks (empirically: 0 bus events carry a wf_ agent id, and
0 events point a transcript_path into a wf_ agent transcript). The Workflow tool emits
ONE bus event for the whole run, never per agent. So per-agent dispatch prompts for
workflow agents are invisible to the live bus.

They are NOT, however, invisible on disk. Each workflow run writes:
  <proj>/<session>/subagents/workflows/wf_<id>/journal.jsonl        ({"type":"started","agentId":..})
  <proj>/<session>/subagents/workflows/wf_<id>/agent-<hex>.jsonl    (line 0 = the dispatch prompt)
These are complete across ALL phases (not just first-wave) and are never pruned. So the
right capture is a JOIN over the transcripts, not a lossy live hook. This module reads
them into per-agent dispatch records and (optionally) re-emits ``bus.workflow.agent.dispatch.v1``
so reflexarc — and any bus consumer — gets full per-agent visibility.

Agent transcript line 0 carries everything we need: ``message.content`` (the FULL prompt),
``slug`` (the agent() label, e.g. 'bound-impact' — facet-3 thought this was lost; it is not),
``sessionId`` (parent), ``cwd``, ``gitBranch``, ``timestamp``, ``agentId``.

Pure stdlib; read-only over the transcript tree.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

CHANNEL = "bus.workflow.agent.dispatch.v1"
DEFAULT_PROMPT_BOUND = 1000  # matches the Agent/Task tool_summary bound decision


def _projects_root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECTS_ROOT",
                               Path.home() / ".claude" / "projects"))


def _project_from_cwd(cwd: str) -> str:
    """Derive a project slug from an agent cwd (segment after '/projects/')."""
    parts = Path(cwd).parts
    if "projects" in parts:
        i = parts.index("projects")
        if i + 1 < len(parts):
            return parts[i + 1]
    return Path(cwd).name or "unknown"


@dataclass
class DispatchRecord:
    wf_run_id: str
    agent_id: str
    prompt: str                 # bounded
    prompt_chars: int           # full length
    transcript_path: str        # join target for the full prompt
    ts: str = ""
    slug: str = ""          # per-RUN human slug (shared across the run's agents), NOT a per-agent label
    agent_type: str = ""    # from agent-<hex>.meta.json (e.g. "workflow-subagent")
    parent_session_id: str = ""
    project: str = ""
    cwd: str = ""
    git_branch: str = ""
    model: str = ""

    def to_event(self) -> dict:
        ev = {
            "ts": self.ts,
            "event": "workflow_agent_dispatch",
            "wf_run_id": self.wf_run_id,
            "agent_id": self.agent_id,
            "slug": self.slug,
            "agent_type": self.agent_type,
            "parent_session_id": self.parent_session_id,
            "project": self.project,
            "cwd": self.cwd,
            "git_branch": self.git_branch,
            "prompt": self.prompt,
            "prompt_chars": self.prompt_chars,
            "transcript_path": self.transcript_path,
            "schema_version": "1",
        }
        if self.model:
            ev["model"] = self.model
        return {k: v for k, v in ev.items() if v not in ("", None)}


def _read_line0(path: Path) -> Optional[dict]:
    try:
        with path.open("r", errors="replace") as fh:
            first = fh.readline()
        return json.loads(first) if first.strip() else None
    except (OSError, ValueError):
        return None


def _prompt_text(message: object) -> str:
    """The dispatch prompt is the user message content; it may be a str or the
    Anthropic content-block list. Normalize to text."""
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = message
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text", ""))
            elif isinstance(block, str):
                out.append(block)
        return "\n".join(out)
    return ""


def record_from_transcript(wf_run_id: str, transcript: Path,
                           bound: int = DEFAULT_PROMPT_BOUND) -> Optional[DispatchRecord]:
    line0 = _read_line0(transcript)
    if not line0:
        return None
    prompt = _prompt_text(line0.get("message"))
    if not prompt:
        return None
    # agent-<hex>.meta.json sits beside the transcript; carries agentType.
    agent_type = ""
    meta = transcript.with_suffix(".meta.json")
    if meta.is_file():
        try:
            agent_type = (json.loads(meta.read_text(errors="replace")) or {}).get("agentType", "")
        except (OSError, ValueError):
            pass
    return DispatchRecord(
        wf_run_id=wf_run_id,
        agent_id=line0.get("agentId") or transcript.stem.replace("agent-", ""),
        prompt=prompt[:bound],
        prompt_chars=len(prompt),
        transcript_path=str(transcript),
        ts=line0.get("timestamp", ""),
        slug=line0.get("slug", ""),
        agent_type=agent_type,
        parent_session_id=line0.get("sessionId", ""),
        cwd=line0.get("cwd", ""),
        git_branch=line0.get("gitBranch", ""),
        project=_project_from_cwd(line0.get("cwd", "")),
    )


def iter_workflow_dirs(root: Optional[Path] = None) -> Iterator[Path]:
    root = root or _projects_root()
    # <proj>/<session>/subagents/workflows/wf_*/
    yield from sorted(root.glob("*/*/subagents/workflows/wf_*"))


def scan(root: Optional[Path] = None,
         bound: int = DEFAULT_PROMPT_BOUND,
         run_filter: Optional[str] = None) -> list[DispatchRecord]:
    """Every per-agent dispatch record across all workflow runs under *root*.

    Agents are discovered from agent-<hex>.jsonl transcripts (the authoritative
    per-agent source); journal.jsonl is used only to scope to genuinely-started
    agents and skip stray files.
    """
    records: list[DispatchRecord] = []
    for wf_dir in iter_workflow_dirs(root):
        wf_run_id = wf_dir.name
        if run_filter and run_filter not in wf_run_id:
            continue
        started = _started_agent_ids(wf_dir)
        for transcript in sorted(wf_dir.glob("agent-*.jsonl")):
            if transcript.name.endswith(".meta.json"):
                continue
            agent_id = transcript.stem.replace("agent-", "")
            if started and agent_id not in started:
                continue  # journal didn't record this agent as started
            rec = record_from_transcript(wf_run_id, transcript, bound)
            if rec:
                records.append(rec)
    return records


def _started_agent_ids(wf_dir: Path) -> set[str]:
    journal = wf_dir / "journal.jsonl"
    out: set[str] = set()
    if not journal.is_file():
        return out
    try:
        for ln in journal.read_text(errors="replace").splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if e.get("type") == "started" and e.get("agentId"):
                out.add(e["agentId"])
    except OSError:
        pass
    return out


def join_full_prompt(transcript_path: str) -> str:
    """The lossless prompt for deep analysis — line 0 of the agent transcript."""
    line0 = _read_line0(Path(transcript_path))
    return _prompt_text(line0.get("message")) if line0 else ""


def emit(records: list[DispatchRecord], *, dry_run: bool = True) -> int:
    """Publish each record on CHANNEL via the sanctioned `nervous publish` SDK.
    Returns the count emitted (or that WOULD be emitted under dry_run)."""
    for rec in records:
        payload = json.dumps(rec.to_event())
        if dry_run:
            continue
        subprocess.run(["nervous", "publish", CHANNEL, payload],
                       capture_output=True, text=True, timeout=10)
    return len(records)


def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Reconstruct workflow-agent dispatch events.")
    ap.add_argument("--emit", action="store_true", help="publish via nervous publish (default: dry-run)")
    ap.add_argument("--run", help="only this wf_ run id (substring match)")
    ap.add_argument("--bound", type=int, default=DEFAULT_PROMPT_BOUND)
    ap.add_argument("--root", help="override projects root")
    args = ap.parse_args(argv)
    recs = scan(Path(args.root) if args.root else None, bound=args.bound, run_filter=args.run)
    by_run: dict[str, int] = {}
    for r in recs:
        by_run[r.wf_run_id] = by_run.get(r.wf_run_id, 0) + 1
    print(f"workflow runs: {len(by_run)} | agent dispatches: {len(recs)}")
    for r in recs[:12]:
        clip = "" if r.prompt_chars <= args.bound else f" (+{r.prompt_chars - args.bound} clipped)"
        print(f"  {r.wf_run_id}  {r.agent_id[:12]}  slug={r.slug or '-':<24} "
              f"{r.prompt_chars:>6}c{clip}  {r.project}")
    n = emit(recs, dry_run=not args.emit)
    print(f"{'EMITTED' if args.emit else 'DRY-RUN (would emit)'}: {n} → {CHANNEL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(__import__("sys").argv[1:]))
