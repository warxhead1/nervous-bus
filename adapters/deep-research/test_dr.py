"""Offline tests for dr: recorded Orca snapshots, no browser, no network."""
import json
import os
import re
import stat
import subprocess
import sys
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("dr", HERE / "dr.py")
dr = importlib.util.module_from_spec(spec)
sys.modules["dr"] = dr
spec.loader.exec_module(dr)

FIX = HERE / "fixtures" / "orca"
PAGE = "page-1"
CFG = {"engine": "gemini-web", "poll_initial": 5.0, "poll_max": 30.0, "timeout": 100.0,
       "step_timeout": 30.0, "orca_bin": "orca"}


def snap(name, **sub):
    t = (FIX / f"{name}.txt").read_text().replace("PAGE", PAGE)
    for k, v in sub.items():
        t = t.replace(k, v)
    return t


class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def now(self):
        return self.t


class FakeOrca:
    """Replays recorded snapshots. A click on the right element advances the phase."""

    FLOW = {  # (phase, clicked name regex) -> next phase
        ("composer", r"^Upload & tools"): "tools_menu",
        ("tools_menu", r"^Deep research"): "composer_dr",
        ("ready", r"^Send message"): "plan",
        ("plan", r"^Start research"): "running",
    }
    SNAP = {"composer": "synthetic_composer", "tools_menu": "synthetic_tools_menu",
            "composer_dr": "synthetic_composer", "ready": "synthetic_ready_to_send",
            "plan": "synthetic_plan", "done": "synthetic_done", "failed": "synthetic_failed",
            "running": "synthetic_running"}

    def __init__(self, end="done", running_polls=2, first="composer", first_snap=None):
        self.phase, self.end, self.running_polls, self.calls = first, end, running_polls, []
        self.first_snap, self.tabs_open = first_snap, 0
        self.filled = None

    def __call__(self, args):
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "tab" and args[1] == "create":
            self.tabs_open += 1
            return json.dumps({"ok": True, "result": {"browserPageId": PAGE}})
        if cmd == "tab" and args[1] == "close":
            self.tabs_open -= 1
            return json.dumps({"ok": True, "result": {}})
        assert "--page" in args and args[args.index("--page") + 1] == PAGE, args
        if cmd == "snapshot":
            return self._snapshot()
        if cmd == "click":
            self._click(args[args.index("--element") + 1])
            return json.dumps({"ok": True, "result": {}})
        if cmd == "fill":
            self.filled = args[args.index("--value") + 1]
            self.phase = "ready"
            return json.dumps({"ok": True, "result": {}})
        if cmd == "eval":
            expr = args[args.index("--expression") + 1]
            if "walk" in expr:
                return json.dumps({"ok": True, "result": {"result": (FIX / "synthetic_report_dom_result.json").read_text()}})
            url = "https://gemini.google.com/app"
            if self.phase in ("plan", "running", "done", "failed"):
                url += "/abc123def"
            return json.dumps({"ok": True, "result": {"result": url}})
        raise AssertionError(f"unexpected orca call {args}")

    def _snapshot(self):
        if self.first_snap and self.phase == "composer":
            return self.first_snap
        if self.phase == "running":
            if self.running_polls <= 0:
                self.phase = self.end
            else:
                self.running_polls -= 1
        return snap(self.SNAP[self.phase])

    def _click(self, ref):
        cur = self._snapshot() if self.phase != "running" else snap("synthetic_running")
        for role, name, r in dr.parse_snapshot(cur):
            if r == ref:
                for (ph, pat), nxt in self.FLOW.items():
                    if ph == self.phase and re.search(pat, name):
                        self.phase = nxt
                        return
                return
        raise AssertionError(f"click on unknown ref {ref}")


def run_it(tmp_path, end="done", running_polls=2, cfg=None, runner=None, **kw):
    fake = runner or FakeOrca(end=end, running_polls=running_polls)
    clk = Clock()
    eng = dr.GeminiWebEngine(cfg or CFG, runner=fake, sleep=clk.sleep, clock=clk.now)
    run = dr.execute_run(eng, "What is AAIF?", cfg or CFG, tmp_path, delete_remote=kw.get("delete_remote", False),
                         sleep=clk.sleep, clock=clk.now, rng=lambda: 0.5)
    return run, fake, clk


# ------------------------------------------------------------ snapshot parsing

def test_real_signed_out_snapshot_is_recognised():
    s = (FIX / "signed_out_tools_menu.txt").read_text()
    assert dr.find_ref(s, "signed_out")
    assert dr.find_ref(s, "prompt") == "@e27"
    assert dr.find_ref(s, "deep_research") == "@e34"
    assert dr.find_ref(s, "tools") == "@e18"
    assert not dr.find_ref(s, "start_research")


def test_ui_table_is_the_only_place_with_labels():
    src = (HERE / "dr.py").read_text()
    body = src.split("class GeminiWebEngine")[1].split("def _expand")[0]
    assert "Start research" not in body and "Deep research" not in body


# ------------------------------------------------------------ engine / polling

def test_run_completes_and_persists(tmp_path):
    run, fake, clk = run_it(tmp_path)
    rdir = tmp_path / run["run_id"]
    assert run["status"] == "completed" and run["engine"] == "gemini-web"
    assert run["interaction_id"] == "abc123def"
    assert (rdir / "report.md").read_text().startswith("# AAIF report")
    assert json.loads((rdir / "interaction.json").read_text())["conversation"] == "abc123def"
    assert [c["url"] for c in run["citations"]] == ["https://example.org/a", "https://example.org/b", "https://example.org/a"]
    assert fake.filled == "What is AAIF?"
    assert fake.tabs_open == 0  # tab closed at the end


def test_start_follows_the_ui_order(tmp_path):
    _, fake, _ = run_it(tmp_path)
    clicks = [c for c in fake.calls if c[0] == "click"]
    assert [c[2] for c in clicks] == ["@e18", "@e34", "@e41", "@e52"]  # tools, deep research, send, start
    assert fake.calls[0][:3] == ["tab", "create", "--url"] and fake.calls[0][3].startswith("https://gemini.google.com")


def test_no_api_key_anywhere(tmp_path):
    _, fake, _ = run_it(tmp_path)
    assert not any("key" in " ".join(c).lower() for c in fake.calls)
    src = (HERE / "dr.py").read_text()
    assert "GEMINI_API_KEY" not in src and "generativelanguage" not in src and "x-goog-api-key" not in src


def test_backoff_grows_and_caps(tmp_path):
    _, _, clk = run_it(tmp_path, running_polls=8)
    polls = [s for s in clk.sleeps if s >= 5.0]
    assert polls[:3] == [5.0, 7.5, 11.25]
    assert max(polls) == 30.0 and polls == sorted(polls)


def test_timeout_closes_tab_and_marks_timeout(tmp_path):
    run, fake, _ = run_it(tmp_path, running_polls=10_000, cfg=dict(CFG, timeout=60.0))
    assert run["status"] == "timeout"
    assert fake.tabs_open == 0
    assert run["report_md"] == "" and not (tmp_path / run["run_id"] / "report.md").exists()


def test_failed_run_has_no_report(tmp_path):
    run, _, _ = run_it(tmp_path, end="failed")
    assert run["status"] == "failed" and run["citations"] == []
    assert (tmp_path / run["run_id"] / "interaction.json").exists()
    assert not (tmp_path / run["run_id"] / "report.md").exists()


def test_conversation_is_never_deleted(tmp_path):
    run, fake, _ = run_it(tmp_path, delete_remote=True)
    assert run["status"] == "completed" and run["remote_deleted"] is False
    assert not any(c[0] in ("delete", "dialog") for c in fake.calls)


def test_signed_out_is_a_login_error_not_a_hang(tmp_path):
    fake = FakeOrca(first_snap=(FIX / "signed_out_tools_menu.txt").read_text())
    clk = Clock()
    eng = dr.GeminiWebEngine(CFG, runner=fake, sleep=clk.sleep, clock=clk.now)
    with pytest.raises(dr.DrError) as e:
        eng.start("q")
    assert e.value.code == dr.EXIT_LOGIN and "sign in" in str(e.value).lower()
    assert not any(c[0] == "fill" for c in fake.calls)  # nothing submitted
    assert fake.tabs_open == 0


def test_missing_ui_element_times_out_with_its_name(tmp_path):
    class Stuck(FakeOrca):
        def _snapshot(self):
            return snap("synthetic_composer")  # tools click never opens a menu
        def _click(self, ref):
            pass
    clk = Clock()
    eng = dr.GeminiWebEngine(CFG, runner=Stuck(), sleep=clk.sleep, clock=clk.now)
    with pytest.raises(dr.DrError, match="tools menu entry"):
        eng.start("q")


def test_orca_missing_binary_message():
    with pytest.raises(dr.DrError, match="not found"):
        dr.orca_runner("/nonexistent/orca-xyz")(["status"])


def test_cli_has_no_key_argument():
    helptext = dr.build_parser().format_help()
    assert "--key" not in helptext and "api-key" not in helptext


# ------------------------------------------------------------ citations / usage

def test_citation_claim_widens_to_its_line():
    text = (FIX / "synthetic_report_dom_result.json").read_text()
    cites = dr.extract_citations(json.loads(text)["markdown"])
    assert cites[0]["claim_span"].startswith("Agent Skills was donated on 2026-10-22")
    assert cites[1]["claim_span"] == cites[2]["claim_span"]


def test_cost_lists_runs_without_prices(tmp_path):
    run_it(tmp_path)
    rep = dr.cost_report(tmp_path)
    assert rep["total"]["runs"] == 1 and rep["total"]["unpriced_runs"] == 1


def test_cost_empty_dir(tmp_path):
    assert dr.cost_report(tmp_path / "nope")["total"]["runs"] == 0


# ------------------------------------------------------------ audit

def completed_run(tmp_path):
    run, _, _ = run_it(tmp_path)
    return run, tmp_path / run["run_id"]


def fake_fetch(table):
    def f(url):
        v = table[url]
        if isinstance(v, Exception):
            raise v
        return 200, v
    return f


def test_aggregate_claim_rules():
    agg = dr.aggregate_claim
    assert agg(["CONFIRMED", "REFUTED"]) == "WEAKENED"
    assert agg(["CONFIRMED", "UNREACHABLE"]) == "CONFIRMED"
    assert agg(["WEAKENED", "REFUTED"]) == "WEAKENED"
    assert agg(["REFUTED", "UNREACHABLE"]) == "REFUTED"
    assert agg(["UNREACHABLE"]) == "UNREACHABLE"


def test_audit_verdicts_and_summary(tmp_path):
    run, rdir = completed_run(tmp_path)
    fetch = fake_fetch({"https://example.org/a": "page a", "https://example.org/b": dr.DrError("HTTP 404")})
    judged = []

    def judge(claim, page):
        judged.append((claim, page))
        return ("CONFIRMED", "ok") if "donated" in claim else ("WEAKENED", "meh")
    audit = dr.audit_run(run, rdir, judge, fetch)
    verdicts = [(i["url"], i["verdict"]) for i in audit["items"]]
    assert verdicts == [("https://example.org/a", "CONFIRMED"), ("https://example.org/b", "UNREACHABLE"),
                        ("https://example.org/a", "WEAKENED")]
    assert len(judged) == 2  # unreachable url is never judged
    s = audit["summary"]
    assert s["claims"] == {"CONFIRMED": 1, "WEAKENED": 1, "REFUTED": 0, "UNREACHABLE": 0}
    # claim 2 has b UNREACHABLE and a WEAKENED -> WEAKENED
    assert s["pairs"] == {"CONFIRMED": 1, "WEAKENED": 1, "REFUTED": 0, "UNREACHABLE": 1}
    assert json.loads((rdir / "audit.json").read_text())["summary"] == s


def test_audit_judge_failure_is_never_confirmed(tmp_path):
    run, rdir = completed_run(tmp_path)

    def judge(claim, page):
        raise dr.DrError("judge exited 1")
    audit = dr.audit_run(run, rdir, judge, fake_fetch({"https://example.org/a": "x", "https://example.org/b": "y"}))
    assert {i["verdict"] for i in audit["items"]} == {"UNREACHABLE"}
    assert all(i["reason"].startswith("unjudged") for i in audit["items"])


def test_audit_fetches_each_url_once(tmp_path):
    run, rdir = completed_run(tmp_path)
    n = []

    def fetch(url):
        n.append(url)
        return 200, "t"
    dr.audit_run(run, rdir, lambda c, p: ("CONFIRMED", ""), fetch)
    assert sorted(n) == ["https://example.org/a", "https://example.org/b"]


def test_parse_judge_output_claude_envelope_and_bare():
    env = json.dumps({"type": "result", "result": 'Sure.\n{"verdict": "refuted", "reason": "no"}'})
    assert dr.parse_judge_output(env) == ("REFUTED", "no")
    assert dr.parse_judge_output('{"verdict": "CONFIRMED", "reason": "yes"}') == ("CONFIRMED", "yes")


@pytest.mark.parametrize("out", ["no json", '{"verdict": "MAYBE"}', json.dumps({"result": "nothing"})])
def test_parse_judge_output_rejects(out):
    with pytest.raises(dr.DrError):
        dr.parse_judge_output(out)


def test_command_judge_runs_configured_command(tmp_path):
    script = tmp_path / "judge.sh"
    script.write_text('#!/bin/sh\ncat >/dev/null\necho \'{"verdict": "WEAKENED", "reason": "stub"}\'\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    assert dr.make_command_judge(str(script), 10)("claim", "page") == ("WEAKENED", "stub")


def test_command_judge_nonzero_exit(tmp_path):
    with pytest.raises(dr.DrError):
        dr.make_command_judge("false", 10)("c", "p")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x.org/a", "http://127.0.0.1/x", "http://localhost/x"])
def test_fetch_refuses_non_public_targets(url):
    with pytest.raises(dr.DrError, match="scheme|public address"):
        dr.fetch_page(url)


# ------------------------------------------------------------ kb sink

def fake_kb(tmp_path, with_tier=True, rc=0):
    """A kb stand-in: --help lists --tier or not; ingest appends its argv to a log."""
    log = tmp_path / "kb.log"
    help_text = "Options:\n      --tier <TIER>  tier\n" if with_tier else "Options:\n      --project <P>\n"
    p = tmp_path / ("kb_tier" if with_tier else "kb_old")
    p.write_text(f'#!/bin/sh\nif [ "$2" = "--help" ]; then printf "{help_text}"; exit 0; fi\n'
                 f'echo "$@" >> {log}\nexit {rc}\n')
    p.chmod(0o755)
    return str(p), log


def audited(tmp_path):
    run, rdir = completed_run(tmp_path)
    dr.audit_run(run, rdir, lambda c, p: ("CONFIRMED", "ok"), fake_fetch({"https://example.org/a": "a", "https://example.org/b": "b"}))
    return run, rdir


def parse_fm(path):
    lines = path.read_text().split("\n")
    assert lines[0] == "---"
    end = lines.index("---", 1)
    return {k: json.loads(v) for k, v in (ln.split(": ", 1) for ln in lines[1:end])}


def test_kb_sink_refuses_unaudited(tmp_path):
    run, rdir = completed_run(tmp_path)
    kb, _ = fake_kb(tmp_path)
    with pytest.raises(dr.DrError, match="un-audited"):
        dr.sink_kb(run, rdir, "research", False, kb)


def test_kb_sink_allow_unaudited_marks_sources(tmp_path):
    run, rdir = completed_run(tmp_path)
    kb, log = fake_kb(tmp_path)
    out, _ = dr.sink_kb(run, rdir, "research", True, kb)
    fm = parse_fm(out)
    assert fm["audit"] == "unaudited" and {s["verdict"] for s in fm["sources"]} == {"UNAUDITED"}
    assert "--tier inbox" in log.read_text()


def test_kb_sink_refuses_without_tier_flag(tmp_path):
    run, rdir = audited(tmp_path)
    kb, log = fake_kb(tmp_path, with_tier=False)
    with pytest.raises(dr.DrError, match="--tier"):
        dr.sink_kb(run, rdir, "research", False, kb)
    assert not log.exists() and not (rdir / "kb").exists()


def test_kb_sink_refuses_missing_kb_binary(tmp_path):
    run, rdir = audited(tmp_path)
    with pytest.raises(dr.DrError, match="--tier"):
        dr.sink_kb(run, rdir, "research", False, str(tmp_path / "nokb"))


def test_kb_sink_never_ingests_promoted(tmp_path):
    run, rdir = audited(tmp_path)
    kb, log = fake_kb(tmp_path)
    dr.sink_kb(run, rdir, "research", False, kb)
    args = log.read_text().split()
    assert args[:3] == ["ingest", "--tier", "inbox"]


def test_kb_sink_failed_run_refused(tmp_path):
    run, _, _ = run_it(tmp_path, end="failed")
    with pytest.raises(dr.DrError, match="failed"):
        dr.sink_kb(run, tmp_path / run["run_id"], "research", True, "kb")


def test_frontmatter_contract(tmp_path):
    run, rdir = audited(tmp_path)
    out, _ = dr.sink_kb(run, rdir, "myproj", False, "unused", render_only=True)
    fm = parse_fm(out)
    for k in dr.REQUIRED_FM:
        assert k in fm, k
    assert fm["project"] == "myproj" and fm["category"] == "research"
    assert fm["source_type"] == "gemini-deep-research"
    assert fm["source"] == "gemini-web:abc123def"
    assert fm["trace_uri"] == run["raw_path"] and Path(fm["trace_uri"]).exists()
    assert fm["run_id"] == run["run_id"] and fm["producer_model"] == "gemini-web-deep-research"
    assert fm["confidence"] <= 0.4 and fm["status"] == "inbox"
    assert fm["needs_human_review"] is True and fm["half_life_days"] == 90
    assert {s["url"] for s in fm["sources"]} == {"https://example.org/a", "https://example.org/b"}
    assert all(s["verdict"] == "CONFIRMED" and s["fetched_at"] for s in fm["sources"])
    assert out.read_text().split("---\n", 2)[2].lstrip().startswith("# AAIF report")


def test_render_only_never_calls_kb(tmp_path):
    run, rdir = audited(tmp_path)
    kb, log = fake_kb(tmp_path)
    dr.sink_kb(run, rdir, "research", False, kb, render_only=True)
    assert not log.exists()


def good_fm():
    return {"title": "t", "project": "research", "category": "research", "source_type": "gemini-deep-research",
            "source": "gemini-web:x", "trace_uri": "/p", "run_id": "r", "producer_model": "m",
            "confidence": 0.3, "status": "inbox", "needs_human_review": True, "sources": [], "half_life_days": 90}


@pytest.mark.parametrize("field,bad", [
    ("category", "reference"), ("source_type", "manual"), ("source", "x"), ("status", "promoted"),
    ("needs_human_review", False), ("confidence", 0.41), ("confidence", True), ("confidence", "0.3"),
    ("half_life_days", 30), ("trace_uri", ""), ("producer_model", ""), ("sources", [{"url": "u"}]),
])
def test_frontmatter_invariants_fail_on_violation(field, bad):
    dr.validate_frontmatter(good_fm())
    fm = good_fm()
    fm[field] = bad
    with pytest.raises(dr.DrError):
        dr.validate_frontmatter(fm)


@pytest.mark.parametrize("field", dr.REQUIRED_FM)
def test_frontmatter_missing_field_fails(field):
    fm = good_fm()
    del fm[field]
    with pytest.raises(dr.DrError, match="missing"):
        dr.validate_frontmatter(fm)


def test_build_frontmatter_clamps_confidence(tmp_path):
    run, rdir = audited(tmp_path)
    audit = json.loads((rdir / "audit.json").read_text())
    assert dr.build_frontmatter(run, audit, "p", confidence=0.9)["confidence"] == 0.4


# ------------------------------------------------------------ legacy runs

def legacy_run(tmp_path):
    rdir = tmp_path / "20260101T000000Z-0001"
    rdir.mkdir(parents=True)
    (rdir / "interaction.json").write_text("{}")
    (rdir / "report.md").write_text("# old\n\nA claim long enough to matter ([s](https://example.org/a)).\n")
    run = {"run_id": rdir.name, "question": "old q", "engine": "gemini-deep-research",
           "agent": "deep-research-preview-04-2026", "interaction_id": "int_old", "started": "t",
           "finished": "2026-01-01T00:00:00Z", "status": "completed", "usage": {"total_tokens": 7},
           "cost_estimate": 0.5, "raw_path": str(rdir / "interaction.json"),
           "report_md": (rdir / "report.md").read_text(), "citations": []}
    dr.write_json(rdir / "run.json", run)
    return rdir


def test_legacy_interactions_run_still_loads_audits_and_renders(tmp_path):
    rdir = legacy_run(tmp_path)
    run, _ = dr.load_run(tmp_path, rdir.name)
    run["citations"] = dr.extract_citations(run["report_md"])
    audit = dr.audit_run(run, rdir, lambda c, p: ("CONFIRMED", "ok"), lambda u: (200, "x"))
    assert audit["summary"]["claims"]["CONFIRMED"] == 1
    out, _ = dr.sink_kb(run, rdir, "research", False, "unused", render_only=True)
    assert parse_fm(out)["source"] == "gemini-interactions:int_old"
    assert dr.cost_report(tmp_path)["total"]["total_tokens"] == 7


def test_validate_accepts_both_source_prefixes_and_rejects_others():
    for ok in ("gemini-web:abc", "gemini-interactions:abc"):
        dr.validate_frontmatter(dict(good_fm(), source=ok))
    with pytest.raises(dr.DrError):
        dr.validate_frontmatter(dict(good_fm(), source="manual:abc"))


# ------------------------------------------------------------ notebook import

NB_SNAPS = {
    "home": '- button "Create new" [ref=e1]\n',
    "sources": '- button "Add source" [ref=e2]\n',
    "types": '- button "Copied text" [ref=e3]\n- button "Websites" [ref=e4]\n',
    "text": '- textbox "Paste text here" [ref=e5]\n- button "Insert" [ref=e6]\n',
}


class FakeNotebook:
    def __init__(self, signed_out=False):
        self.calls, self.phase, self.filled, self.signed_out = [], "home", [], signed_out
        self.tabs = 0
        self.kind = None

    def __call__(self, args):
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "tab" and args[1] == "create":
            self.tabs += 1
            return json.dumps({"ok": True, "result": {"browserPageId": PAGE}})
        if cmd == "tab":
            self.tabs -= 1
            return json.dumps({"ok": True, "result": {}})
        if cmd == "snapshot":
            if self.signed_out:
                return '- button "Sign in to Gemini Notebook" [ref=e40]\n'
            return NB_SNAPS[self.phase]
        if cmd == "click":
            ref = args[args.index("--element") + 1]
            self.phase = {"@e1": "sources", "@e2": "types", "@e3": "text", "@e4": "text", "@e6": "sources"}[ref]
            if ref in ("@e3", "@e4"):
                self.kind = ref
            return json.dumps({"ok": True, "result": {}})
        if cmd == "fill":
            self.filled.append((self.kind, args[args.index("--value") + 1]))
            return json.dumps({"ok": True, "result": {}})
        if cmd == "eval":
            return json.dumps({"ok": True, "result": {"result": "https://notebook.google.com/notebook/nb9"}})
        raise AssertionError(args)


def test_notebook_requires_audit(tmp_path):
    run, rdir = completed_run(tmp_path)
    with pytest.raises(dr.DrError, match="un-audited"):
        dr.notebook_import(run, rdir, FakeNotebook())


def test_notebook_adds_report_then_only_confirmed_urls(tmp_path):
    run, rdir = completed_run(tmp_path)
    dr.audit_run(run, rdir, lambda c, p: ("CONFIRMED", "") if "donated" in c else ("REFUTED", ""),
                 fake_fetch({"https://example.org/a": "a", "https://example.org/b": "b"}))
    fake = FakeNotebook()
    clk = Clock()
    got = dr.notebook_import(run, rdir, fake, sleep=clk.sleep, clock=clk.now, step_timeout=30)
    assert got == {"notebook_url": "https://notebook.google.com/notebook/nb9", "urls_added": 1}
    assert fake.filled[0][1].startswith("# AAIF report")
    assert fake.filled[1][1] == "https://example.org/a"
    assert fake.tabs == 0


def test_notebook_signed_out_is_a_login_error(tmp_path):
    run, rdir = audited(tmp_path)
    fake = FakeNotebook(signed_out=True)
    clk = Clock()
    with pytest.raises(dr.DrError) as e:
        dr.notebook_import(run, rdir, fake, sleep=clk.sleep, clock=clk.now, step_timeout=30)
    assert e.value.code == dr.EXIT_LOGIN and not fake.filled and fake.tabs == 0


# ------------------------------------------------------------ cli

def test_cli_end_to_end_run_audit_render(tmp_path, capsys, monkeypatch):
    cfgp = tmp_path / "c.toml"
    judge = tmp_path / "judge.sh"
    judge.write_text('#!/bin/sh\ncat >/dev/null\necho \'{"verdict": "CONFIRMED", "reason": "s"}\'\n')
    judge.chmod(0o755)
    cfgp.write_text(f'poll_initial = 0.0\npoll_max = 0.0\njudge_cmd = "{judge}"\n')
    runs = tmp_path / "runs"
    base = ["--config", str(cfgp), "--runs-dir", str(runs)]
    monkeypatch.setattr(dr.time, "sleep", lambda s: None)
    assert dr.main(base + ["run", "q?"], runner=FakeOrca()) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    monkeypatch.setattr(dr, "fetch_page", lambda url, t, m: (200, "page"))
    assert dr.main(base + ["audit", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["claims"]["CONFIRMED"] == 2
    assert dr.main(base + ["sink", "kb", run_id, "--render-only"]) == 0
    assert Path(json.loads(capsys.readouterr().out)["file"]).exists()
    assert dr.main(base + ["notebook", run_id], runner=FakeNotebook()) == 0
    assert json.loads(capsys.readouterr().out)["urls_added"] == 2
    assert dr.main(base + ["cost"]) == 0
    assert json.loads(capsys.readouterr().out)["total"]["runs"] == 1


def test_cli_signed_out_exit_code(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(dr.time, "sleep", lambda s: None)
    fake = FakeOrca(first_snap=(FIX / "signed_out_tools_menu.txt").read_text())
    rc = dr.main(["--runs-dir", str(tmp_path), "run", "q"], runner=fake)
    assert rc == dr.EXIT_LOGIN and "not signed in" in capsys.readouterr().err


def test_cli_rejects_path_traversal_run_id(tmp_path, capsys):
    assert dr.main(["--runs-dir", str(tmp_path), "audit", "../x"]) == dr.EXIT_USAGE


def test_wrapper_script_help():
    p = subprocess.run([str(HERE / "dr"), "--help"], capture_output=True, text=True)
    assert p.returncode == 0 and "notebook" in p.stdout
