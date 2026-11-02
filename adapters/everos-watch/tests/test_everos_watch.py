import json
import os
import sys
from pathlib import Path

import jsonschema
import pytest

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
import everos_watch as ew  # noqa: E402

SCHEMAS = HERE.parents[3] / "schemas"
SECRET = "SECRET-BODY-TEXT-do-not-leak"


def entry(eid, body="body", session="s-1"):
    return (f"<!-- entry:{eid} -->\n## {eid}\n\n**owner_id**: x\n**session_id**: {session}\n\n"
            f"### Content\n{body}\n<!-- /entry:{eid} -->\n")


def daily(entries):
    return "---\nid: x\n---\n" + "".join(entries)


class Bus:
    def __init__(self):
        self.events = []

    def __call__(self, ev):
        self.events.append(ev)
        return True

    def take(self):
        out, self.events = self.events, []
        return out


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "root"
    (root / ".index" / "sqlite").mkdir(parents=True)
    (root / ".tmp").mkdir()
    w = ew.Watcher(root, debounce=0.0)
    bus = Bus()
    w.poll(bus)  # silent baseline on empty root
    return root, w, bus


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def validate(ev):
    schema = json.loads((SCHEMAS / f"{ev['channel']}.json").read_text())
    jsonschema.validate(ev["data"], schema)
    assert SECRET not in json.dumps(ev)


EP = "hearth/household/users/dana/episodes/episode-2026-11-02.md"


def test_created_updated_deleted_sequence(env):
    root, w, bus = env
    p = write(root, EP, daily([entry("ep_1", SECRET)]))
    w.poll(bus)
    ev = bus.take()
    assert [e["channel"] for e in ev] == ["memory.node.created.v1"]
    d = ev[0]["data"]
    assert (d["app_id"], d["project_id"], d["owner_kind"], d["owner_id"], d["kind"]) == (
        "hearth", "household", "user", "dana", "episode")
    assert d["entry_id"] == "ep_1" and d["session_id"] == "s-1" and d["md_path"] == EP

    # append a second entry: only the new one is created
    p.write_text(daily([entry("ep_1", SECRET), entry("ep_2")]))
    w.poll(bus)
    assert [(e["channel"], e["data"]["entry_id"]) for e in bus.take()] == [("memory.node.created.v1", "ep_2")]

    # edit ep_1 in place
    p.write_text(daily([entry("ep_1", SECRET + " edited"), entry("ep_2")]))
    os.utime(p, ns=(1, 1))  # force a signature change regardless of fs timestamp granularity
    w.poll(bus)
    assert [(e["channel"], e["data"]["entry_id"]) for e in bus.take()] == [("memory.node.updated.v1", "ep_1")]

    # remove ep_2 from the file
    p.write_text(daily([entry("ep_1", SECRET + " edited")]))
    w.poll(bus)
    assert [(e["channel"], e["data"]["entry_id"]) for e in bus.take()] == [("memory.node.deleted.v1", "ep_2")]

    # delete the file
    p.unlink()
    w.poll(bus)
    ev = bus.take()
    assert [(e["channel"], e["data"]["entry_id"]) for e in ev] == [("memory.node.deleted.v1", "ep_1")]
    validate(ev[0])


def test_noop_poll_is_silent(env):
    root, w, bus = env
    write(root, EP, daily([entry("ep_1")]))
    w.poll(bus)
    bus.take()
    w.poll(bus)
    w.poll(bus)
    assert bus.events == []


def test_atomic_rename_write_is_one_event_and_tmp_is_ignored(env):
    root, w, bus = env
    tmp = write(root, ".tmp/batch-123.md", daily([entry("ep_1", SECRET)]))
    w.poll(bus)
    assert bus.events == []  # staging dir is never scanned
    target = root / EP
    target.parent.mkdir(parents=True)
    os.replace(tmp, target)
    w.poll(bus)
    ev = bus.take()
    assert [e["channel"] for e in ev] == ["memory.node.created.v1"]
    # atomic rewrite of identical content through a fresh temp file: no event
    t2 = write(root, ".tmp/batch-124.md", target.read_text())
    os.replace(t2, target)
    w.poll(bus)
    assert bus.events == []


def test_debounce_defers_fresh_writes(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    w = ew.Watcher(root, debounce=5.0)
    bus = Bus()
    w.poll(bus)
    p = write(root, EP, daily([entry("ep_1")]))
    mt = p.stat().st_mtime
    w.poll(bus, now=mt + 1)
    assert bus.events == []
    w.poll(bus, now=mt + 10)
    assert len(bus.events) == 1


def test_kinds_and_owner_kinds(env):
    root, w, bus = env
    write(root, "hearth/household/users/dana/user.md", "---\nid: u\n---\nprefers coffee\n")
    write(root, "hearth/household/users/dana/.atomic_facts/atomic_fact-2026-11-02.md", daily([entry("af_1")]))
    write(root, "hearth/household/users/dana/.foresights/foresight-2026-11-02.md", daily([entry("fs_1")]))
    write(root, "nervous-bus/agents/agents/claude-code/.cases/agent_case-2026-11-02.md", daily([entry("ac_1", session="cc:abc")]))
    write(root, "nervous-bus/agents/agents/claude-code/skills/skill_x/SKILL.md", "# skill\n")
    write(root, "hearth/household/knowledge/note.md", "ignored")
    write(root, "hearth/household/users/dana/episodes/notes.txt", "ignored")
    w.poll(bus)
    got = {e["data"]["kind"]: e["data"] for e in bus.take()}
    assert set(got) == {"profile", "atomic_fact", "foresight", "agent_case", "agent_skill"}
    assert got["profile"]["entry_id"] == "user"
    assert got["agent_case"]["owner_kind"] == "agent" and got["agent_case"]["session_id"] == "cc:abc"
    assert got["agent_skill"]["entry_id"] == "skill_x"
    # profile rewrite is an update
    p = root / "hearth/household/users/dana/user.md"
    p.write_text("---\nid: u\n---\nprefers tea\n")
    os.utime(p, ns=(1, 1))
    w.poll(bus)
    ev = bus.take()
    assert [(e["channel"], e["data"]["kind"]) for e in ev] == [("memory.node.updated.v1", "profile")]
    validate(ev[0])


def test_studio_owner_redaction(env):
    root, w, bus = env
    fan = "9f2c" * 8
    rel = f"studio/persona-vera/users/{fan}/episodes/episode-2026-11-02.md"
    write(root, rel, daily([entry("ep_1", SECRET, session="thread:77")]))
    w.poll(bus)
    ev = bus.take()
    assert len(ev) == 1
    d = ev[0]["data"]
    assert d["redacted"] is True and "owner_id" not in d and "session_id" not in d
    assert fan not in json.dumps(ev) and "thread:77" not in json.dumps(ev)
    assert d["md_path"] == "studio/persona-vera/users/_redacted_/episodes/episode-2026-11-02.md"
    validate(ev[0])
    (root / rel).unlink()
    w.poll(bus)
    gone = bus.take()
    assert gone[0]["channel"] == "memory.node.deleted.v1" and fan not in json.dumps(gone)
    validate(gone[0])


def test_default_scope_dirs_map_back(env):
    root, w, bus = env
    write(root, "default_app/default_project/users/u1/episodes/episode-2026-11-02.md", daily([entry("ep_1")]))
    w.poll(bus)
    d = bus.take()[0]["data"]
    assert (d["app_id"], d["project_id"]) == ("default", "default")


def test_restart_resumes_from_state_without_replay(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    st = tmp_path / "state.json"
    w = ew.Watcher(root, debounce=0.0, state_path=st)
    bus = Bus()
    w.poll(bus)
    write(root, EP, daily([entry("ep_1")]))
    w.poll(bus)
    assert len(bus.take()) == 1
    assert oct(st.stat().st_mode & 0o777) == "0o600"
    # change while "down", then restart
    p = root / EP
    p.write_text(daily([entry("ep_1"), entry("ep_2")]))
    w2 = ew.Watcher(root, debounce=0.0, state_path=st)
    w2.poll(bus)
    assert [e["data"]["entry_id"] for e in bus.take()] == ["ep_2"]


def test_first_run_baseline_is_silent_then_emit_existing(tmp_path):
    root = tmp_path / "r"
    write(root, EP, daily([entry("ep_1")]))
    bus = Bus()
    ew.Watcher(root, debounce=0.0).poll(bus)
    assert bus.events == []
    ew.Watcher(root, debounce=0.0).poll(bus, emit_existing=True)
    assert len(bus.events) == 1


def test_failed_publish_retries(env):
    root, w, bus = env
    write(root, EP, daily([entry("ep_1")]))
    assert w.poll(lambda e: False) == 0
    w.poll(bus)
    assert len(bus.events) == 1


def test_every_payload_validates_and_has_no_content_keys(env):
    root, w, bus = env
    write(root, EP, daily([entry("ep_1", SECRET)]))
    write(root, "hearth/household/users/dana/user.md", SECRET)
    w.poll(bus)
    for e in bus.events:
        validate(e)
        assert set(e["data"]) <= {"app_id", "project_id", "owner_kind", "owner_id", "kind", "entry_id",
                                  "session_id", "md_path", "observed_at", "redacted"}
