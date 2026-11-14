"""Claude Code transcript -> EverOS MessageItems (OpenAI chat shape, tool calls kept).

EverOS only builds an agent_case from a trajectory it can see. That means
assistant messages carrying `tool_calls`, and `tool` rows answering them by
`tool_call_id`. This module turns one Claude session JSONL into that shape:

- assistant lines that share a message id merge into one item;
- `tool_use` blocks become `tool_calls` (arguments as a JSON string);
- `tool_result` blocks become `role: tool` rows, in the order they arrived;
- thinking blocks, meta lines, and non-message lines (summaries, attachments,
  progress) are dropped.

stdlib only. The EverOS request contract this targets is MessageItemDTO in
everos/entrypoints/api/routes/memorize.py (v1.6).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Iterable, Iterator, List, Optional

MAX_MESSAGES_PER_ADD = 500     # MemorizeAddRequest.messages max_length
TOOL_OUTPUT_CAP = 4000         # chars of one tool result kept for extraction
CASE_MIN_TOOL_ROUNDS = 3       # fewer rounds than this never yields an agent_case


def _ms(ts: Optional[str]) -> Optional[int]:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _text(content) -> str:
    """Plain text from a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _cap(s: str, cap: int) -> str:
    return s if len(s) <= cap else s[:cap] + f"\n[... {len(s) - cap} chars cut]"


def records(lines: Iterable[str]) -> Iterator[dict]:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            yield rec


def claude_messages(lines: Iterable[str], *, agent_id: str = "claude-code",
                    user_id: str = "operator", tool_cap: int = TOOL_OUTPUT_CAP) -> List[dict]:
    out: List[dict] = []
    last_ms = 1
    open_assistant: Optional[str] = None      # message id of the item being merged into

    for rec in records(lines):
        kind = rec.get("type")
        msg = rec.get("message")
        if kind not in ("user", "assistant") or not isinstance(msg, dict) or rec.get("isMeta"):
            continue
        ts = _ms(rec.get("timestamp")) or last_ms
        last_ms = ts
        content = msg.get("content")

        if kind == "assistant":
            text = _text(content)
            calls = []
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                    calls.append({"id": block["id"], "type": "function", "function": {
                        "name": str(block.get("name") or "tool"),
                        "arguments": json.dumps(block.get("input") or {}, sort_keys=True)}})
            if not text and not calls:
                continue                          # thinking-only line
            mid = msg.get("id")
            if mid and mid == open_assistant and out and out[-1]["role"] == "assistant":
                item = out[-1]
                if text:
                    item["content"] = f"{item['content']}\n{text}" if item["content"] else text
                if calls:
                    item.setdefault("tool_calls", []).extend(calls)
                continue
            item = {"sender_id": agent_id, "role": "assistant", "timestamp": ts, "content": text}
            if calls:
                item["tool_calls"] = calls
            out.append(item)
            open_assistant = mid
            continue

        open_assistant = None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id"):
                    body = _text(block.get("content"))
                    if block.get("is_error"):
                        body = f"[error] {body}"
                    out.append({"sender_id": agent_id, "role": "tool", "timestamp": ts,
                                "content": _cap(body, tool_cap), "tool_call_id": block["tool_use_id"]})
        text = _text(content)
        if text.strip():
            out.append({"sender_id": user_id, "role": "user", "timestamp": ts, "content": text})
    return _drop_orphans(out)


def _drop_orphans(items: List[dict]) -> List[dict]:
    """EverOS rejects a tool row whose call it never saw (422). Drop those rows."""
    seen = set()
    kept = []
    for item in items:
        for call in item.get("tool_calls") or []:
            seen.add(call["id"])
        if item["role"] == "tool" and item["tool_call_id"] not in seen:
            continue
        kept.append(item)
    return kept


def tool_rounds(items: List[dict]) -> int:
    """Assistant turns that called a tool and got at least one answer back."""
    answered = {i["tool_call_id"] for i in items if i["role"] == "tool"}
    return sum(1 for i in items if i["role"] == "assistant"
               and any(c["id"] in answered for c in i.get("tool_calls") or []))


def batches(items: List[dict], size: int = MAX_MESSAGES_PER_ADD) -> Iterator[List[dict]]:
    """Request-sized chunks. A tool row never starts a chunk apart from its call."""
    start = 0
    while start < len(items):
        end = min(start + size, len(items))
        while end < len(items) and end > start + 1 and items[end]["role"] == "tool":
            end -= 1
        yield items[start:end]
        start = end


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="map a Claude transcript to EverOS MessageItems (no network)")
    p.add_argument("transcript")
    p.add_argument("--dump", action="store_true", help="print the items, not just the summary")
    a = p.parse_args(argv)
    with open(a.transcript, encoding="utf-8", errors="replace") as f:
        items = claude_messages(f)
    if a.dump:
        print(json.dumps(items, indent=1))
    rounds = tool_rounds(items)
    print(json.dumps({"messages": len(items), "tool_rounds": rounds,
                      "requests": sum(1 for _ in batches(items)),
                      "case_eligible": rounds >= CASE_MIN_TOOL_ROUNDS}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
