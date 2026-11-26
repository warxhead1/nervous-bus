#!/usr/bin/env python3
"""ste_check: a small heuristic linter for AI tells and STE-style (ASD-STE100-inspired) English.

Part of the ste80-humanizer skill. It finds places to look at.
It does not prove compliance with ASD-STE100 and it is not an AI detector.
No dependencies. Python 3.8+.

Usage:
    python ste_check.py FILE [FILE ...]
    python ste_check.py --strict FILE
    python ste_check.py --ignore passive,word FILE
    python ste_check.py --select not-x-but-y,chatbot FILE
    python ste_check.py --max-findings 5 FILE
    python ste_check.py --list-rules
    python ste_check.py --compare ORIGINAL REWRITE
    cat draft.txt | python ste_check.py -
    python ste_check.py --json FILE

Inline suppression comments (ignored inside code blocks and inline code):
    <!-- ste-check-disable -->
    <!-- ste-check-disable passive word -->
    <!-- ste-check-enable -->
    <!-- ste-check-enable passive -->
    <!-- ste-check-disable-next-line passive -->

An unknown rule name in a comment gives a 'directive' finding.

--compare lists numbers, code, CLI flags, URLs, quotes and hedges that are in
ORIGINAL but not in REWRITE (fidelity-* rules). It does not check meaning.

Exit code: 0 if findings <= --max-findings (default 0), 1 if more, 2 on usage error.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
import re
import sys
from dataclasses import dataclass, asdict

# --- Word lists (short, paraphrased working lists; not the ASD dictionary) ---

SWAPS = {
    "utilize": "use", "utilise": "use", "leverage": "use", "employ": "use",
    "perform": "do", "conduct": "do", "accomplish": "do", "facilitate": "help, let",
    "ensure": "make sure", "verify": "make sure", "commence": "start",
    "initiate": "start", "terminate": "stop", "eliminate": "remove",
    "modify": "change", "indicate": "show", "demonstrate": "show",
    "obtain": "get", "acquire": "get", "provide": "give", "assist": "help",
    "attempt": "try", "retain": "keep", "observe": "monitor, see",
    "locate": "find", "determine": "find", "subsequently": "then",
    "however": "but", "therefore": "thus, as a result", "hence": "thus",
    "furthermore": "also", "moreover": "also", "additionally": "also",
    "whereas": "but", "via": "through", "whether": "if",
    "numerous": "many", "adequate": "sufficient", "identical": "same",
    "simultaneously": "at the same time", "prior to": "before",
    "due to": "because of", "in order to": "to", "in the event that": "if",
    "it is important to note": "(delete)", "it's important to note": "(delete)",
    "it should be noted": "(delete)",
    "delve": "examine", "robust": "strong, reliable", "seamless": "(delete)",
    "seamlessly": "(delete)", "comprehensive": "full", "crucial": "important",
    "pivotal": "important", "landscape": "field, area", "realm": "field, area",
    "showcase": "show", "streamline": "simplify", "empower": "let, help",
    "harness": "use", "foster": "help", "underscore": "show",
    "game-changer": "(delete)", "tapestry": "(delete)", "testament": "(delete) or say what it shows",
    "elevate": "improve", "unlock": "let, give", "intricate": "complex", "meticulous": "careful",
    "vibrant": "(delete)", "enhance": "improve", "groundbreaking": "(fact or delete)",
    "cutting-edge": "(fact or delete)", "world-class": "(fact or delete)", "best-in-class": "(fact or delete)",
    "renowned": "(fact or delete)", "stunning": "(delete)", "breathtaking": "(delete)",
    "serves as": "is", "stands as": "is", "boasts": "has",
}

STRICT_ONLY_SWAPS = {
    "need": "necessary (adj)", "may": "can", "might": "can", "could": "can",
    "should": "must", "shall": "must", "would": "will", "since": "because",
    "both": "the two", "further": "more", "additional": "more",
    "acceptable": "permitted", "main": "primary", "portion": "part",
    "required": "necessary", "insert": "put", "rotate": "turn",
    "avoid": "prevent", "follow": "obey", "inspect": "examine",
    "e.g.": "for example", "i.e.": "that is", "etc.": "and other",
}

TELLS = [
    (r"\b(?:it'?s|it is|this is|that'?s|is)\s+not\s+(?:just|only|merely|simply|about)\b", "not-x-but-y", "'not just X, but Y' framing: state the point"),
    (r"\bnot\s+(?:just|only|merely)\b[^.]{0,80}\bbut\b", "not-x-but-y", "'not just X but Y' framing: state the point"),
    (r"\bisn'?t\s+(?:just\s+)?about\b", "not-x-but-y", "'this isn't about X' framing: state the point"),
    (r"^(?:great question|certainly|of course|sure|absolutely)[!,.]", "chatbot", "chatbot opener: delete"),
    (r"\bi hope this helps\b|\blet me know if\b|\bwould you like me to\b|\bfeel free to\b|\bhappy to help\b", "chatbot", "chatbot closer: delete"),
    (r"\blet'?s (?:dive|delve|break (?:this|it) down|explore|unpack)\b|\bhere'?s the thing\b|\bwithout further ado\b", "run-up", "run-up before the point: start with the point"),
    (r"\bat its core\b|\bthe real question is\b|\bfundamentally\b|\bat the end of the day\b", "fake-depth", "fake depth: say the plain claim"),
    (r"\blet that sink in\b|\band that changes everything\b|\bthat'?s the real (?:win|story)\b|\bread that again\b", "closer", "dramatic closer: delete"),
    (r"\bdon'?t get me wrong\b|\bto be clear\b|\bone might be tempted\b", "strawman", "arguing with nobody: remove"),
    (r"\bas of my (?:last|latest) (?:update|training)\b|\bbased on (?:the )?available information\b", "knowledge-hedge", "knowledge-limit hedge: delete"),
    (r"\bin this (?:guide|article|section|post),? (?:we|i)(?:'ll| will)\b", "meta", "writing about the text: give the content"),
    (r"\b(?:could|may|might) (?:potentially|possibly|arguably)\b", "stacked-hedge", "stacked hedge: keep one hedge"),
    (r"\b(?:experts|studies|many people|observers) (?:say|agree|show|believe|suggest)\b", "vague-authority", "vague authority: name the source or delete"),
    (r",\s+(?:highlighting|underscoring|emphasizing|ensuring|reflecting|showcasing|fostering|cultivating|contributing to)\b", "ing-rider", "shallow '-ing' rider: delete or make it a sentence"),
    (r"\b(?:plays|play|played) a (?:pivotal|crucial|key|vital) role\b|\bmarks? a (?:significant|major) milestone\b|\bthe future looks bright\b", "inflation", "inflated significance: say what happened"),
    (r"\b(?:is|are|was|were) associated with\b|\b(?:in connection with|linked to|tied to)\b", "vague-association", "vague connection: name the relation (founded, caused, part of)"),
    (r"\b(?:is|are|was|were|be)(?:\s+\w+ly)?\s+(?:high-quality|well-known|well-documented|long-term|real-time|well-tested|client-facing)\b", "hyphen-pair", "drop the hyphen after the noun ('is well documented')"),
    (r"^(?:that'?s|this is) (?:the real|why it|what) ", "closer", "possible one-line closer: cut it if it repeats the point"),
]

BUILTIN_RULES = frozenset({
    "contraction",
    "curly-quotes",
    "dash",
    "directive",
    "ing-form",
    "modal-passive",
    "noun-only",
    "paragraph-length",
    "passive",
    "perfect-tense",
    "phrasal-verb",
    "progressive",
    "semicolon",
    "sentence-length",
    "word",
})

FIDELITY_RULES = frozenset({
    "fidelity-certainty",
    "fidelity-code",
    "fidelity-number",
    "fidelity-quote",
    "fidelity-url",
})

ALL_RULES = frozenset(BUILTIN_RULES | FIDELITY_RULES | {rule for _, rule, _ in TELLS})

DIRECTIVE_RE = re.compile(
    r"<!--\s*ste-check-(disable-next-line|disable|enable)(?:\s+([^>]*?))?\s*-->",
    re.I,
)

PHRASAL = [
    "set up", "find out", "look into", "look at", "fill in", "fill out",
    "turn on", "turn off", "switch on", "switch off", "go through",
    "point out", "put together", "take out", "come up with", "give up",
    "carry out", "figure out", "check out", "back up", "shut down",
]

ING_NOUN_OK = {
    "during", "following", "opening", "remaining", "according", "something",
    "nothing", "anything", "everything", "thing", "things", "string", "strings",
    "bring", "ring", "spring", "king", "wing", "sing", "single", "logging",
    "caching", "routing", "testing", "debugging", "networking", "staging",
    "setting", "settings", "mapping", "warning", "warnings", "building",
    "meaning", "morning", "evening", "ceiling", "padding", "spacing",
    "rendering", "hosting", "tooling", "training", "pricing", "billing",
    "encoding", "streaming", "scheduling", "parsing", "linting", "formatting",
    "being", "missing", "existing", "incoming", "outgoing", "pending",
}

NOT_PARTICIPLE = {
    "different", "present", "absent", "current", "recent", "frequent", "silent",
    "independent", "efficient", "sufficient", "apparent", "consistent", "important",
    "relevant", "urgent", "evident", "excellent", "competent", "dependent", "transparent",
    "open", "even", "often", "golden", "hidden", "sudden", "wooden", "own", "down",
    "shown", "known", "unknown", "brown", "grown", "drawn", "built", "content",
    "intent", "event", "agent", "client", "point", "front", "want", "difficult",
}

AUX = r"(?:am|is|are|was|were|be|been|being)"
PASSIVE_RE = re.compile(rf"\b{AUX}\s+(?:\w+ly\s+)?(\w+(?:ed|en|wn|lt|nt|pt|ung|ade|one|ept|ought))\b", re.I)
PERFECT_RE = re.compile(r"\b(?:has|have|had)\s+(?:not\s+)?(?:been|\w+ed|\w+en|done|made|gone|seen|run|set|put)\b", re.I)
PROGRESSIVE_RE = re.compile(rf"\b{AUX}\s+(?:not\s+)?(\w+ing)\b", re.I)
MODAL_PASSIVE_RE = re.compile(r"\b(?:can|could|may|might|must|should|shall|will|would)\s+(?:not\s+)?be\s+\w+(?:ed|en)\b", re.I)
CONTRACTION_RE = re.compile(r"\b\w+(?:n't|'re|'ve|'ll|'d|'m)\b|\b(?:it|that|there|what|here)'s\b", re.I)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*|\d[\w.,:/%-]*")

INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
BARE_URL_RE = re.compile(r"https?://[^\s)>\]\"']+")
# A number with an optional unit or suffix: 40+, 99.9%, 4.0.0, v2, 30s, 100ms, 8GB, 2x.
NUMBER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])v?\d+(?:[.,]\d+)*(?:\+|%|[A-Za-z]{1,5})?(?![A-Za-z0-9_])", re.I)
CLI_FLAG_RE = re.compile(r"(?<![\w-])--[A-Za-z][\w-]*")
QUOTE_SPAN_RE = re.compile(r"\"([^\"\n]{4,})\"|“([^”\n]{4,})”")
HEDGE_SOURCE_RE = re.compile(
    r"\b(?:may|might|could|probably|possibly|perhaps|likely|sometimes|usually|in most cases|tends?\s+to)\b",
    re.I,
)
HEDGE_PRESERVED_RE = re.compile(
    r"\b(?:may|might|could|can|probably|possibly|perhaps|likely|sometimes|usually|in most cases|tends?\s+to)\b",
    re.I,
)



@dataclass
class Finding:
    line: int
    rule: str
    message: str
    excerpt: str


def _parse_directive_rules(raw: str | None) -> set[str]:
    if not raw or not raw.strip():
        return set()
    return {tok.strip().lower() for tok in re.split(r"[\s,]+", raw.strip()) if tok.strip()}


def parse_suppressions(text: str) -> tuple[dict[int, set[str] | None], list[tuple[int, str]]]:
    """Return ({line_no: None (all rules) or set[str] (named rules)}, [(line_no, unknown_rule)]).

    Directives inside YAML front matter, fenced code blocks, indented code blocks,
    or inline backtick spans are ignored.
    """
    suppressed: dict[int, set[str] | None] = {}
    unknown: list[tuple[int, str]] = []
    in_code = False
    in_front = False
    block_all = False
    block_rules: set[str] = set()
    next_line_all = False
    next_line_rules: set[str] = set()

    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if i == 1 and line.strip() == "---":
            in_front = True
            continue
        if in_front:
            if line.strip() == "---":
                in_front = False
            continue
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
            next_line_all = False
            next_line_rules.clear()
            continue
        if in_code or line.startswith("    "):
            next_line_all = False
            next_line_rules.clear()
            continue

        # Capture any pending next-line suppression for this line before updating it.
        cur_next_all = next_line_all
        cur_next_rules = set(next_line_rules)
        next_line_all = False
        next_line_rules.clear()

        # Ignore directives written inside inline code spans.
        scannable = re.sub(r"`[^`]*`", "", line)
        matches = list(DIRECTIVE_RE.finditer(scannable))
        for m in matches:
            action = m.group(1).lower()
            rules = _parse_directive_rules(m.group(2))
            unknown.extend((i, name) for name in sorted(rules - ALL_RULES))
            if action == "disable":
                if not rules:
                    block_all = True
                else:
                    block_rules.update(rules)
            elif action == "enable":
                if not rules:
                    block_all = False
                    block_rules.clear()
                else:
                    if block_all:
                        block_all = False
                        block_rules = set(ALL_RULES) - rules
                    else:
                        block_rules.difference_update(rules)
            elif action == "disable-next-line":
                if not rules:
                    next_line_all = True
                else:
                    next_line_rules.update(rules)

        if block_all or cur_next_all:
            suppressed[i] = None
        else:
            combined = block_rules | cur_next_rules
            if combined:
                suppressed[i] = set(combined)

    return suppressed, unknown


def _is_suppressed(suppressions: dict[int, set[str] | None], line_no: int, rule: str) -> bool:
    if line_no not in suppressions:
        return False
    entry = suppressions[line_no]
    return entry is None or rule in entry


def strip_markdown(text: str) -> list[tuple[int, str]]:
    """Return (line_no, line) for prose lines. Drop code blocks, tables, front matter."""
    out = []
    in_code = False
    in_front = False
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if i == 1 and line.strip() == "---":
            in_front = True
            continue
        if in_front:
            if line.strip() == "---":
                in_front = False
            continue
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code or line.lstrip().startswith("|") or line.startswith("    "):
            out.append((i, ""))
            continue
        line = re.sub(r"`[^`]*`", "CODE", line)            # inline code = 1 word
        line = DIRECTIVE_RE.sub("", line)                  # inline ste-check directives
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)  # links
        line = re.sub(r"https?://\S+", "URL", line)
        if re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", line):
            out.append((i, ""))                               # each list item = own paragraph
            line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)
        line = re.sub(r"^\s*>\s?", "", line)                   # quotes
        line = re.sub(r"^#+\s+", "", line)                     # headings
        line = re.sub(r"\*\*|__|\*|_", "", line)
        out.append((i, line))
    return out


def paragraphs(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    paras, buf, start = [], [], None
    for no, line in lines:
        if line.strip():
            if start is None:
                start = no
            buf.append(line.strip())
        elif buf:
            paras.append((start, " ".join(buf)))
            buf, start = [], None
    if buf:
        paras.append((start, " ".join(buf)))
    return paras


def sentences(paragraph: str) -> list[str]:
    protected = re.sub(r"\b(e\.g|i\.e|etc|vs|Dr|Mr|Ms|No)\.", lambda m: m.group(0).replace(".", "§"), paragraph)
    parts = re.split(r"(?<=[.!?:])\s+(?=[A-Z0-9\"'(])", protected)
    return [p.replace("§", ".").strip() for p in parts if p.strip()]


def count_words(sentence: str) -> int:
    s = re.sub(r"\([^)]*\)", " PAREN ", sentence)         # parentheses = 1 word
    s = re.sub(r"\"[^\"]*\"|“[^”]*”", " QUOTE ", s)        # quoted text = 1 word
    s = re.sub(r"\b(\d[\d.,]*)\s*(%|ms|s|kb|mb|gb|tb|px|kg|g|m|cm|mm|km|h|min)\b", r"\1", s, flags=re.I)
    return len(WORD_RE.findall(s))


def is_imperative(sentence: str) -> bool:
    first = (WORD_RE.findall(sentence) or [""])[0].lower()
    return first in {
        "add", "apply", "attach", "click", "close", "configure", "connect", "copy",
        "create", "delete", "disconnect", "do", "download", "edit", "enter", "examine",
        "find", "get", "give", "install", "keep", "let", "make", "measure", "monitor",
        "move", "open", "push", "pull", "put", "read", "remove", "replace", "restart",
        "run", "select", "send", "set", "start", "stop", "turn", "type", "update",
        "upload", "use", "write", "if", "when", "before", "after", "to",
    }


def _scan_blocks(text: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Return (prose_lines, fenced_blocks) after skipping YAML front matter."""
    prose: list[tuple[int, str]] = []
    fenced: list[tuple[int, str]] = []
    in_front = False
    in_code = False
    fence_start = 0
    fence_buf: list[str] = []

    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if i == 1 and line.strip() == "---":
            in_front = True
            continue
        if in_front:
            if line.strip() == "---":
                in_front = False
            continue
        if line.lstrip().startswith(("```", "~~~")):
            if not in_code:
                in_code = True
                fence_start = i
                fence_buf = []
            else:
                in_code = False
                fenced.append((fence_start, "\n".join(fence_buf).strip()))
            continue
        if in_code:
            fence_buf.append(line)
            continue
        prose.append((i, line))
    if in_code and fence_buf:
        fenced.append((fence_start, "\n".join(fence_buf).strip()))
    return prose, fenced


def _extract_fidelity_items(text: str) -> dict:
    """Extract factual anchors (code, URLs, numbers, quotes, hedges) with line numbers."""
    prose, fenced = _scan_blocks(text)
    inline_code: list[tuple[int, str, str]] = []
    urls: list[tuple[int, str, str]] = []
    numbers: list[tuple[int, str, str]] = []
    quotes: list[tuple[int, str, str]] = []
    hedges: list[tuple[int, str, str]] = []
    preserved_hedges: list[str] = []

    for no, raw_line in prose:
        excerpt = raw_line.strip()[:100]
        for m in INLINE_CODE_RE.finditer(raw_line):
            inline_code.append((no, m.group(1), excerpt))

        no_code = INLINE_CODE_RE.sub(" ", raw_line)
        for m in CLI_FLAG_RE.finditer(no_code):
            inline_code.append((no, m.group(0), excerpt))
        no_code = CLI_FLAG_RE.sub(" ", no_code)

        for m in MD_LINK_RE.finditer(no_code):
            target = m.group(1).strip()
            if target:
                urls.append((no, target, excerpt))
        no_links = MD_LINK_RE.sub(" ", no_code)
        for m in BARE_URL_RE.finditer(no_links):
            target = m.group(0).rstrip(".,;:!?")
            if target:
                urls.append((no, target, excerpt))
        clean = BARE_URL_RE.sub(" ", no_links)

        for m in QUOTE_SPAN_RE.finditer(clean):
            q = (m.group(1) or m.group(2) or "").strip()
            if q:
                quotes.append((no, q, excerpt))

        # Strip leading Markdown list numbering ("1. ", "2) ") so re-formatting lists does not false-positive.
        num_line = re.sub(r"^\s*(?:>\s*)*(?:\d+[.)])\s+", "", clean)
        for m in NUMBER_TOKEN_RE.finditer(num_line):
            numbers.append((no, m.group(0).lower(), excerpt))

        for m in HEDGE_SOURCE_RE.finditer(clean):
            hedges.append((no, m.group(0).lower(), excerpt))
        for m in HEDGE_PRESERVED_RE.finditer(clean):
            preserved_hedges.append(m.group(0).lower())

    return {
        "fenced": fenced,
        "inline_code": inline_code,
        "urls": urls,
        "numbers": numbers,
        "quotes": quotes,
        "hedges": hedges,
        "preserved_hedges": preserved_hedges,
    }


def compare_texts(original: str, rewrite: str) -> list[Finding]:
    """Compare original and rewritten text for dropped code, URLs, numbers, quotes, or uncertainty."""
    orig = _extract_fidelity_items(original)
    rew = _extract_fidelity_items(rewrite)
    findings: list[Finding] = []

    # 1. Fenced code blocks
    rew_fenced = Counter(block for _, block in rew["fenced"] if block)
    for line, block in orig["fenced"]:
        if not block:
            continue
        if rew_fenced[block] > 0:
            rew_fenced[block] -= 1
        else:
            first_line = block.splitlines()[0][:80]
            findings.append(
                Finding(line, "fidelity-code", "fenced code block from original missing or altered in rewrite", first_line)
            )

    # 2. Inline code spans
    rew_inline = Counter(tok for _, tok, _ in rew["inline_code"])
    for line, tok, excerpt in orig["inline_code"]:
        if rew_inline[tok] > 0:
            rew_inline[tok] -= 1
        else:
            findings.append(
                Finding(line, "fidelity-code", f"code `{tok}` from original missing in rewrite", excerpt)
            )

    # 3. URLs and Markdown link targets
    rew_urls = Counter(u for _, u, _ in rew["urls"])
    for line, u, excerpt in orig["urls"]:
        if rew_urls[u] > 0:
            rew_urls[u] -= 1
        else:
            findings.append(
                Finding(line, "fidelity-url", f"URL or link target '{u}' from original missing in rewrite", excerpt)
            )

    # 4. Numbers and quantifiers (for example, '40+' stays '40+', not '40' or '40-odd')
    rew_nums = Counter(n for _, n, _ in rew["numbers"])
    for line, n, excerpt in orig["numbers"]:
        if rew_nums[n] > 0:
            rew_nums[n] -= 1
        else:
            findings.append(
                Finding(line, "fidelity-number", f"number '{n}' from original missing or altered in rewrite", excerpt)
            )

    # 5. Verbatim quoted strings
    rew_quotes = Counter(q for _, q, _ in rew["quotes"])
    for line, q, excerpt in orig["quotes"]:
        if rew_quotes[q] > 0:
            rew_quotes[q] -= 1
        else:
            findings.append(
                Finding(line, "fidelity-quote", f"quoted text \"{q[:60]}\" from original missing or altered in rewrite", excerpt)
            )

    # 6. Certainty / uncertainty hedge preservation ('might fail' -> 'can fail' passes; 'fails' flags)
    if orig["hedges"] and not rew["preserved_hedges"]:
        first_line, _, first_excerpt = orig["hedges"][0]
        sample = ", ".join(dict.fromkeys(h for _, h, _ in orig["hedges"][:3]))
        findings.append(
            Finding(
                first_line,
                "fidelity-certainty",
                f"uncertainty hedge(s) ({sample}) from original dropped without 'can' or another qualifier in rewrite",
                first_excerpt,
            )
        )

    findings.sort(key=lambda x: (x.line, x.rule))
    return findings


def check(
    text: str,
    strict: bool,
    select: set[str] | None = None,
    ignore: set[str] | None = None,
) -> list[Finding]:
    f: list[Finding] = []
    suppressions, unknown_directive_rules = parse_suppressions(text)
    lines = strip_markdown(text)
    raw_by_no = dict(enumerate(text.splitlines(), 1))

    for start, para in paragraphs(lines):
        sents = sentences(para)
        if len(sents) > 6:
            f.append(Finding(start, "paragraph-length", f"{len(sents)} sentences in one paragraph (max 6)", para[:80]))
        for s in sents:
            n = count_words(s)
            limit = 20 if is_imperative(s) else 25
            slack = 0 if strict else 3
            if n > limit + slack:
                kind = "instruction" if limit == 20 else "description"
                f.append(Finding(start, "sentence-length", f"{n} words ({kind} max {limit})", s[:100]))

    for no, line in lines:
        if not line.strip():
            continue
        low = line.lower()
        if ";" in line:
            f.append(Finding(no, "semicolon", "semicolon: write two sentences or a list", line.strip()[:100]))
        if "—" in line or " -- " in line:
            f.append(Finding(no, "dash", "em dash as sentence glue: use a period or colon", line.strip()[:100]))
        for m in PASSIVE_RE.finditer(line):
            if m.group(1).lower() in NOT_PARTICIPLE:
                continue
            f.append(Finding(no, "passive", f"possible passive voice: '{m.group(0)}'", line.strip()[:100]))
        for m in MODAL_PASSIVE_RE.finditer(line):
            f.append(Finding(no, "modal-passive", f"modal + passive: '{m.group(0)}'", line.strip()[:100]))
        for m in PERFECT_RE.finditer(line):
            f.append(Finding(no, "perfect-tense", f"perfect tense: '{m.group(0)}'", line.strip()[:100]))
        for m in PROGRESSIVE_RE.finditer(line):
            if m.group(1).lower() not in ING_NOUN_OK:
                f.append(Finding(no, "progressive", f"progressive tense: '{m.group(0)}'", line.strip()[:100]))
        for m in re.finditer(r"(?:^|[.,;:]\s+|\b(?:by|while|when|after|before)\s+)(\w+ing)\b", line, re.I):
            w = m.group(1).lower()
            if w not in ING_NOUN_OK and len(w) > 4:
                f.append(Finding(no, "ing-form", f"'-ing' verb form: '{m.group(1)}'", line.strip()[:100]))
        for p in PHRASAL:
            if re.search(rf"\b{re.escape(p)}\b", low):
                f.append(Finding(no, "phrasal-verb", f"phrasal verb '{p}'", line.strip()[:100]))
        for pat, rule, msg in TELLS:
            if re.search(pat, line, re.I):
                f.append(Finding(no, rule, msg, line.strip()[:100]))
        if re.search(r"[“”‘’]", raw_by_no.get(no, "")):
            f.append(Finding(no, "curly-quotes", "curly quotes: use straight quotes in Markdown and code", line.strip()[:100]))
        swaps = dict(SWAPS)
        if strict:
            swaps.update(STRICT_ONLY_SWAPS)
            if CONTRACTION_RE.search(line):
                f.append(Finding(no, "contraction", "contraction (not permitted in strict mode)", line.strip()[:100]))
            if re.search(r"\b(?:check|test)(?:s|ed)?\s+(?:the|a|an|that|if|your|all)\b", low):
                f.append(Finding(no, "noun-only", "'check'/'test' used as a verb (STE: noun only)", line.strip()[:100]))
        for word, alt in swaps.items():
            if re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", low):
                f.append(Finding(no, "word", f"'{word}' -> {alt}", line.strip()[:100]))

    filtered: list[Finding] = []
    for item in f:
        if select is not None and item.rule not in select:
            continue
        if ignore is not None and item.rule in ignore:
            continue
        if _is_suppressed(suppressions, item.line, item.rule):
            continue
        filtered.append(item)

    # A typo in a comment must not stay silent, so inline comments cannot hide these findings.
    if (select is None or "directive" in select) and (ignore is None or "directive" not in ignore):
        for no, name in unknown_directive_rules:
            filtered.append(Finding(no, "directive", f"unknown rule '{name}' in ste-check comment (see --list-rules)", name))

    filtered.sort(key=lambda x: (x.line, x.rule))
    return filtered


def summary(text: str) -> dict:
    paras = paragraphs(strip_markdown(text))
    sents = [s for _, p in paras for s in sentences(p)]
    counts = [count_words(s) for s in sents] or [0]
    return {
        "sentences": len(sents),
        "avg_words_per_sentence": round(sum(counts) / max(len(sents), 1), 1),
        "max_words_per_sentence": max(counts),
        "paragraphs": len(paras),
    }


def _parse_cli_rules(values: list[str], flag_name: str, ap: argparse.ArgumentParser) -> set[str]:
    rules: set[str] = set()
    for chunk in values:
        for item in chunk.split(","):
            name = item.strip().lower()
            if name:
                rules.add(name)
    unknown = sorted(rules - ALL_RULES)
    if unknown:
        ap.error(f"unknown rule(s) in {flag_name}: {', '.join(unknown)} (run --list-rules to see valid names)")
    return rules


def _read_input(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Heuristic linter for AI tells and STE-style English (not a compliance checker, not an AI detector).")
    ap.add_argument("files", nargs="*", help="files to check, or - for stdin")
    ap.add_argument("--strict", action="store_true", help="strict mode: exact limits, more word rules")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--select", action="append", default=[], metavar="RULES", help="comma-separated rule names to run (repeatable)")
    ap.add_argument("--ignore", action="append", default=[], metavar="RULES", help="comma-separated rule names to skip (repeatable)")
    ap.add_argument("--max-findings", type=int, default=0, metavar="N", help="exit 0 unless total findings exceed N (default 0)")
    ap.add_argument("--list-rules", action="store_true", help="print all supported rule names and exit")
    ap.add_argument(
        "--compare",
        nargs=2,
        metavar=("ORIGINAL", "REWRITE"),
        help="compare original and rewritten files for dropped numbers, code, URLs, quotes, or uncertainty",
    )
    args = ap.parse_args(argv)

    if args.list_rules:
        names = sorted(ALL_RULES)
        if args.json:
            print(json.dumps(names, indent=2))
        else:
            for name in names:
                print(name)
        return 0

    if args.max_findings < 0:
        ap.error("--max-findings must be a non-negative integer")

    if args.compare and args.files:
        ap.error("--compare cannot be combined with positional files")
    if not args.compare and not args.files:
        ap.error("the following arguments are required: files")

    select = _parse_cli_rules(args.select, "--select", ap) if args.select else None
    ignore = _parse_cli_rules(args.ignore, "--ignore", ap) if args.ignore else None

    def read(path: str) -> str:
        try:
            return _read_input(path)
        except (OSError, UnicodeDecodeError) as exc:
            ap.error(f"cannot read {path}: {exc}")

    if args.compare:
        orig_path, rew_path = args.compare
        if orig_path == "-" and rew_path == "-":
            ap.error("cannot read both ORIGINAL and REWRITE from stdin")
        orig_text = read(orig_path)
        rew_text = read(rew_path)
        findings = [
            x for x in compare_texts(orig_text, rew_text)
            if (select is None or x.rule in select) and (ignore is None or x.rule not in ignore)
        ]
        orig_stats = summary(orig_text)
        rew_stats = summary(rew_text)
        if args.json:
            payload = {
                "original": orig_path,
                "rewrite": rew_path,
                "original_stats": orig_stats,
                "rewrite_stats": rew_stats,
                "findings": [asdict(x) for x in findings],
            }
            print(json.dumps(payload, indent=2))
        else:
            print(
                f"compare {orig_path} -> {rew_path}: {len(findings)} finding(s) | "
                f"original {orig_stats['sentences']} sentences, rewrite {rew_stats['sentences']} sentences"
            )
            for x in findings:
                print(f"  {x.line:>4}  {x.rule:<18} {x.message}")
        return 1 if len(findings) > args.max_findings else 0

    total = 0
    report = []
    for path in args.files:
        text = read(path)
        findings = check(text, args.strict, select=select, ignore=ignore)
        total += len(findings)
        stats = summary(text)
        if args.json:
            report.append({"file": path, "stats": stats, "findings": [asdict(x) for x in findings]})
            continue
        print(f"{path}: {len(findings)} finding(s) | {stats['sentences']} sentences, "
              f"avg {stats['avg_words_per_sentence']} words, max {stats['max_words_per_sentence']}")
        for x in findings:
            print(f"  {x.line:>4}  {x.rule:<16} {x.message}")
    if args.json:
        print(json.dumps(report, indent=2))
    return 1 if total > args.max_findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
