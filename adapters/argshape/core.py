"""argshape core: plan corrections before a call, learn after it.

Golden rule: a token is only ever rewritten if THIS build of the binary (or
THIS database connection) has already rejected exactly that token, and a
replacement is known with enough confidence. Everything else is advice or
silence. Nothing here blocks a command.

Confidence ladder for a replacement (fix):
  tool-hint     the tool itself printed the suggestion           n >= 1
  pg-hint       postgres HINT "Perhaps you meant ..."            n >= 1
  spec-unique   learned --help has exactly one obvious match     n >= 1
  catalog       learned column list has exactly one close match  n >= 1
  session-pair  an agent's next successful call swapped it       sessions >= min_pair_sessions,
                                                                  or 1 when --help corroborates
A fix is demoted to advice forever once a corrected command fails on the
same token, the agent opts out right after a correction, or failures
outnumber successes.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time

import errclass
import helpspec
import segment
import sqlshape
import store

PAIR_WINDOW_S = 15 * 60
SQL_BINS = re.compile(r"(^|/)(psql|pgcli|sqlite3?|duckdb|mysql|mariadb|clickhouse(-client)?|[\w.-]*(db|sql|psql)[\w.-]*\.sh)$", re.I)
NUMERIC_OPT = re.compile(r"^-\d+$")
CHANNEL = "agent.cli.correction.v1"
# Failures where the command never ran (a hook or the user refused it) teach
# nothing about the command's arguments.
NOT_RUN = re.compile(r"^(?:Exit code \d+\n)?(?:PreToolUse:\w+ hook error|The user doesn't want to proceed|"
                     r"Permission to use \w+ with command|Hook PreToolUse)", re.M)


# ---------------------------------------------------------------- identity

def bin_identity(db, name: str, cwd: str) -> tuple[str, str | None]:
    """(bin_key, path). bin_key changes whenever the binary file changes."""
    alias = store.meta_get(db, f"alias:{name}") if db else ""
    target = alias or name
    if "/" in target:
        path = os.path.normpath(os.path.join(cwd or "/", target))
    else:
        path = shutil.which(target)
    if not path:
        return f"name:{target}", None
    try:
        st = os.stat(path)
    except OSError:
        return f"name:{target}", None
    return f"{st.st_dev}:{st.st_ino}:{st.st_mtime_ns}:{st.st_size}", path


def option_words(seg, spec=None):
    """Yield (index, name, word) for option-looking args; stops at `--`."""
    args = seg.args
    skip_next = False
    for i, w in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        v = w.value
        if v == "--":
            return
        if not v.startswith("-") or v == "-" or w.dynamic:
            continue
        name = v.split("=", 1)[0] if v.startswith("--") else v
        if spec and spec.opts.get(name) and "=" not in v:
            skip_next = True
        yield i + 1, name, w


def positionals(seg, spec=None):
    args = seg.args
    skip_next = False
    for i, w in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        v = w.value
        if v == "--":
            break
        if v.startswith("-") and v != "-":
            name = v.split("=", 1)[0]
            if spec and spec.opts.get(name) and "=" not in v:
                skip_next = True
            continue
        yield i + 1, w


def context(db, bin_key: str, seg):
    """(ctx, spec) — ctx is the subcommand path (up to two levels) if any."""
    spec = store.get_spec(db, bin_key, "")
    ctx = ""
    cur = spec
    pos = [w.value for _, w in positionals(seg, spec)]
    for level in range(2):
        if not cur or not cur.subcmds or level >= len(pos) or pos[level] not in cur.subcmds:
            break
        ctx = (ctx + " " + pos[level]).strip()
        nxt = store.get_spec(db, bin_key, ctx)
        if not nxt:
            break
        cur = nxt
    return ctx, (store.get_spec(db, bin_key, ctx) if ctx else spec) or spec


# ---------------------------------------------------------------- fixes

def fix_eligible(fix: dict, spec, cfg: dict) -> bool:
    if fix["demoted"]:
        return False
    if fix["n_bad"] > fix["n_ok"] and fix["n_bad"] > 0:
        return False
    if fix["kind"] == "flag" and spec and spec.opts:
        if fix["bad"] in spec.opts or fix["good"].split()[0] not in spec.opts:
            return False
    if fix["kind"] == "subcmd" and spec and spec.subcmds and fix["good"] not in spec.subcmds:
        return False
    src = fix["source"]
    if src in ("tool-hint", "pg-hint", "spec-unique", "catalog", "shape"):
        return fix["n"] >= 1
    if src == "session-pair":
        sessions = len(json.loads(fix["sessions"] or "[]"))
        corroborated = bool(spec and fix["good"] in spec.opts and fix["bad"] not in spec.opts)
        return sessions >= int(cfg.get("min_pair_sessions", 2)) or corroborated
    return False


def best_fix(db, scope: str, ctx: str, kind: str, bad: str, spec, cfg: dict):
    rows = db.execute(
        "SELECT scope, ctx, kind, bad, good, source, n, sessions, n_applied, n_ok, n_bad, demoted "
        "FROM fixes WHERE scope=? AND ctx IN (?, '') AND kind=? AND bad=?",
        (scope, ctx, kind, bad)).fetchall()
    cols = ("scope", "ctx", "kind", "bad", "good", "source", "n", "sessions",
            "n_applied", "n_ok", "n_bad", "demoted")
    fixes = [dict(zip(cols, r)) for r in rows]
    rank = {"tool-hint": 0, "pg-hint": 0, "spec-unique": 1, "catalog": 1, "shape": 1, "session-pair": 2}
    fixes.sort(key=lambda f: (f["ctx"] != ctx, rank.get(f["source"], 9), -f["n"]))
    eligible = [f for f in fixes if fix_eligible(f, spec, cfg)]
    # Two different eligible targets for one bad token = ambiguity: advise only.
    if len({f["good"] for f in eligible}) > 1:
        return None, fixes
    return (eligible[0] if eligible else None), fixes


def rejected(db, bin_key: str, ctx: str, kind: str, token: str) -> bool:
    return db.execute("SELECT 1 FROM rejects WHERE bin_key=? AND ctx IN (?, '') AND kind=? AND token=?",
                      (bin_key, ctx, kind, token)).fetchone() is not None


def breaker_open(db) -> bool:
    try:
        return float(store.meta_get(db, "breaker_until", "0")) > time.time()
    except ValueError:
        return False


# ---------------------------------------------------------------- pre

def plan(db, cfg: dict, command: str, cwd: str) -> dict:
    """Decide corrections/advice for a command. Pure apart from DB reads."""
    edits: list[tuple[int, int, str]] = []
    notes: list[str] = []
    applied: list[dict] = []
    _check_breaker(db, cfg)
    if breaker_open(db):
        cfg = {**cfg, "mode": min(cfg.get("mode", "correct"), "advise", key=store.MODES.index)}
    for seg in segment.parse(command):
        name = seg.bin
        if not name or seg.words[0].dynamic:
            continue
        base = os.path.basename(name)
        mode = store.effective_mode(cfg, base)
        if mode in ("off", "observe"):
            continue
        bin_key, _ = bin_identity(db, name, cwd)
        ctx, spec = context(db, bin_key, seg)
        label = base + (f" {ctx}" if ctx else "")
        # flags
        for _, opt, w in option_words(seg, spec):
            num = NUMERIC_OPT.match(opt)
            if num and not _numeric_invalid_context(seg, opt):
                continue      # `| tail -5` and `tail -5 file` are valid; only mixed use fails
            key_tok = "-#" if num else opt      # `-N` lessons generalize over N
            if not rejected(db, bin_key, ctx, "flag", key_tok):
                continue
            fix, all_fixes = best_fix(db, bin_key, ctx, "flag", key_tok, spec, cfg)
            if fix and num:
                fix = {**fix, "good": fix["good"].replace("#", opt[1:])}
            rewritable = w.raw.startswith(opt) and not w.quoted
            if fix and mode == "correct" and rewritable:
                edits.append((w.start, w.start + len(opt), fix["good"]))
                applied.append({**fix, "label": label})
                notes.append(f"`{label} {opt}` -> `{fix['good']}` ({fix['source']})")
            else:
                near = helpspec.nearest(opt, spec.opts) if spec else []
                cand = fix["good"] if fix else (all_fixes[0]["good"] if all_fixes else None)
                notes.append(f"advice: this build of `{label}` rejected `{opt}` before"
                             + (f"; likely `{cand}`" if cand else "")
                             + (f"; nearest valid: {', '.join(near)}" if near and not cand else ""))
        # subcommand
        pos = list(positionals(seg, store.get_spec(db, bin_key, "")))
        if pos:
            idx, w = pos[0]
            if rejected(db, bin_key, "", "subcmd", w.value):
                root = store.get_spec(db, bin_key, "")
                fix, _ = best_fix(db, bin_key, "", "subcmd", w.value, root, cfg)
                if fix and mode == "correct" and not w.quoted:
                    edits.append((w.start, w.end, fix["good"]))
                    applied.append({**fix, "label": base})
                    notes.append(f"`{base} {w.value}` -> `{base} {fix['good']}` ({fix['source']})")
                elif root and root.subcmds:
                    near = helpspec.nearest(w.value, root.subcmds)
                    notes.append(f"advice: `{base}` has no subcommand `{w.value}`"
                                 + (f"; nearest: {', '.join(near)}" if near else ""))
        # SQL identifiers and literal shapes
        for sql, s0, s1, is_stdin in sqlshape.find_sql(seg):
            smode = store.effective_mode(cfg, base, sql=True)
            if smode in ("off", "observe"):
                continue
            conn = sqlshape.conn_key(seg, cwd)
            raw = command[s0:s1]
            new_raw = raw
            for ident in sorted(sqlshape.identifiers(sql), key=len, reverse=True):
                col = ident.split(".")[-1]
                if not rejected(db, conn, "", "column", col.lower()):
                    continue
                fix, all_fixes = best_fix(db, conn, "", "column", col.lower(), None, cfg)
                if fix and smode == "correct" and re.search(r"(?<![\w$])" + re.escape(col) + r"(?![\w(])", new_raw):
                    new_raw = sqlshape.replace_ident(new_raw, col, fix["good"])
                    applied.append({**fix, "label": f"{base} sql"})
                    notes.append(f"SQL column `{col}` -> `{fix['good']}` ({fix['source']})")
                else:
                    cols = known_columns(db, conn, sqlshape.relations(sql))
                    near = helpspec.nearest(col.lower(), cols) if cols else []
                    notes.append(f"advice: column `{col}` was rejected on this connection before"
                                 + (f"; nearest known: {', '.join(near)}" if near else ""))
            for col, lit in sqlshape.literal_comparisons(sql):
                row = db.execute("SELECT family, variant FROM shapes WHERE conn=? AND col=?",
                                 (conn, col)).fetchone()
                if not row:
                    continue
                conv = sqlshape.convert(lit, row[0], row[1])
                if not conv:
                    continue
                needle = f"'{lit}'"
                if smode == "correct" and new_raw.count(needle) == 1:
                    new_raw = new_raw.replace(needle, f"'{conv}'")
                    applied.append({"scope": conn, "ctx": col, "kind": "shape", "bad": lit,
                                    "good": conv, "source": "shape", "label": f"{base} sql"})
                    notes.append(f"`{col}` values are {row[0]} {row[1]}: '{lit}' -> '{conv}'")
                else:
                    notes.append(f"advice: `{col}` values are {row[0]} {row[1]}; '{lit}' would match as '{conv}'")
            if new_raw != raw:
                edits.append((s0, s1, new_raw))
    new_command = command
    for a, b, rep in sorted(edits, key=lambda e: e[0], reverse=True):
        new_command = new_command[:a] + rep + new_command[b:]
    return {"command": new_command, "notes": notes, "applied": applied}


def _numeric_invalid_context(seg, opt: str) -> bool:
    """GNU head/tail reject obsolete `-N` only beside other options or with 2+ files."""
    others = [n for _, n, _ in option_words(seg) if n != opt]
    files = [w for _, w in positionals(seg)]
    return bool(others) or len(files) >= 2


def known_columns(db, conn: str, rels: list[str]) -> list[str]:
    cols: list[str] = []
    for rel in rels or [""]:
        for (c,) in db.execute("SELECT cols FROM columns WHERE conn=? AND (relation=? OR ?='')",
                               (conn, rel.split(".")[-1], rel)):
            cols += [x for x in json.loads(c) if x not in cols]
    return cols


def record_applied(db, session: str, tool_use_id: str, before: str, after: str, applied: list[dict]) -> None:
    db.execute("BEGIN")
    try:
        _record_applied(db, session, tool_use_id, before, after, applied)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def _record_applied(db, session, tool_use_id, before, after, applied) -> None:
    db.execute("INSERT OR REPLACE INTO applied VALUES (?,?,?,?,?,?,NULL)",
               (tool_use_id or f"t{time.time()}", session, time.time(),
                json.dumps([{k: a[k] for k in ("scope", "ctx", "kind", "bad", "good")} for a in applied]),
                before[:2000], after[:2000]))
    for a in applied:
        db.execute("UPDATE fixes SET n_applied=n_applied+1 WHERE scope=? AND ctx=? AND kind=? AND bad=? AND good=?",
                   (a["scope"], a["ctx"], a["kind"], a["bad"], a["good"]))


def register_optout(db, session: str) -> list[dict]:
    """The agent re-ran with `argshape:off` — demote what we just changed."""
    row = db.execute("SELECT tool_use_id, fixes FROM applied WHERE session=? AND ts > ? "
                     "AND (outcome IS NULL OR outcome='ok') ORDER BY ts DESC LIMIT 1",
                     (session, time.time() - 600)).fetchone()
    if not row:
        return []
    fixes = json.loads(row[1])
    for f in fixes:
        _demote(db, f, "opt-out")
    db.execute("UPDATE applied SET outcome='reverted' WHERE tool_use_id=?", (row[0],))
    return fixes


def _demote(db, f: dict, why: str) -> None:
    if f.get("kind") == "shape":
        # ctx carries the column for shape corrections.
        db.execute("DELETE FROM shapes WHERE conn=? AND col=?", (f["scope"], f["ctx"]))
        store.meta_set(db, f"shape_off:{f['scope']}:{f['ctx']}", why)
        emit({"event": "demotion", "kind": "shape", "bad": f["bad"], "good": f["good"], "reason": why})
        return
    db.execute("UPDATE fixes SET demoted=1, n_bad=n_bad+1 WHERE scope=? AND ctx=? AND kind=? AND bad=? AND good=?",
               (f["scope"], f["ctx"], f["kind"], f["bad"], f["good"]))
    emit({"event": "demotion", "kind": f["kind"], "bad": f["bad"], "good": f["good"], "reason": why})


# ---------------------------------------------------------------- post

def attribute(rej, segs, db, cwd):
    """The single segment a rejection belongs to, or None when ambiguous."""
    cands = []
    for seg in segs:
        if not seg.bin:
            continue
        if rej.kind in ("flag", "missing_arg", "dash_value"):
            tok = rej.token
            hit = False
            for _, name, w in option_words(seg):
                if name == tok or w.value == tok:
                    hit = True
                elif len(tok) == 2 and tok[0] == "-" and not name.startswith("--") and tok[1] in name[1:]:
                    hit = True
                elif tok.startswith("--") and name == "-" + tok[2:]:
                    hit = True    # git prints `limt` for both -limt and --limt
            if hit:
                cands.append(seg)
        elif rej.kind == "subcmd":
            if any(w.value == rej.token for _, w in positionals(seg)):
                cands.append(seg)
        elif rej.kind in ("column", "relation"):
            col = rej.token.split(".")[-1]
            if not (SQL_BINS.search(seg.bin) or any(
                    w.value in sqlshape.SQL_ARG_OPTS for w in seg.args)):
                continue      # `echo "select ..."` is not where the error came from
            for sql, *_ in sqlshape.find_sql(seg):
                if col in sqlshape.identifiers(sql) or col.lower() in sql.lower():
                    cands.append(seg)
                    break
    if len(cands) > 1 and len({os.path.basename(c.bin) for c in cands}) == 1:
        cands = cands[:1]     # the same program twice: one rejection, one lesson
    if len(cands) > 1 and rej.prog:
        named = [s for s in cands if os.path.basename(s.bin) == rej.prog]
        if named:
            cands = named
    if len(cands) == 1:
        seg = cands[0]
        if rej.prog and os.path.basename(seg.bin) != rej.prog and shutil.which(rej.prog) \
                and rej.kind in ("flag", "subcmd", "missing_arg") and "/" not in seg.bin:
            # A shell function/alias forwards this name to another program
            # (CC's `grep` is ugrep). Learn that once, key on the real binary.
            store.meta_set(db, f"alias:{os.path.basename(seg.bin)}", rej.prog)
        return seg
    return None


def on_failure(db, cfg: dict, ev: dict) -> list[str]:
    """Learn from a failed Bash call; return advisory notes for the agent."""
    command = (ev.get("tool_input") or {}).get("command", "")
    text = ev.get("error") or ""
    if isinstance(text, (list, dict)):
        text = json.dumps(text)
    cwd = ev.get("cwd") or os.getcwd()
    session = ev.get("session_id") or ""
    segs = segment.parse(command)
    notes: list[str] = []
    if NOT_RUN.search(text[:400]):
        return notes
    _settle_applied(db, cfg, ev, failed=True, text=text)
    rejs = errclass.classify(text)
    attributed = []
    for rej in rejs:
        seg = attribute(rej, segs, db, cwd)
        if seg is not None:
            attributed.append((rej, seg))
    if not attributed:
        last = segs[-1] if segs else None
        if last and last.bin:
            cls = "usage" if errclass.looks_like_usage(text) else (
                "notfound" if re.search(r"command not found|No such file", text) else "other")
            store.upsert_count(db, "sigs", {"name": os.path.basename(last.bin), "ctx": "",
                                            "err_class": cls, "sig": errclass.signature(text)},
                               {"bin_key": "", "via": " > ".join(last.via)})
        return notes
    for rej, seg in attributed:
        if rej.kind == "dash_value":
            name = os.path.basename(seg.bin)
            store.upsert_count(db, "sigs", {"name": name, "ctx": "", "err_class": "dash_value",
                                            "sig": errclass.signature(text)}, {"bin_key": "", "via": " > ".join(seg.via)})
            notes.append(f"argshape: `{name}` parsed `{rej.token}` as an option because it starts with `-`. "
                         f"Pass it as `-e '{rej.token}'` (grep family) or put it after `--`.")
        elif rej.kind in ("flag", "subcmd", "missing_arg"):
            notes += _learn_arg_rejection(db, cfg, rej, seg, cwd, session, text)
        else:
            notes += _learn_sql_rejection(db, cfg, rej, seg, cwd, session, text, command)
    return notes


def _ensure_spec(db, bin_key, path, name, ctx_words, proven):
    ctx = " ".join(ctx_words)
    spec = store.get_spec(db, bin_key, ctx)
    if spec is not None or not path:
        return spec
    learned = helpspec.learn(path, ctx_words, proven_parser=proven)
    if learned:
        if not ctx:
            learned.version = helpspec.version_of(path)
            db.execute("INSERT OR IGNORE INTO bins VALUES (?,?,?,?,?,?)",
                       (bin_key, os.path.basename(name), path, learned.version, None, time.time()))
        store.put_spec(db, bin_key, ctx, learned)
    return learned


def _learn_arg_rejection(db, cfg, rej, seg, cwd, session, text) -> list[str]:
    name = os.path.basename(seg.bin)
    bin_key, path = bin_identity(db, seg.bin, cwd)
    root = _ensure_spec(db, bin_key, path, name, [], proven=True)
    pos = [w.value for _, w in positionals(seg, root)]
    ctx_words = []
    if root and root.subcmds and pos and pos[0] in root.subcmds and rej.kind != "subcmd":
        ctx_words = [pos[0]]
        # One level per failure keeps the hook bounded (<= 2 binaries' --help);
        # a deeper level is learned on a later failure if it ever matters.
        sub = _ensure_spec(db, bin_key, path, name, ctx_words, proven=True)
        if sub and sub.subcmds and len(pos) > 1 and pos[1] in sub.subcmds \
                and store.get_spec(db, bin_key, f"{pos[0]} {pos[1]}"):
            ctx_words.append(pos[1])
    ctx = " ".join(ctx_words)
    spec = store.get_spec(db, bin_key, ctx) or root
    tok = rej.token
    kind = "subcmd" if rej.kind == "subcmd" else "flag"
    if rej.kind == "flag" and not any(n == tok for _, n, _ in option_words(seg)):
        # git reports `limt` for `-limt`; record the word as typed.
        tok = next((n for _, n, _ in option_words(seg) if n.lstrip("-") == tok.lstrip("-")), tok)
    if rej.kind == "missing_arg":
        store.upsert_count(db, "sigs", {"name": name, "ctx": ctx, "err_class": "missing_arg",
                                        "sig": errclass.signature(text)}, {"bin_key": bin_key, "via": " > ".join(seg.via)})
        if spec and tok in spec.opts:
            return [f"`{name}{' ' + ctx if ctx else ''} {tok}` takes a value"]
        return []
    shown = tok
    if NUMERIC_OPT.match(tok):
        tok = "-#"
        rej = errclass.Rejection(rej.kind, tok, rej.prog, "-n #")
    first = db.execute("SELECT n FROM rejects WHERE bin_key=? AND ctx=? AND kind=? AND token=?",
                       (bin_key, "" if kind == "subcmd" else ctx, kind, tok)).fetchone() is None
    store.upsert_count(db, "rejects", {"bin_key": bin_key, "ctx": "" if kind == "subcmd" else ctx,
                                       "kind": kind, "token": tok}, {"name": name})
    store.upsert_count(db, "sigs", {"name": name, "ctx": ctx, "err_class": "usage",
                                    "sig": errclass.signature(text)}, {"bin_key": bin_key, "via": " > ".join(seg.via)})
    fctx = "" if kind == "subcmd" else ctx
    good = None
    if rej.hint and rej.hint != tok:
        good = rej.hint
        store.add_fix(db, bin_key, fctx, kind, tok, good, "tool-hint", session, name)
    elif kind == "flag" and spec:
        u = helpspec.unique_fix(tok, spec)
        if u:
            good = u
            store.add_fix(db, bin_key, fctx, kind, tok, good, "spec-unique", session, name)
    elif kind == "subcmd" and (rej.choices or (root and root.subcmds)):
        close = [c for c in (rej.choices or root.subcmds) if helpspec.distance(c, tok) == 1]
        if len(close) == 1:
            good = close[0]
            store.add_fix(db, bin_key, fctx, kind, tok, good, "spec-unique", session, name)
    if kind == "flag" and session and tok != "-#":
        opts = sorted({n for _, n, _ in option_words(seg, spec)})
        db.execute("INSERT OR REPLACE INTO pending VALUES (?,?,?,?,?,?)",
                   (session, bin_key, ctx, tok, json.dumps(opts), time.time()))
    if first:
        emit({"event": "rejection", "bin": name, "ctx": ctx, "kind": kind, "bad": tok,
              "good": good or "", "version": (spec.version if spec else "")})
    label = name + (f" {ctx}" if ctx else "")
    ver = (root.version if root and root.version else "")
    head = f"argshape: `{label}`{f' ({ver})' if ver else ''} does not accept `{shown}` here."
    if good:
        good_shown = good.replace("#", shown[1:]) if tok == "-#" else good
        return [head + f" Use `{good_shown}`; argshape will apply this fix automatically next time."]
    if kind == "flag" and spec and spec.opts:
        near = helpspec.nearest(tok, spec.opts)
        return [head + (f" Nearest valid options: {', '.join(near)}." if near else
                        f" `{name} {ctx} --help` lists {len(spec.opts)} options.")]
    if kind == "subcmd" and root and root.subcmds:
        near = helpspec.nearest(tok, root.subcmds)
        return [head + f" Subcommands: {', '.join(near or root.subcmds[:12])}."]
    return []


def _learn_sql_rejection(db, cfg, rej, seg, cwd, session, text, command) -> list[str]:
    conn = sqlshape.conn_key(seg, cwd)
    name = os.path.basename(seg.bin)
    sqls = sqlshape.find_sql(seg)
    rels = []
    for sql, *_ in sqls:
        rels += [r for r in sqlshape.relations(sql) if r not in rels]
    kind = rej.kind
    col = rej.token.split(".")[-1].lower()
    store.upsert_count(db, "rejects", {"bin_key": conn, "ctx": "", "kind": kind, "token": col}, {"name": name})
    store.upsert_count(db, "sigs", {"name": name, "ctx": "sql", "err_class": f"sql_{kind}",
                                    "sig": errclass.signature(text)}, {"bin_key": conn, "via": " > ".join(seg.via)})
    notes = []
    if kind == "relation":
        return notes
    good = None
    if rej.hint:
        good = rej.hint.split(".")[-1].lower()
        store.add_fix(db, conn, "", "column", col, good, "pg-hint", session, name)
    cols = known_columns(db, conn, rels)
    if not cols and cfg.get("sql_active_learning", True) and rels:
        cols = _catalog_learn(db, seg, conn, rels, cwd, command)
    if cols and not good:
        lowered = [c.lower() for c in cols]
        norm = lambda s: s.replace("_", "").lower()
        exact = [c for c in lowered if norm(c) == norm(col)]
        close = exact or [c for c in lowered if helpspec.distance(c, col) <= 2]
        if len(close) == 1 and close[0] != col:
            good = close[0]
            store.add_fix(db, conn, "", "column", col, good, "catalog", session, name)
    rel_txt = ", ".join(rels) if rels else "this query"
    if good:
        notes.append(f"argshape: column `{col}` does not exist on {rel_txt}; use `{good}` "
                     "(argshape will apply this automatically next time).")
    if cols:
        shown = cols if len(cols) <= 40 else cols[:40] + [f"... +{len(cols) - 40}"]
        notes.append(f"argshape: columns of {rel_txt}: {', '.join(shown)}")
    return notes


def _catalog_learn(db, seg, conn, rels, cwd, command) -> list[str]:
    """Ask the same connection for its column list. Read-only catalog query."""
    if not SQL_BINS.search(seg.bin or "") or "sudo" in seg.via:
        return []
    arg_slots = [(s0, s1) for _, s0, s1, is_stdin in sqlshape.find_sql(seg) if not is_stdin]
    if len(arg_slots) != 1:
        return []
    names = sorted({r.split(".")[-1] for r in rels if re.fullmatch(r"[\w.]+", r)})
    if not names:
        return []
    inlist = ", ".join(f"'{n}'" for n in names)
    if re.search(r"sqlite", seg.bin, re.I):
        q = (f"select 'ARGSHAPE:'||m.name||'.'||p.name from sqlite_master m "
             f"join pragma_table_info(m.name) p where m.name in ({inlist});")
    else:
        q = (f"select 'ARGSHAPE:'||table_name||'.'||column_name from information_schema.columns "
             f"where table_name in ({inlist}) order by table_name, ordinal_position;")
    # Rebuild the segment's own text with the SQL word replaced.
    s0, s1 = arg_slots[0]
    cmd_text = command[seg.start:s0] + shlex.quote(q) + command[s1:seg.end]
    try:
        p = subprocess.run(["/bin/bash", "-c", cmd_text], cwd=cwd, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=6, start_new_session=True)
    except (subprocess.TimeoutExpired, OSError):
        return []
    by_rel: dict[str, list[str]] = {}
    for m in re.finditer(r"ARGSHAPE:(\w+)\.(\w+)", p.stdout.decode("utf-8", "replace")):
        by_rel.setdefault(m.group(1).lower(), []).append(m.group(2).lower())
    for rel, cols in by_rel.items():
        db.execute("INSERT OR REPLACE INTO columns VALUES (?,?,?,?,?)",
                   (conn, rel, json.dumps(cols), "catalog", time.time()))
    return [c for cols in by_rel.values() for c in cols]


def on_success(db, cfg: dict, ev: dict) -> list[str]:
    command = (ev.get("tool_input") or {}).get("command", "")
    resp = ev.get("tool_response") or {}
    stdout = resp.get("stdout", "") if isinstance(resp, dict) else str(resp)
    cwd = ev.get("cwd") or os.getcwd()
    session = ev.get("session_id") or ""
    _settle_applied(db, cfg, ev, failed=False, text=stdout)
    segs = segment.parse(command)
    # session-pair learning
    if session:
        rows = db.execute("SELECT bin_key, ctx, bad, opts, ts FROM pending WHERE session=?", (session,)).fetchall()
        for bin_key, ctx, bad, opts_json, ts in rows:
            if time.time() - ts > PAIR_WINDOW_S:
                db.execute("DELETE FROM pending WHERE session=? AND bin_key=? AND ctx=? AND bad=?",
                           (session, bin_key, ctx, bad))
                continue
            for seg in segs:
                if not seg.bin or bin_identity(db, seg.bin, cwd)[0] != bin_key:
                    continue
                sctx, spec = context(db, bin_key, seg)
                if sctx != ctx:
                    continue
                now_opts = {n for _, n, _ in option_words(seg, spec)}
                if bad in now_opts:
                    continue
                added = now_opts - set(json.loads(opts_json))
                if len(added) == 1:
                    good = added.pop()
                    store.add_fix(db, bin_key, ctx, "flag", bad, good, "session-pair", session,
                                  os.path.basename(seg.bin))
                db.execute("DELETE FROM pending WHERE session=? AND bin_key=? AND ctx=? AND bad=?",
                           (session, bin_key, ctx, bad))
                break
    # SQL passive learning: only when exactly one segment carries SQL
    sql_segs = [(s, sqlshape.find_sql(s)) for s in segs]
    sql_segs = [(s, q) for s, q in sql_segs if q]
    if len(sql_segs) == 1 and stdout:
        seg, slots = sql_segs[0]
        conn = sqlshape.conn_key(seg, cwd)
        sql = slots[0][0]
        _learn_from_output(db, conn, seg, sql, stdout)
    return []


def _learn_from_output(db, conn, seg, sql, stdout) -> None:
    hdr, rows = sqlshape.parse_table(stdout, sqlshape.output_sep(seg))
    if not hdr:
        return
    rels = sqlshape.relations(sql)
    t = time.time()
    low = [h.lower() for h in hdr]
    if sql.strip().lower().startswith("\\d") and low[:2] == ["column", "type"]:
        rel = sql.split()[1].split(".")[-1].lower() if len(sql.split()) > 1 else ""
        if rel:
            db.execute("INSERT OR REPLACE INTO columns VALUES (?,?,?,?,?)",
                       (conn, rel, json.dumps([r[0].lower() for r in rows]), "describe", t))
        return
    if len(rels) == 1 and re.search(r"select\s+\*\s+from", sql, re.I):
        db.execute("INSERT OR REPLACE INTO columns VALUES (?,?,?,?,?)",
                   (conn, rels[0].split(".")[-1], json.dumps(low), "select*", t))
    if len(rows) >= 3:
        for j, col in enumerate(low):
            if not re.fullmatch(r"[a-z_]\w*", col) or store.meta_get(db, f"shape_off:{conn}:{col}"):
                continue
            shape = sqlshape.infer_column_shape([r[j] for r in rows[:20]])
            if shape:
                db.execute("INSERT OR REPLACE INTO shapes VALUES (?,?,?,?,?,?)",
                           (conn, col, shape[0], shape[1], min(len(rows), 20), t))


def _settle_applied(db, cfg, ev, failed: bool, text: str) -> None:
    tid = ev.get("tool_use_id")
    if not tid:
        return
    row = db.execute("SELECT fixes FROM applied WHERE tool_use_id=? AND outcome IS NULL", (tid,)).fetchone()
    if not row:
        return
    fixes = json.loads(row[0])
    outcome = "ok"
    if failed:
        rejs = errclass.classify(text)
        toks = {r.token.split(".")[-1].lower() for r in rejs} | {r.token for r in rejs}
        related = [f for f in fixes if f["good"] in toks or f["good"].lower() in toks or f["bad"] in toks]
        outcome = "fail-related" if related else "fail-other"
        for f in related:
            _demote(db, f, "corrected command rejected the replacement")
    else:
        for f in fixes:
            db.execute("UPDATE fixes SET n_ok=n_ok+1 WHERE scope=? AND ctx=? AND kind=? AND bad=? AND good=?",
                       (f["scope"], f["ctx"], f["kind"], f["bad"], f["good"]))
    db.execute("UPDATE applied SET outcome=? WHERE tool_use_id=?", (outcome, tid))
    _check_breaker(db, cfg)


def _check_breaker(db, cfg) -> None:
    window = float(cfg.get("breaker_window_h", 24)) * 3600
    rows = db.execute("SELECT outcome FROM applied WHERE ts > ? AND outcome IS NOT NULL",
                      (time.time() - window,)).fetchall()
    n = len(rows)
    bad = sum(1 for (o,) in rows if o in ("fail-related", "reverted"))
    if n >= int(cfg.get("breaker_min_applied", 4)) and bad / n >= float(cfg.get("breaker_fail_ratio", 0.5)):
        if not breaker_open(db):
            store.meta_set(db, "breaker_until", str(time.time() + window))
            emit({"event": "breaker", "applied": n, "bad_count": bad})


# ---------------------------------------------------------------- bus

def emit(payload: dict) -> None:
    """Fire-and-forget bus event. Never raises, never waits."""
    if os.environ.get("ARGSHAPE_NO_BUS"):
        return
    nervous = shutil.which("nervous")
    if not nervous:
        return
    payload = {k: v for k, v in payload.items() if v is not None}
    payload.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    try:
        subprocess.Popen([nervous, "publish", CHANNEL, json.dumps(payload)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass
