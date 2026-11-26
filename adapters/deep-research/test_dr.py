"""Offline tests for dr: recorded fixtures, no network."""
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("dr", HERE / "dr.py")
dr = importlib.util.module_from_spec(spec)
sys.modules["dr"] = dr
spec.loader.exec_module(dr)

FIX = HERE / "fixtures"
FAKE_KEY = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE12"


def fixture(name):
    return json.loads((FIX / name).read_text())


class FakeHttp:
    """Scripted Interactions API. polls = list of fixture dicts returned in order (last repeats)."""

    def __init__(self, polls, create_id="int_abc123", fail_delete=False):
        self.polls, self.calls, self.create_id, self.fail_delete = list(polls), [], create_id, fail_delete

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        path = url.split("/v1beta", 1)[1]
        if method == "POST" and path == "/interactions":
            return 200, json.dumps({"id": self.create_id, "status": "in_progress"}).encode()
        if method == "GET":
            item = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            return 200, json.dumps(item).encode()
        if method == "POST" and path.endswith("/cancel"):
            return 200, b"{}"
        if method == "DELETE":
            return (500, b"boom") if self.fail_delete else (200, b"{}")
        return 404, b"{}"


CFG = {"agent": "deep-research-preview-04-2026", "api_revision": "2026-05-20", "poll_initial": 5.0,
       "poll_max": 30.0, "timeout": 100.0, "prices_per_mtok": {}}


def engine(http, cfg=None):
    return dr.GeminiEngine(cfg or CFG, http=http, env={"GEMINI_API_KEY": FAKE_KEY})


class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def now(self):
        return self.t


def run_it(tmp_path, polls, cfg=None, **kw):
    http = FakeHttp(polls, **{k: v for k, v in kw.items() if k == "fail_delete"})
    clk = Clock()
    run = dr.execute_run(engine(http, cfg), "What is AAIF?", cfg or CFG, tmp_path,
                         delete_remote=kw.get("delete_remote", False), sleep=clk.sleep,
                         clock=clk.now, rng=lambda: 0.5)
    return run, http, clk


# ------------------------------------------------------------ engine / polling

def test_run_completes_and_persists(tmp_path):
    run, http, clk = run_it(tmp_path, [fixture("in_progress.json"), fixture("in_progress.json"), fixture("completed.json")])
    rdir = tmp_path / run["run_id"]
    assert run["status"] == "completed"
    assert json.loads((rdir / "interaction.json").read_text())["id"] == "int_abc123"
    assert (rdir / "report.md").read_text().startswith("# AAIF report")
    assert run["usage"]["total_tokens"] == 1035000
    assert [c["url"] for c in run["citations"]] == ["https://example.org/a", "https://example.org/b", "https://example.org/a"]
    create = http.calls[0]
    assert create[0] == "POST" and json.loads(create[3]) == {
        "input": "What is AAIF?", "agent": "deep-research-preview-04-2026", "background": True,
        "agent_config": {"type": "deep-research"}}


def test_key_is_a_header_never_in_url_or_run_dir(tmp_path):
    run, http, _ = run_it(tmp_path, [fixture("completed.json")])
    for method, url, headers, body in http.calls:
        assert FAKE_KEY not in url and "key=" not in url
        assert headers["x-goog-api-key"] == FAKE_KEY
        assert headers["Api-Revision"] == "2026-05-20"
        assert body is None or FAKE_KEY.encode() not in body
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert FAKE_KEY not in p.read_text()


def test_backoff_grows_and_caps(tmp_path):
    polls = [fixture("in_progress.json")] * 7 + [fixture("completed.json")]
    _, _, clk = run_it(tmp_path, polls)
    assert clk.sleeps[:3] == [5.0, 7.5, 11.25]
    assert max(clk.sleeps) == 30.0 and clk.sleeps == sorted(clk.sleeps)


def test_timeout_cancels_and_marks_timeout(tmp_path):
    run, http, _ = run_it(tmp_path, [fixture("in_progress.json")], cfg=dict(CFG, timeout=60.0))
    assert run["status"] == "timeout"
    assert any(c[0] == "POST" and c[1].endswith("/interactions/int_abc123/cancel") for c in http.calls)
    assert run["report_md"] == "" and not (tmp_path / run["run_id"] / "report.md").exists()


def test_failed_run_has_no_report(tmp_path):
    run, _, _ = run_it(tmp_path, [fixture("failed.json")])
    assert run["status"] == "failed" and run["citations"] == []
    assert (tmp_path / run["run_id"] / "interaction.json").exists()
    assert not (tmp_path / run["run_id"] / "report.md").exists()


def test_delete_remote_after_download(tmp_path):
    run, http, _ = run_it(tmp_path, [fixture("completed.json")], delete_remote=True)
    assert run["remote_deleted"] is True
    assert (tmp_path / run["run_id"] / "interaction.json").exists()
    assert http.calls[-1][0] == "DELETE"


def test_delete_remote_failure_keeps_run(tmp_path):
    run, _, _ = run_it(tmp_path, [fixture("completed.json")], delete_remote=True, fail_delete=True)
    assert run["status"] == "completed" and run["remote_deleted"] is False


def test_no_delete_by_default(tmp_path):
    _, http, _ = run_it(tmp_path, [fixture("completed.json")])
    assert all(c[0] != "DELETE" for c in http.calls)


def test_missing_key_refused():
    with pytest.raises(dr.DrError) as e:
        dr.GeminiEngine(CFG, http=FakeHttp([]), env={})
    assert e.value.code == dr.EXIT_NOKEY


@pytest.mark.parametrize("agent", ["", "latest", "deep-research-latest"])
def test_unpinned_agent_refused(agent):
    with pytest.raises(dr.DrError):
        dr.GeminiEngine(dict(CFG, agent=agent), http=FakeHttp([]), env={"GEMINI_API_KEY": FAKE_KEY})


def test_http_error_message_never_contains_key():
    def bad(method, url, headers, body, timeout):
        return 400, f"bad request key={FAKE_KEY}".encode()
    with pytest.raises(dr.DrError) as e:
        engine(bad).start("q")
    assert FAKE_KEY not in str(e.value) and "[REDACTED]" in str(e.value)


def test_cli_has_no_key_argument():
    helptext = dr.build_parser().format_help()
    assert "--key" not in helptext and "api-key" not in helptext


# ------------------------------------------------------------ citations / usage

def test_citation_offsets_are_bytes_not_chars():
    raw = fixture("completed.json")
    text = dr.report_text(raw)
    cites = dr.extract_citations(text, raw)
    assert cites[0]["claim_span"] == "was donated on 2026-10-22 to the AAIF by Anthropic."
    assert cites[1]["claim_span"] == "The AAIF hosts 10 projects now."


def test_out_of_range_annotation_dropped():
    raw = fixture("completed.json")
    for a in raw["steps"][1]["content"][0]["annotations"]:
        a["end_index"] = 10_000
    cites = dr.extract_citations(dr.report_text(raw), raw)
    assert [c["url"] for c in cites] == ["https://example.org/lf"]  # markdown-link fallback


def test_cost_estimate_only_with_configured_prices(tmp_path):
    run, _, _ = run_it(tmp_path, [fixture("completed.json")])
    assert run["cost_estimate"] is None
    cfg = dict(CFG, prices_per_mtok={"input": 2.0, "output": 10.0})
    run, _, _ = run_it(tmp_path, [fixture("completed.json")], cfg=cfg)
    assert run["cost_estimate"] == pytest.approx(1.0 * 2 + 0.02 * 10 + 0.005 * 2 + 0.01 * 10)


def test_cost_cumulative(tmp_path):
    run_it(tmp_path, [fixture("completed.json")])
    run_it(tmp_path, [fixture("completed.json")], cfg=dict(CFG, prices_per_mtok={"input": 1.0, "output": 1.0}))
    rep = dr.cost_report(tmp_path)
    assert rep["total"]["runs"] == 2 and rep["total"]["total_tokens"] == 2 * 1035000
    assert rep["total"]["unpriced_runs"] == 1 and rep["total"]["cost_estimate"] == pytest.approx(1.035)


def test_cost_empty_dir(tmp_path):
    assert dr.cost_report(tmp_path / "nope")["total"]["runs"] == 0


# ------------------------------------------------------------ audit

def completed_run(tmp_path):
    run, _, _ = run_it(tmp_path, [fixture("completed.json")])
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
    run, _, _ = run_it(tmp_path, [fixture("failed.json")])
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
    assert fm["source"] == "gemini-interactions:int_abc123"
    assert fm["trace_uri"] == run["raw_path"] and Path(fm["trace_uri"]).exists()
    assert fm["run_id"] == run["run_id"] and fm["producer_model"] == "deep-research-preview-04-2026"
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
            "source": "gemini-interactions:x", "trace_uri": "/p", "run_id": "r", "producer_model": "m",
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


# ------------------------------------------------------------ notebook sink

def test_notebook_off_by_default_prints_manual_steps(tmp_path):
    run, rdir = audited(tmp_path)
    calls = []
    with pytest.raises(dr.DrError) as e:
        dr.sink_notebook(run, rdir, False, importer=lambda **kw: calls.append(kw))
    assert not calls
    assert e.value.code == dr.EXIT_USAGE and "UNOFFICIAL" in str(e.value) and "Deep Research" in str(e.value)


def test_notebook_requires_audit(tmp_path):
    run, rdir = completed_run(tmp_path)
    with pytest.raises(dr.DrError, match="un-audited"):
        dr.sink_notebook(run, rdir, True, importer=lambda **kw: kw)


def test_notebook_passes_only_confirmed_urls(tmp_path):
    run, rdir = completed_run(tmp_path)
    dr.audit_run(run, rdir, lambda c, p: ("CONFIRMED", "") if "donated" in c else ("REFUTED", ""),
                 fake_fetch({"https://example.org/a": "a", "https://example.org/b": "b"}))
    got = dr.sink_notebook(run, rdir, True, notebook_id="nb1", importer=lambda **kw: kw)
    assert got["urls"] == ["https://example.org/a"] and got["notebook_id"] == "nb1"
    assert got["report"].startswith("# AAIF report")


def test_notebook_without_package_prints_manual(tmp_path, monkeypatch):
    run, rdir = audited(tmp_path)
    monkeypatch.setattr(dr.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(dr.DrError, match="not importable"):
        dr.sink_notebook(run, rdir, True)


# ------------------------------------------------------------ cli

def test_cli_end_to_end_run_audit_render(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    cfgp = tmp_path / "c.toml"
    judge = tmp_path / "judge.sh"
    judge.write_text('#!/bin/sh\ncat >/dev/null\necho \'{"verdict": "CONFIRMED", "reason": "s"}\'\n')
    judge.chmod(0o755)
    cfgp.write_text(f'poll_initial = 0.0\npoll_max = 0.0\njudge_cmd = "{judge}"\n')
    runs = tmp_path / "runs"
    http = FakeHttp([fixture("completed.json")])
    base = ["--config", str(cfgp), "--runs-dir", str(runs)]
    assert dr.main(base + ["run", "q?"], http=http) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    monkeypatch.setattr(dr, "fetch_page", lambda url, t, m: (200, "page"))
    assert dr.main(base + ["audit", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["claims"]["CONFIRMED"] == 2
    assert dr.main(base + ["sink", "kb", run_id, "--render-only"]) == 0
    assert Path(json.loads(capsys.readouterr().out)["file"]).exists()
    assert dr.main(base + ["cost"]) == 0
    assert json.loads(capsys.readouterr().out)["total"]["runs"] == 1


def test_cli_missing_key_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    rc = dr.main(["--runs-dir", str(tmp_path), "run", "q"], http=FakeHttp([]))
    assert rc == dr.EXIT_NOKEY and "GEMINI_API_KEY" in capsys.readouterr().err


def test_cli_rejects_path_traversal_run_id(tmp_path, capsys):
    assert dr.main(["--runs-dir", str(tmp_path), "audit", "../x"]) == dr.EXIT_USAGE


def test_wrapper_script_help():
    p = subprocess.run([str(HERE / "dr"), "--help"], capture_output=True, text=True,
                       env={**os.environ, "GEMINI_API_KEY": ""})
    assert p.returncode == 0 and "sink" in p.stdout
