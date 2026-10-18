#!/usr/bin/env python3
"""steering_audit.py — measure our steering surface against fetched external guidance.

The mechanical half of the steering audit. Zero LLM calls. Reads:

  - the guidance snapshots guidance_watch.py keeps (GUIDANCE_DIR/sources),
    for numeric thresholds and the doc's failure-pattern list;
  - the steering surface on disk: the ~/.claude/CLAUDE.md import chain,
    project CLAUDE.md / AGENTS.md / .claude/rules, auto-memory MEMORY.md
    indexes, skills, and agent definitions;
  - tokens.db `instructions` (how often each file actually loaded, and at what
    token cost) and runs.db `detector_hits` (how often each failure pattern
    the doc names actually happened here).

and writes STEERING_DIGEST_DIR/latest.{md,json} plus a dated copy.

Findings are raised ONLY against limits read out of a fetched source
(guidance_sources.toml [[threshold]]). A pattern that stops matching is itself
a finding (`threshold_unresolved`): the doc's wording moved. Everything else is
a metric, ranked but not judged: emphasis density, dated-incident lines,
rules already marked ENFORCED by a hook.

The digest carries paths, line numbers, counts and source ids — never the text
of a steering rule. The judgment half (which line to cut, move, or convert) is
the saved `steering-audit` workflow, which reads this digest; its proposals go
through automation/steering_audit_publish.py.

    steering_audit.py [--days 7] [--out-dir D] [--json] [--md]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
SOURCES_TOML = HERE / "guidance_sources.toml"
CROSSWALK_TOML = HERE / "failure_crosswalk.toml"
GUIDANCE_DIR = HOME / ".cache" / "nervous-bus" / "reflex" / "guidance"
STEERING_DIGEST_DIR = HOME / ".cache" / "nervous-bus" / "reflex" / "steering-digest"
TOKENS_DB = HOME / ".cache" / "nervous-bus" / "reflex" / "tokens.db"
RUNS_DB = HOME / ".cache" / "nervous-bus" / "reflex" / "runs.db"
TOKEN_DIGEST = HOME / ".cache" / "nervous-bus" / "reflex" / "token-digest" / "latest.json"
CLAUDE_HOME = HOME / ".claude"
PROJECTS_ROOT = HOME / "projects"
CHARS_PER_TOKEN = 4  # same estimate as token_report.py

PROJECT_INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", ".claude/CLAUDE.md", "CLAUDE.local.md")
# Words that mark a line as emphasized. The doc: "If you emphasize many lines,
# none of them stands out." Counted, not judged — the doc gives no number.
EMPHASIS = re.compile(r"\b(IMPORTANT|NEVER|ALWAYS|MUST|CRITICAL|DO NOT|DON'T|ONLY|EVERY)\b")
DATED = re.compile(r"\b20\d\d-\d\d-\d\d\b")
ENFORCED = re.compile(r"\bENFORCED\b|\benforced by\b", re.I)
_IMPORT = re.compile(r"(?:^|(?<=\s))@([~./\w][^\s`)]*)")
_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN = re.compile(r"`[^`]*`")
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_PATTERN_HEAD = re.compile(r"^\*\s+\*\*(.+?)\*\*")


# ---------------------------------------------------------------- guidance

def _to_int(raw: str) -> int | None:
    raw = raw.replace(",", "").strip().lower()
    return int(raw) if raw.isdigit() else _WORDS.get(raw)


def thresholds(cfg: dict, gdir: Path) -> dict[str, dict]:
    out = {}
    for t in cfg.get("threshold", []):
        snap = gdir / "sources" / f"{t['source']}.md"
        rec = {"source": t["source"], "value": None, "line": None}
        if snap.exists():
            for i, line in enumerate(snap.read_text().splitlines(), 1):
                m = re.search(t["pattern"], line)
                if m:
                    rec.update(value=_to_int(m.group(1)), line=i)
                    break
        out[t["id"]] = rec
    return out


def failure_patterns(gdir: Path) -> list[dict]:
    """The bold pattern names under the best-practices failure-patterns heading."""
    snap = gdir / "sources" / "cc-best-practices.md"
    if not snap.exists():
        return []
    found, inside = [], False
    for i, line in enumerate(snap.read_text().splitlines(), 1):
        if line.startswith("## "):
            inside = "failure pattern" in line.lower()
            continue
        m = _PATTERN_HEAD.match(line) if inside else None
        if m:
            found.append({"name": m.group(1).strip().rstrip(".").lower(), "line": i})
    return found


def guidance_changes(gdir: Path, since: str) -> list[dict]:
    p = gdir / "changes.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("ts", "") >= since:
            out.append({k: rec.get(k) for k in ("ts", "id", "added_lines", "removed_lines",
                                                "diff", "pages_added", "pages_removed")
                        if rec.get(k) is not None})
    return out


def source_health(gdir: Path) -> list[dict]:
    p = gdir / "state.json"
    state = json.loads(p.read_text()) if p.exists() else {}
    return [{"id": k, "status": v.get("status"), "checked_at": v.get("checked_at"),
             "changed_at": v.get("changed_at")} for k, v in sorted(state.items())]


# ---------------------------------------------------------------- steering

def _imports(text: str, base: Path) -> list[Path]:
    """`@path` imports outside fenced blocks and code spans, resolved and existing."""
    out, fenced = [], False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        for m in _IMPORT.finditer(_CODE_SPAN.sub("", line)):
            raw = m.group(1).rstrip(".,;:")
            p = Path(raw).expanduser()
            p = p if p.is_absolute() else (base / p)
            if p.is_file():
                out.append(p.resolve())
    return out


def file_metrics(path: Path) -> dict:
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    return {"path": str(path), "lines": len(lines), "bytes": len(text.encode()),
            "est_tokens": len(text) // CHARS_PER_TOKEN,
            "emphasis_lines": sum(1 for l in lines if EMPHASIS.search(l)),
            "dated_lines": sum(1 for l in lines if DATED.search(l)),
            "enforced_lines": [i for i, l in enumerate(lines, 1) if ENFORCED.search(l)]}


def import_chain(root: Path, max_hops: int | None) -> dict:
    """Expand a root instruction file's import tree; hop depth from the root."""
    seen: dict[Path, int] = {}
    stack = [(root.resolve(), 0)]
    while stack:
        p, depth = stack.pop()
        if p in seen and seen[p] <= depth:
            continue
        seen[p] = depth
        for child in _imports(p.read_text(errors="replace"), p.parent):
            stack.append((child, depth + 1))
    files = [{**file_metrics(p), "hops": d} for p, d in sorted(seen.items(), key=lambda x: (x[1], str(x[0])))]
    return {"root": str(root), "files": files,
            "lines": sum(f["lines"] for f in files),
            "est_tokens": sum(f["est_tokens"] for f in files),
            "max_hops_seen": max(seen.values()),
            "beyond_max_hops": [f["path"] for f in files if max_hops is not None and f["hops"] > max_hops]}


def project_roots(projects_root: Path) -> list[Path]:
    """Instruction files Claude Code loads at a project root. Per cc-memory
    § AGENTS.md, AGENTS.md loads only when no CLAUDE.md/CLAUDE.local.md exists;
    when CLAUDE.md imports it, it is already part of that chain."""
    roots = []
    for d in sorted(p for p in projects_root.iterdir() if p.is_dir()) if projects_root.exists() else []:
        found = [d / f for f in PROJECT_INSTRUCTION_FILES if (d / f).is_file() and f != "AGENTS.md"]
        roots += found or ([d / "AGENTS.md"] if (d / "AGENTS.md").is_file() else [])
    return roots


_WORKTREE = re.compile(r"^(?P<home>.*?)/data2/worktrees/(?P<proj>[^/]+)/[^/]+/(?P<rest>.+)$")


def canonical_path(path: str) -> str:
    """Fold a worktree copy of an instruction file onto its ~/projects original,
    so loads from agent worktrees count toward the file they were copied from."""
    m = _WORKTREE.match(path)
    return f"{m['home']}/projects/{m['proj']}/{m['rest']}" if m else path


def unscoped_rules(projects_root: Path) -> list[dict]:
    """.claude/rules/*.md without `paths:` frontmatter load every session."""
    out = []
    for f in sorted(projects_root.glob("*/.claude/rules/**/*.md")) if projects_root.exists() else []:
        head = f.read_text(errors="replace")[:2000]
        scoped = head.startswith("---") and re.search(r"^paths:", head.split("---", 2)[1] if head.count("---") >= 2 else "", re.M)
        if not scoped:
            out.append(file_metrics(f))
    return out


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    fm, key, out = text[3:end] if end > 0 else "", None, {}
    for line in fm.splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s?(.*)$", line)
        if m:
            key = m.group(1)
            out[key] = m.group(2).strip().strip("'\"")
        elif key and line.startswith((" ", "\t")):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def skills(skill_root: Path) -> list[dict]:
    out = []
    for d in sorted(skill_root.iterdir()) if skill_root.exists() else []:
        f = d / "SKILL.md"
        if not f.is_file():
            continue
        text = f.read_text(errors="replace")
        fm = _frontmatter(text)
        listing = (fm.get("description", "") + " " + fm.get("when_to_use", "")).strip()
        out.append({"name": d.name, "path": str(f.resolve()), "lines": len(text.splitlines()),
                    "listing_chars": len(listing)})
    return out


def agent_definitions(agent_root: Path) -> list[dict]:
    out = []
    for f in sorted(agent_root.glob("*.md")) if agent_root.exists() else []:
        fm = _frontmatter(f.read_text(errors="replace"))
        out.append({"name": f.stem, "path": str(f), "has_tools": "tools" in fm,
                    "model": fm.get("model")})
    return out


def memory_indexes(claude_home: Path) -> list[dict]:
    return [file_metrics(p) for p in sorted((claude_home / "projects").glob("*/memory/MEMORY.md"))]


# ---------------------------------------------------------------- measured use

def _ro(db: Path) -> sqlite3.Connection | None:
    if not db.exists():
        return None
    try:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def instruction_loads(db: Path, since: str) -> dict[str, dict]:
    conn = _ro(db)
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            """SELECT i.path, COUNT(*), SUM(i.chars) FROM instructions i
               JOIN sessions s ON s.key = i.session_key
               WHERE s.started >= ? GROUP BY 1""", (since,)).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for p, n, c in rows:
        agg = out.setdefault(canonical_path(p), {"loads": 0, "load_tokens": 0})
        agg["loads"] += n
        agg["load_tokens"] += (c or 0) // CHARS_PER_TOKEN
    return out


def detector_counts(db: Path, since: str) -> dict[str, dict]:
    conn = _ro(db)
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            """SELECT detector, COUNT(*), COUNT(DISTINCT run_id) FROM detector_hits
               WHERE ts >= ? GROUP BY 1""", (since,)).fetchall()
        themes = conn.execute(
            """SELECT signature, COUNT(*) FROM detector_hits
               WHERE detector = 'user_correction' AND ts >= ? GROUP BY 1""", (since,)).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out = {d: {"hits": h, "runs": r} for d, h, r in rows}
    out["_themes"] = {t: n for t, n in themes}
    return out


def _rules_by_signal(rules_toml: Path) -> dict[str, list[str]]:
    """detector/theme name -> memory-global rule ids that claim to address it."""
    if not rules_toml.exists():
        return {}
    out: dict[str, list[str]] = {}
    for rid, r in tomllib.loads(rules_toml.read_text()).get("rule", {}).items():
        for sig in r.get("detectors", []) + r.get("themes", []):
            out.setdefault(sig, []).append(rid)
    return out


def crosswalk(patterns: list[dict], cw: dict, counts: dict, token_digest: Path,
              rules_toml: Path) -> list[dict]:
    token = {}
    if token_digest.exists():
        try:
            token = {c["id"]: c.get("iet") for c in json.loads(token_digest.read_text()).get("candidates", [])}
        except (ValueError, KeyError):
            token = {}
    by_signal = _rules_by_signal(rules_toml)
    themes = counts.get("_themes", {})
    rows = []
    for p in patterns:
        m = cw.get("pattern", {}).get(p["name"])
        if m is None:
            rows.append({"pattern": p["name"], "doc_line": p["line"], "mapped": False})
            continue
        dets = {d: counts.get(d, {}).get("hits", 0) for d in m.get("detectors", [])}
        ths = {t: themes.get(t, 0) for t in m.get("themes", [])}
        rows.append({"pattern": p["name"], "doc_line": p["line"], "mapped": True,
                     "detector_hits": dets, "theme_hits": ths,
                     "token_iet": {t: token.get(t) for t in m.get("token", [])},
                     "steering_lint": m.get("steering_lint", []),
                     "rules": sorted({r for s in list(dets) + list(ths) for r in by_signal.get(s, [])})})
    return rows


# ---------------------------------------------------------------- build

def build(days: int = 7, gdir: Path = GUIDANCE_DIR, claude_home: Path = CLAUDE_HOME,
          projects_root: Path = PROJECTS_ROOT, tokens_db: Path = TOKENS_DB,
          runs_db: Path = RUNS_DB, token_digest: Path = TOKEN_DIGEST,
          sources_toml: Path = SOURCES_TOML, crosswalk_toml: Path = CROSSWALK_TOML,
          last_audit: str | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    cfg = tomllib.loads(sources_toml.read_text())
    th = thresholds(cfg, gdir)
    tv = {k: v["value"] for k, v in th.items()}
    loads = instruction_loads(tokens_db, since)
    findings: list[dict] = []

    for tid, t in th.items():
        if t["value"] is None:
            findings.append({"rule": "threshold_unresolved", "threshold": tid, "source": t["source"]})

    def _cite(tid: str) -> dict:
        return {"threshold": tid, "limit": tv[tid], "source": th[tid]["source"], "source_line": th[tid]["line"]}

    chains = []
    roots = [claude_home / "CLAUDE.md"] if (claude_home / "CLAUDE.md").is_file() else []
    for root in roots + project_roots(projects_root):
        ch = import_chain(root, tv.get("import_max_hops"))
        for f in ch["files"]:
            f.update(loads.get(f["path"], {"loads": 0, "load_tokens": 0}))
        ch["loads"] = ch["files"][0]["loads"]
        chains.append(ch)
        # Only steering that actually ran in the window is judged; an unloaded
        # root (a vendored clone, a dormant repo) is listed but not a finding.
        if not ch["loads"] and root != claude_home / "CLAUDE.md":
            continue
        if tv.get("claude_md_target_lines") and ch["lines"] > tv["claude_md_target_lines"]:
            findings.append({"rule": "claude_md_over_target", "path": ch["root"], "value": ch["lines"],
                             "files_in_chain": len(ch["files"]), **_cite("claude_md_target_lines")})
        if ch["beyond_max_hops"]:
            findings.append({"rule": "import_beyond_max_hops", "path": ch["root"],
                             "files": ch["beyond_max_hops"], **_cite("import_max_hops")})

    mems = memory_indexes(claude_home)
    for m in mems:
        over_lines = tv.get("memory_index_lines") and m["lines"] > tv["memory_index_lines"]
        over_kb = tv.get("memory_index_kb") and m["bytes"] > tv["memory_index_kb"] * 1024
        if over_lines or over_kb:
            findings.append({"rule": "memory_index_truncated", "path": m["path"], "value": m["lines"],
                             "bytes": m["bytes"], **_cite("memory_index_lines"),
                             "limit_kb": tv.get("memory_index_kb")})

    sk = skills(claude_home / "skills")
    for s in sk:
        if tv.get("skill_md_lines") and s["lines"] > tv["skill_md_lines"]:
            findings.append({"rule": "skill_md_over_lines", "path": s["path"], "value": s["lines"],
                             **_cite("skill_md_lines")})
        if tv.get("skill_listing_desc_chars") and s["listing_chars"] > tv["skill_listing_desc_chars"]:
            findings.append({"rule": "skill_listing_truncated", "path": s["path"], "value": s["listing_chars"],
                             **_cite("skill_listing_desc_chars")})

    counts = detector_counts(runs_db, since)
    pats = failure_patterns(gdir)
    cw = tomllib.loads(crosswalk_toml.read_text()) if crosswalk_toml.exists() else {}
    xw = crosswalk(pats, cw, counts, token_digest, claude_home / "memory-global" / "rules.toml")
    for row in xw:
        if not row["mapped"]:
            findings.append({"rule": "failure_pattern_unmapped", "pattern": row["pattern"],
                             "source": "cc-best-practices", "source_line": row["doc_line"]})
    if not pats:
        findings.append({"rule": "failure_patterns_unparsed", "source": "cc-best-practices"})

    changes = guidance_changes(gdir, last_audit or since)
    for c in changes:
        findings.append({"rule": "guidance_changed", "source": c["id"], "ts": c["ts"],
                         "diff": c.get("diff"), "pages_added": len(c.get("pages_added", []))})

    global_chain = chains[0] if roots else None
    # What the workflow hands to `/doctor prompt-audit`: the global chain, every
    # project root that loaded in the window, the agent definitions, and any
    # skill a finding names. Directories audit everything beneath them.
    targets = [str(claude_home / "CLAUDE.md"), str(claude_home / "memory-global"),
               str(claude_home / "agents")] if roots else []
    targets += [c["root"] for c in chains[1 if roots else 0:] if c["loads"]]
    targets += sorted({f["path"] for f in findings if f["rule"].startswith("skill_")})
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_days": days,
        "changes_since": last_audit or since,
        "thresholds": th,
        "findings": findings,
        "global_chain": global_chain,
        "project_chains": [{k: c[k] for k in ("root", "lines", "est_tokens", "max_hops_seen", "loads")}
                           | {"files": len(c["files"])} for c in chains[1 if roots else 0:]],
        "memory_indexes": sorted(mems, key=lambda m: -m["lines"])[:15],
        "unscoped_rules": unscoped_rules(projects_root),
        "skills": {"count": len(sk), "largest": sorted(sk, key=lambda s: -s["lines"])[:10]},
        "agents_without_tools": [a["name"] for a in agent_definitions(claude_home / "agents") if not a["has_tools"]],
        "audit_targets": [t for t in targets if Path(t).exists()],
        "failure_crosswalk": xw,
        "guidance_changes": changes,
        "sources": source_health(gdir),
    }


def _short(p: str) -> str:
    return p.replace(str(HOME), "~", 1)


def to_markdown(rep: dict) -> str:
    L = [f"# Steering audit digest — {rep['generated_at']}", "",
         f"Window {rep['window_days']}d. Guidance changes since {rep['changes_since']}.", "",
         "## Findings (limits read from fetched guidance)", ""]
    if not rep["findings"]:
        L.append("None.")
    for f in rep["findings"]:
        where = _short(f.get("path", "")) or f.get("source", "") or f.get("pattern", "")
        lim = f" > {f['limit']} ({f['source']}:{f['source_line']})" if "limit" in f else ""
        val = f" = {f['value']}" if "value" in f else ""
        L.append(f"- `{f['rule']}` {where}{val}{lim}")
    g = rep.get("global_chain")
    if g:
        L += ["", f"## Global always-loaded chain: {g['lines']} lines, ~{g['est_tokens']} tokens, "
              f"{len(g['files'])} files, max hops {g['max_hops_seen']}", "",
              "| file | hops | lines | ~tokens | loads | load tokens | emphasis lines | dated lines | ENFORCED lines |",
              "|---|---|---|---|---|---|---|---|---|"]
        for f in sorted(g["files"], key=lambda f: -f["est_tokens"]):
            L.append(f"| {_short(f['path'])} | {f['hops']} | {f['lines']} | {f['est_tokens']} | {f['loads']} | "
                     f"{f['load_tokens']} | {f['emphasis_lines']} | {f['dated_lines']} | "
                     f"{','.join(map(str, f['enforced_lines'])) or '-'} |")
    L += ["", "## Project instruction chains (loads in window; unloaded roots are not judged)", "",
          "| root | files | lines | ~tokens | loads |", "|---|---|---|---|---|"]
    for c in sorted(rep["project_chains"], key=lambda c: (-c["loads"], -c["lines"])):
        L.append(f"| {_short(c['root'])} | {c['files']} | {c['lines']} | {c['est_tokens']} | {c['loads']} |")
    L += ["", "## Doc failure patterns -> measured here", "",
          "| pattern | detector hits | correction themes | token IET | rules claiming it |", "|---|---|---|---|---|"]
    for r in rep["failure_crosswalk"]:
        if not r["mapped"]:
            L.append(f"| {r['pattern']} | UNMAPPED | | | |")
            continue
        fmt = lambda d: ", ".join(f"{k}={v}" for k, v in d.items()) or "-"
        L.append(f"| {r['pattern']} | {fmt(r['detector_hits'])} | {fmt(r['theme_hits'])} | "
                 f"{fmt(r['token_iet'])} | {', '.join(r['rules']) or '-'} |")
    L += ["", "## Largest MEMORY.md indexes", "", "| path | lines | bytes |", "|---|---|---|"]
    L += [f"| {_short(m['path'])} | {m['lines']} | {m['bytes']} |" for m in rep["memory_indexes"][:8]]
    if rep["agents_without_tools"]:
        L += ["", f"Agent definitions with no `tools:` (inherit every tool): {', '.join(rep['agents_without_tools'])}"]
    if rep.get("prior_proposals"):
        L += ["", "## Prior proposals (target lines at filing -> now)", "",
              "| filed | title | target | action | lines | issue |", "|---|---|---|---|---|---|"]
        L += [f"| {r['filed']} | {r['title']} | {_short(r['target_path'] or '')} | {r['action']} | "
              f"{r['lines_at_filing']} -> {r['lines_now']} | {r.get('issue') or r.get('bead') or '-'} |"
              for r in rep["prior_proposals"]]
    L += ["", "## Guidance sources", "", "| id | status | changed_at |", "|---|---|---|"]
    L += [f"| {s['id']} | {s['status']} | {s['changed_at']} |" for s in rep["sources"]]
    return "\n".join(L) + "\n"


def prior_proposals(out_dir: Path, limit: int = 20) -> list[dict]:
    """Filed proposals with the target's line count then vs now: did it move?"""
    p = out_dir / "proposals.jsonl"
    rows = []
    for line in (p.read_text().splitlines() if p.exists() else []):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        t = Path(r.get("target_path", ""))
        rows.append({k: r.get(k) for k in ("filed", "title", "target_path", "action", "issue", "bead")}
                    | {"lines_at_filing": r.get("target_lines_at_filing"),
                       "lines_now": len(t.read_text(errors="replace").splitlines()) if t.is_file() else None})
    return rows[-limit:]


def _last_audit(out_dir: Path) -> str | None:
    """When a steering-audit run last went through the publish gate for real
    (steering_audit_publish.py stamps `last_audit`; dry runs do not). NOT
    latest.json, which the nightly rewrites every morning and would hide every
    change older than a few hours."""
    try:
        return (out_dir / "last_audit").read_text().strip() or None
    except OSError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--out-dir", type=Path, default=STEERING_DIGEST_DIR)
    ap.add_argument("--json", action="store_true", help="print the JSON digest to stdout")
    ap.add_argument("--md", action="store_true", help="print the markdown digest to stdout")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()
    rep = build(a.days, last_audit=_last_audit(a.out_dir))
    rep["prior_proposals"] = prior_proposals(a.out_dir)
    md = to_markdown(rep)
    if not a.no_write:
        a.out_dir.mkdir(parents=True, exist_ok=True)
        day = rep["generated_at"][:10]
        for name in ("latest", day):
            (a.out_dir / f"{name}.json").write_text(json.dumps(rep, indent=2) + "\n")
            (a.out_dir / f"{name}.md").write_text(md)
    if a.json:
        print(json.dumps(rep, indent=2))
    elif a.md:
        print(md)
    else:
        print(f"steering-audit: {len(rep['findings'])} findings -> {a.out_dir}/latest.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
