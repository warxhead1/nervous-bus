"""Empty state copy, counted by human-attention's copy-audit: no subtitle under the heading,
no description slot, and the compose action wired."""
import json
import subprocess
import sys
from pathlib import Path

AUDIT = Path(__file__).resolve().parents[3] / "human-attention" / "skills" / "human-attention" / "scripts" / "copy-audit"


def grade(ctx):
    src = ctx.read("src/Inbox.jsx") or ""
    p = subprocess.run([sys.executable, str(AUDIT), str(ctx.repo / "src"), "--json"],
                       capture_output=True, text=True)
    total = json.loads(p.stdout)["total"] if p.returncode == 0 and p.stdout.strip() else {}
    checks = {
        "handles_empty": "length" in src or "!messages" in src or "messages?.length" in src,
        "compose_wired": "onCompose" in src.split("return", 1)[-1],
        "visible_words": total.get("words", -1),
        "subtitle_pairs": total.get("heading_subtitle_pairs", -1),
        "description_slots": total.get("description_slots", -1),
    }
    checks["score"] = (checks["handles_empty"] and checks["compose_wired"]
                       and checks["subtitle_pairs"] == 0 and checks["description_slots"] == 0)
    return checks
