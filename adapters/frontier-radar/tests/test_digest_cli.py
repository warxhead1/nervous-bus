import json
import os
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from fr import cli
from fr.digest import KbRefused, build_digest, kb_preflight, select
from fr.parsers import Item
from fr.scoring import refresh_scores
from fr.store import Store
from helpers import NOW, FakeFetcher, fixture, no_wait_throttle, src

VENDOR = src("vendor", "rss", "https://vendor.test/feed", "primary", signal=5)
DOMAINS = ["harness", "models", "research", "gamedev"]


def seed_digest_events(store, n, age_h=48, lane="digest"):
    for i in range(n):
        at = NOW - timedelta(hours=age_h)
        s = src(f"v{i}", "rss", "https://vendor.test/f", "primary", domains=[DOMAINS[i % len(DOMAINS)]], signal=1 + i % 5)
        store.record_sighting(s, Item(url=f"https://vendor.test/p{i}", title=f"Post {i}", published_at=at), at)
        store.update_event(f"https://vendor.test/p{i}", status="triaged", lane=lane,
                           triage={"claim": f"Claim {i}.", "why_it_matters": f"Matters {i}.", "domain": DOMAINS[i % len(DOMAINS)],
                                   "kind": "tool", "durability": "durable", "action": "trial", "confidence": 0.9})
    refresh_scores(store)


def fake_kb(tmp_path, with_tier: bool) -> str:
    log = tmp_path / "kb.log"
    script = tmp_path / "fakekb"
    help_text = "Options:\n      --tier <TIER>  inbox or promoted\n      --project <P>\n" if with_tier else "Options:\n      --project <P>\n"
    script.write_text(f"""#!/bin/sh
echo "$@" >> {log}
if [ "$2" = "--help" ]; then printf '%s' '{help_text}'; fi
exit 0
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_digest_never_exceeds_the_attention_budget_and_picks_the_top_scores():
    store = Store(":memory:")
    seed_digest_events(store, 12)
    res = build_digest(store, NOW, days=7, max_items=7, out_dir=None, dry_run=True)
    assert len(res["items"]) == 7
    scores = [e["score"] for e in res["items"]]
    assert scores == sorted(scores, reverse=True)
    assert min(scores) >= sorted((e["score"] for e in store.events()), reverse=True)[6]
    assert res["text"].count("\n- ") == 7


def test_digest_default_budget_is_seven():
    from fr.cli import build_parser
    assert build_parser().parse_args(["digest"]).max == 7


def test_digest_groups_by_domain_with_claim_why_link_action():
    store = Store(":memory:")
    seed_digest_events(store, 4)
    text = build_digest(store, NOW, 7, 7, None, dry_run=True)["text"]
    assert "## harness" in text and "## models" in text
    assert "Claim 0. Matters 0. [v0](https://vendor.test/p0) - **trial**" in text
    assert text.startswith("---\n") and "source_type: external-research" in text
    assert "half_life_days: 90" in text and "needs_human_review: true" in text and "status: inbox" in text


def test_digested_events_do_not_repeat_and_review_lane_stays_out(tmp_path):
    store = Store(":memory:")
    seed_digest_events(store, 3)
    seed_digest_events_review = Item(url="https://vendor.test/r", title="Thin", published_at=NOW - timedelta(hours=40))
    store.record_sighting(VENDOR, seed_digest_events_review, NOW - timedelta(hours=40))
    store.update_event("https://vendor.test/r", lane="review", status="triaged",
                       triage={"claim": "x", "why_it_matters": "y", "domain": "harness", "kind": "other",
                               "durability": "decaying", "action": "watch", "confidence": 0.2})
    first = build_digest(store, NOW, 7, 7, tmp_path)
    assert len(first["items"]) == 3 and first["review_count"] == 1
    assert "https://vendor.test/r" not in first["text"]
    assert first["path"].read_text().count("\n- ") == 3
    assert build_digest(store, NOW + timedelta(days=1), 7, 7, tmp_path)["items"] == []


def test_events_younger_than_24h_wait_for_their_first_rescore():
    store = Store(":memory:")
    seed_digest_events(store, 2, age_h=5)
    assert select(store, NOW, 7, 7) == []
    assert len(select(store, NOW, 7, 7, include_fresh=True)) == 2
    assert len(select(store, NOW + timedelta(days=1), 7, 7)) == 2


def test_kb_flag_refuses_when_kb_has_no_tier_option_and_ingests_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("FRONTIER_RADAR_KB", fake_kb(tmp_path, with_tier=False))
    store = Store(":memory:")
    seed_digest_events(store, 3)
    with pytest.raises(KbRefused, match="promoted tier"):
        build_digest(store, NOW, 7, 7, tmp_path / "out", kb=True)
    assert not (tmp_path / "out").exists()
    assert store.events("digested_at IS NOT NULL") == []
    assert "ingest --project" not in (tmp_path / "kb.log").read_text()


def test_kb_flag_refuses_when_kb_binary_is_missing(monkeypatch):
    monkeypatch.setenv("FRONTIER_RADAR_KB", "/nonexistent/kb-binary")
    with pytest.raises(KbRefused, match="cannot run"):
        kb_preflight()


def test_kb_flag_ingests_into_the_inbox_tier_when_supported(tmp_path, monkeypatch):
    monkeypatch.setenv("FRONTIER_RADAR_KB", fake_kb(tmp_path, with_tier=True))
    store = Store(":memory:")
    seed_digest_events(store, 3)
    res = build_digest(store, NOW, 7, 7, tmp_path / "out", kb=True)
    assert res["kb_ingested"] is True
    calls = (tmp_path / "kb.log").read_text().splitlines()
    ingest = [c for c in calls if c.startswith("ingest --project")][0]
    assert "--tier inbox" in ingest and ingest.endswith(str(res["path"]))
    assert len(store.events("digested_at IS NOT NULL")) == 3


def test_cli_digest_exits_3_on_kb_refusal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FRONTIER_RADAR_KB", fake_kb(tmp_path, with_tier=False))
    db = tmp_path / "radar.db"
    store = Store(db)
    seed_digest_events(store, 2)
    store.close()
    rc = cli.main(["--state", str(db), "digest", "--kb", "--no-publish"], now=NOW)
    assert rc == 3
    assert "promoted tier" in capsys.readouterr().err
    assert not (tmp_path / "digests").exists()


def test_cli_end_to_end_poll_triage_digest_with_published_payloads_valid_against_schema(tmp_path, monkeypatch, capsys):
    jsonschema = pytest.importorskip("jsonschema")
    repo = Path(__file__).resolve().parents[3]
    pubs = []
    monkeypatch.setattr(cli, "publish", lambda ch, p: pubs.append((ch, p)) or True)
    url = "https://github.com/anthropics/claude-code/releases.atom"
    srcs = tmp_path / "sources.toml"
    srcs.write_text(f'[[source]]\nid="cc"\nurl="{url}"\nkind="github-releases"\ndomains=["harness"]\nsignal=5\n'
                    'freshness_slo_days=14\nrole="primary"\nmin_interval_minutes=0\n')
    monkeypatch.setattr(cli, "Fetcher", lambda: FakeFetcher({url: fixture("claude_code_releases.atom")}))
    monkeypatch.setattr(cli, "Throttle", no_wait_throttle)
    db = str(tmp_path / "radar.db")
    base = ["--state", db, "--sources", str(srcs)]
    assert cli.main([*base, "poll"], now=NOW) == 0
    good = {"claim": "Claude Code shipped a release.", "why_it_matters": "It changes our harness.",
            "domain": "harness", "kind": "tool", "durability": "decaying", "action": "trial", "confidence": 0.8}
    reply = tmp_path / "reply.json"
    reply.write_text(json.dumps({"result": json.dumps(good)}))
    classifier = tmp_path / "classify"
    classifier.write_text(f"#!/bin/sh\ncat > /dev/null\ncat {reply}\n")
    classifier.chmod(0o755)
    assert cli.main([*base, "triage", "--cmd", str(classifier), "--limit", "4"], now=NOW) == 0
    later = NOW + timedelta(days=2)
    assert cli.main([*base, "digest", "--max", "2"], now=later) == 0
    out = capsys.readouterr().out
    assert out.count("\n- ") == 2
    assert list((tmp_path / "digests").glob("digest-*.md"))
    channels = {c for c, _ in pubs}
    assert channels == {"research.radar.item.v1", "research.radar.digest.v1"}
    for ch, payload in pubs:
        schema = json.loads((repo / "schemas" / f"{ch}.json").read_text())
        jsonschema.Draft202012Validator(schema).validate(payload)
    assert os.path.exists(db)
