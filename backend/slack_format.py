"""Pure helpers that turn council output into Slack messages.

No Slack SDK imports here, so this module is easy to unit test
(see tests/test_slack_format.py).
"""

import re
from typing import Any, Dict, List, Optional

# Slack truncates `text` at 40k chars but recommends staying under 4k so
# messages render well and don't get collapsed oddly. Leave headroom for
# labels and "(1/3)" suffixes.
SLACK_CHUNK_LIMIT = 3500

_FENCE_RE = re.compile(r"^\s*```")


def _escape(text: str) -> str:
    """Escape the three characters Slack treats as control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _convert_inline(line: str) -> str:
    """Convert inline Markdown (bold/italic/links/strike) on one line, sparing `code` spans."""
    out = []
    for i, part in enumerate(re.split(r"(`[^`\n]+`)", line)):
        if i % 2 == 1:  # inline code span: keep as-is (escaped)
            out.append(_escape(part))
            continue
        # Pull links out first so their URLs aren't mangled by later rules.
        links: List[str] = []

        def _link(m: "re.Match[str]") -> str:
            label, url = m.group(1), m.group(2)
            links.append(f"<{url}|{_escape(label)}>")
            return f"\x00{len(links) - 1}\x00"

        part = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", _link, part)
        part = _escape(part)
        # Bold+italic / bold / italic. Use \x01 as a temporary bold marker so
        # the italic rule doesn't re-match it.
        part = re.sub(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*", "\x01_\\1_\x01", part)
        part = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", "\x01\\1\x01", part)
        part = re.sub(r"__(?=\S)(.+?)(?<=\S)__", "\x01\\1\x01", part)
        part = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"_\1_", part)
        part = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"~\1~", part)
        part = part.replace("\x01", "*")
        part = re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], part)
        out.append(part)
    return "".join(out)


def _is_table_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and s.count("|") >= 2


def md_to_mrkdwn(md: str) -> str:
    """Best-effort conversion of GitHub-flavoured Markdown to Slack mrkdwn.

    - fenced code blocks are kept verbatim (language tag dropped)
    - headings become bold lines
    - **bold** -> *bold*, *italic* -> _italic_, ~~strike~~ -> ~strike~
    - [text](url) -> <url|text>
    - bullets become "•", horizontal rules are dropped
    - Markdown tables are wrapped in a code block so columns stay aligned
    """
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out: List[str] = []
    in_code = False
    table: List[str] = []

    def flush_table() -> None:
        if table:
            out.append("```")
            out.extend(_escape(t) for t in table if not re.fullmatch(r"\s*\|?[\s:\-|]+\|?\s*", t))
            out.append("```")
            table.clear()

    for line in lines:
        if _FENCE_RE.match(line):
            flush_table()
            in_code = not in_code
            out.append("```")
            continue
        if in_code:
            out.append(_escape(line))
            continue
        if _is_table_line(line):
            table.append(line)
            continue
        flush_table()

        m = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            title = _convert_inline(m.group(1)).replace("*", "").strip()
            out.append(f"*{title}*" if title else "")
            continue
        if re.fullmatch(r"\s{0,3}([-*_])(\s*\1){2,}\s*", line):
            continue  # horizontal rule
        m = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if m:
            indent = "    " * (len(m.group(1).replace("\t", "  ")) // 2)
            out.append(f"{indent}• {_convert_inline(m.group(2))}")
            continue
        m = re.match(r"^\s*>\s?(.*)$", line)
        if m:
            out.append(f">{_convert_inline(m.group(1))}")
            continue
        out.append(_convert_inline(line))

    flush_table()
    if in_code:  # unterminated fence in source
        out.append("```")
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_message(text: str, limit: int = SLACK_CHUNK_LIMIT) -> List[str]:
    """Split text into chunks <= limit chars.

    Prefers paragraph breaks, then line breaks, then spaces, then a hard cut.
    Code fences left open at a chunk boundary are closed and reopened in the
    next chunk so formatting survives the split.
    """
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []

    budget = limit - 8  # room to close/reopen a fence
    chunks: List[str] = []
    rest = text
    reopen = False
    while rest:
        if reopen:
            rest = "```\n" + rest
        if len(rest) <= limit:
            chunks.append(rest)
            break
        window = rest[:budget]
        cut = -1
        for sep in ("\n\n", "\n", " "):
            idx = window.rfind(sep)
            if idx > budget // 3:
                cut = idx
                break
        if cut == -1:
            cut = budget
        chunk, rest = rest[:cut].rstrip(), rest[cut:].lstrip("\n ")
        reopen = chunk.count("```") % 2 == 1
        if reopen:
            chunk += "\n```"
        chunks.append(chunk)
    return chunks


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = text.rfind(" ", 0, limit)
    if cut < limit // 2:
        cut = limit
    clipped = text[:cut].rstrip()
    if clipped.count("```") % 2 == 1:
        clipped += "\n```"
    return clipped + "\n_…(truncated)_"


def labeled(label: str, body_md: str, limit: int = SLACK_CHUNK_LIMIT) -> List[str]:
    """Convert body to mrkdwn, split it, and put `label` on the first chunk.

    Continuation chunks get a "(cont. 2/3)" header.
    """
    body = md_to_mrkdwn(body_md) or "_(empty)_"
    header_room = len(label) + 24
    parts = split_message(body, limit - header_room)
    n = len(parts)
    msgs = []
    for i, p in enumerate(parts, start=1):
        head = label if i == 1 else f"{label} _(cont. {i}/{n})_"
        if n > 1 and i == 1:
            head += f" _(1/{n})_"
        msgs.append(f"{head}\n{p}")
    return msgs


# ---------------------------------------------------------------------------
# Stage formatters
# ---------------------------------------------------------------------------

MEMBER_EMOJI = {"ChatGPT": ":large_green_circle:", "Claude": ":large_orange_circle:", "Grok": ":black_circle:"}


def _member_label(name: str) -> str:
    return f"{MEMBER_EMOJI.get(name, ':speech_balloon:')} *Stage 1 · {name}*"


def format_member_answer(name: str, response: Optional[str]) -> List[str]:
    if not response:
        return [f"{_member_label(name)}\n:warning: _{name} didn't answer this round (error or timeout). The council continues without it._"]
    return labeled(_member_label(name), response)


def deanonymize(text: str, label_to_model: Dict[str, str]) -> str:
    def rep(m: "re.Match[str]") -> str:
        lbl = m.group(0)
        return f"{lbl} ({label_to_model[lbl]})" if lbl in label_to_model else lbl
    return re.sub(r"Response [A-Z]\b", rep, text)


def format_rankings(
    stage2: List[Dict[str, Any]],
    label_to_model: Dict[str, str],
    aggregate: List[Dict[str, Any]],
    critique_chars: int = 1200,
    failed_reviewers: Optional[List[str]] = None,
) -> List[str]:
    """Stage 2: one summary message, then one (truncated) critique per reviewer."""
    lines = ["*Stage 2 · Peer review* (answers were anonymized; average position, lower is better)"]
    medals = [":first_place_medal:", ":second_place_medal:", ":third_place_medal:"]
    if aggregate:
        for i, a in enumerate(aggregate):
            medal = medals[i] if i < len(medals) else f"{i + 1}."
            votes = a.get("rankings_count", 0)
            lines.append(f"{medal} *{a['model']}* — avg {a['average_rank']} ({votes} vote{'s' if votes != 1 else ''})")
    else:
        lines.append("_No parsable rankings came back._")
    for r in stage2:
        order = [label_to_model[lbl] for lbl in r.get("parsed_ranking", []) if lbl in label_to_model]
        if order:
            lines.append(f"• {r['model']} ranked: {' > '.join(order)}")
    for name in failed_reviewers or []:
        lines.append(f":warning: _{name} didn't submit a review._")
    msgs = ["\n".join(lines)]

    for r in stage2:
        body = deanonymize(r.get("ranking", ""), label_to_model)
        body = body.split("FINAL RANKING:")[0].strip() or body
        body = truncate(body, critique_chars)
        msgs.extend(labeled(f"_Critique by {r['model']}_", body))
    return msgs


def format_verdict(stage3: Dict[str, Any]) -> List[str]:
    model = stage3.get("model", "Chairman")
    resp = stage3.get("response") or ""
    if model == "error" or resp.startswith("Error:"):
        return [f":warning: *Chairman verdict unavailable*\n_{resp or 'Unknown error'}_"]
    return labeled(f":scales: *Chairman verdict* ({model})", resp)


def convening_text(member_names: List[str], chairman: str) -> str:
    names = ", ".join(member_names[:-1]) + (" and " if len(member_names) > 1 else "") + member_names[-1]
    return (f":classical_building: Convening the council… {names} answer, review each other "
            f"anonymously, then {chairman} delivers the verdict (usually about a minute).")


VERDICT_PREFIX = ":scales: *Chairman verdict*"


# ---------------------------------------------------------------------------
# Thread follow-ups
# ---------------------------------------------------------------------------

def strip_mentions(text: str) -> str:
    """Remove <@U123> user mentions (e.g. the bot's own) and tidy whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"<@[A-Z0-9]+(\|[^>]*)?>", "", text or "")).strip()


def build_followup_question(history: List[Dict[str, str]], question: str, max_chars: int = 8000) -> str:
    """Fold earlier thread turns into a new council question.

    `history` is a list of {"role": "user"|"council", "text": ...} oldest first.
    Keeps the most recent turns that fit in `max_chars`.
    """
    turns = [h for h in history if h.get("text", "").strip()]
    if not turns:
        return question
    kept: List[str] = []
    used = 0
    for h in reversed(turns):
        who = "User" if h["role"] == "user" else "Council verdict"
        piece = f"{who}: {h['text'].strip()}"
        if used + len(piece) > max_chars:
            if not kept:
                kept.append(piece[: max_chars] + " …")
            break
        kept.append(piece)
        used += len(piece)
    context = "\n\n".join(reversed(kept))
    return (
        "This is a follow-up in an ongoing council discussion. Earlier in the thread:\n\n"
        f"{context}\n\n"
        f"Follow-up question: {question}"
    )
