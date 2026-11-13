import json
import os
import subprocess
import sys
from pathlib import Path

AUDIT = Path(__file__).resolve().parents[1] / "skills/human-attention/scripts/copy-audit"

TSX = """
export function Page({ items }: { items: Array<string> }) {
  const [open, setOpen] = useState<boolean>(false);
  return (
    <div>
      <h1>Settings</h1>
      <p className="text-muted-foreground">Manage your settings and preferences in one place</p>
      <Card><CardTitle>Alerts</CardTitle><CardDescription>Choose which alerts you get</CardDescription></Card>
      <Input placeholder="Search" aria-label="Search settings" onChange={(e) => setOpen(e.target.value > "")} />
      {items.map((i) => <li key={i}>{i}</li>)}
      <Dialog><DialogTitle>Delete</DialogTitle><DialogDescription>This cannot be undone.</DialogDescription></Dialog>
    </div>
  );
}
"""

HTML = """<!doctype html><html><head><title>T</title><style>.a{color:red}</style></head><body>
<h2>Fever</h2><button>Start</button>
<script>ctx.fillText("Stand on the tile", 1, 1); if (a<b) x();</script></body></html>"""


def run(tmp_path, name, body, *args):
    f = tmp_path / name
    f.write_text(body)
    r = subprocess.run([sys.executable, str(AUDIT), str(f), "--json", *args], capture_output=True, text=True)
    return r.returncode, json.loads(r.stdout)


def test_tsx_counts_visible_copy_only(tmp_path):
    rc, out = run(tmp_path, "p.tsx", TSX)
    texts = [s["text"] for s in out["files"][0]["items"]]
    assert rc == 0
    assert texts == ["Settings", "Manage your settings and preferences in one place", "Alerts",
                     "Choose which alerts you get", "Search", "Delete", "This cannot be undone."]
    assert out["total"]["heading_subtitle_pairs"] == 2
    assert out["total"]["description_slots"] == 2  # the Radix dialog description is exempt
    assert out["total"]["stock"] == 1


def test_html_skips_code_and_reads_canvas_text(tmp_path):
    _, out = run(tmp_path, "g.html", HTML)
    assert [s["text"] for s in out["files"][0]["items"]] == ["Fever", "Start", "Stand on the tile"]


def test_limit_exceeded_exits_1(tmp_path):
    rc, _ = run(tmp_path, "p.tsx", TSX, "--max-pairs", "0")
    assert rc == 1


HOOK = AUDIT.with_name("copy-audit-hook")

GAME = """<!doctype html><html><body><h1>Fever Dungeon</h1>
<p>You are burning up. Your fever climbs every second and at one hundred you die.</p>
<p>Stand on a blue tile to cool down. Dodge the red embers. Reach the golden door in each of the rooms.</p>
<p>Move with the arrow keys or WASD. Pause with P or Escape. Good luck out there.</p>
<button>Start</button><canvas id="c"></canvas></body></html>"""

QUIET_GAME = """<!doctype html><html><body><h1>Fever Dungeon</h1><button>Play</button><canvas id="c"></canvas></body></html>"""

REPORT = """<!doctype html><html><body><h1>Quarterly findings</h1>
<p>The migration finished two weeks ahead of plan and the error rate fell by a third over the same period.</p>
<p>Three services still write to the old table. Each one is listed below with its owner and the date it moves.</p>
<h2>Detail</h2><p>The largest remaining writer is the billing exporter, which runs nightly and takes about four minutes.</p>
</body></html>"""


def hook(tmp_path, event):
    env = {**os.environ, "XDG_CACHE_HOME": str(tmp_path / "cache")}
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), capture_output=True, text=True, env=env)


def write_event(f, session="s"):
    return {"session_id": session, "tool_name": "Write", "tool_input": {"file_path": str(f)}}


def test_hook_fires_on_a_wordy_new_game_then_stays_quiet(tmp_path):
    f = tmp_path / "game.html"
    f.write_text(GAME)
    first, second = hook(tmp_path, write_event(f)), hook(tmp_path, write_event(f))
    assert first.returncode == 2 and "Stand on a blue tile" in first.stderr
    assert second.returncode == 0 and second.stderr == ""


def test_hook_is_silent_on_a_quiet_game(tmp_path):
    f = tmp_path / "game.html"
    f.write_text(QUIET_GAME)
    assert hook(tmp_path, write_event(f)).returncode == 0


def test_hook_is_silent_on_a_prose_report(tmp_path):
    f = tmp_path / "report.html"
    f.write_text(REPORT)
    assert hook(tmp_path, write_event(f)).returncode == 0


def test_hook_does_not_blame_an_edit_for_copy_already_there(tmp_path):
    f = tmp_path / "p.tsx"
    f.write_text(TSX.replace("useState<boolean>(false)", "useState<boolean>(true)"))
    ev = {"session_id": "s", "tool_name": "Edit", "tool_input": {
        "file_path": str(f), "old_string": "useState<boolean>(false)", "new_string": "useState<boolean>(true)"}}
    assert hook(tmp_path, ev).returncode == 0


def test_hook_fires_on_an_edit_that_adds_subtitles(tmp_path):
    f = tmp_path / "p.tsx"
    f.write_text(TSX)
    old = "<div>\n      <Dialog>"
    ev = {"session_id": "s", "tool_name": "Edit", "tool_input": {"file_path": str(f), "old_string": "<div>\n    </div>",
          "new_string": TSX[TSX.index("<div>"):TSX.index("</div>") + 6]}}
    r = hook(tmp_path, ev)
    assert r.returncode == 2 and "Manage your settings" in r.stderr
