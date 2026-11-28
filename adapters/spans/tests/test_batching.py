"""spans scan publishes in batches through `nervous publish-batch`, with the cursor saved per batch."""
from __future__ import annotations

import json
import stat

from conftest import FAKE, FAKE_FAIL, spans
from test_spans import result, use


def calls(world):
    f = world.tmp / "calls"
    return len(f.read_text().splitlines()) if f.exists() else 0


def five_calls(world):
    lines = []
    for i in range(5):
        lines += [use(f"c{i}", "Bash", i * 3, world.alpha, "ls"), result(f"c{i}", i * 3 + 1)]
    world.write(*lines)


def script(world, name, body):
    path = world.tmp / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def scan_with(world, nervous, *extra):
    return spans.main(["scan", "--root", str(world.root), "--cursor", str(world.cursor), "--nervous", nervous, *extra])


def test_scan_publishes_in_batches_not_one_process_per_span(world):
    five_calls(world)
    assert world.scan("--batch-size", "2") == 0
    assert calls(world) == 3 and len(world.sent()) == 5
    assert world.scan("--batch-size", "2") == 0
    assert calls(world) == 3


def test_batches_span_files_and_the_cursor_follows_each_batch(world):
    other = world.root / "-alpha" / "s2.jsonl"
    world.write(use("a", "Bash", 0, world.alpha, "ls"), result("a", 1), use("b", "Bash", 2, world.alpha, "ls"), result("b", 3))
    world.write(use("c", "Bash", 4, world.alpha, "ls"), result("c", 5), path=other)
    assert world.scan("--batch-size", "3") == 0
    assert calls(world) == 1 and len(world.sent()) == 3
    cur = json.loads(world.cursor.read_text())
    assert sorted(cur) == ["-alpha/s1.jsonl", "-alpha/s2.jsonl"]
    assert all(c["offset"] > 0 for c in cur.values())


def test_failed_second_batch_keeps_the_first_batch_in_the_cursor(world):
    five_calls(world)
    flaky = script(world, "flaky", FAKE.replace("body=$(cat)", FAKE_FAIL).replace("TMP", str(world.tmp)).split("\n", 1)[1])
    assert scan_with(world, flaky, "--batch-size", "2") == 1
    assert len(world.sent()) == 2
    cur = next(iter(json.loads(world.cursor.read_text()).values()))
    assert len(cur["done"]) == 2
    assert world.scan("--batch-size", "2") == 0
    sent = world.sent()
    assert len(sent) == 5 and len({s["span_id"] for s in sent}) == 5


def test_rejected_lines_are_accounted_and_not_resent(world, capsys):
    five_calls(world)
    reject = script(world, "reject", "cat >/dev/null\necho call >> '%s/calls'\n"
                    "echo '{\"published\":4,\"rejected\":[{\"line\":2,\"reason\":\"schema_violation\"}]}'\nexit 2\n" % world.tmp)
    assert scan_with(world, reject) == 0
    first = capsys.readouterr().err
    assert scan_with(world, reject) == 0
    assert calls(world) == 1
    assert json.loads(first.strip().splitlines()[-1]) == {"published": 4, "rejected": 1, "dry_run": False}
    assert "1 spans rejected" in first


def test_a_summary_that_does_not_add_up_is_retried(world):
    five_calls(world)
    liar = script(world, "liar", "cat >/dev/null\necho '{\"published\":1,\"rejected\":[]}'\n")
    assert scan_with(world, liar) == 1
    assert world.scan() == 0 and len(world.sent()) == 5


def test_batch_size_below_one_is_clamped(world):
    five_calls(world)
    assert world.scan("--batch-size", "0") == 0
    assert calls(world) == 5
