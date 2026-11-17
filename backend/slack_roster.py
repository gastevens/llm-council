"""Slash commands and Block Kit for managing the War Room roster from Slack.

Shared with the TUI and `council_config` CLI: the same `~/.config/war-room/council.json`
file, the same validation (unique names, valid providers/models, at least
MIN_ENABLED members switched on, chair is a configured member).
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from .council_config import (
    HEX_TO_NAME,
    PALETTE,
    PROVIDERS,
    MemberConfig,
    Roster,
    RosterError,
    current_roster,
    config_path,
    load_roster,
    next_color,
    resolve_color,
    roster_lines,
    save_roster,
    suggested_models,
)
from .providers import Member

DENIED = (
    ":no_entry: Only War Room admins can change the council. "
    "You can still `/warroom roster` to view it, or ask a question."
)

HELP_TEXT = """*War Room commands*
`/warroom <question>` — convene the council
`/warroom roster` — show the council (buttons to edit, if you're allowed)
`/warroom add NAME provider [model] [color]` — add a member
`/warroom remove NAME`
`/warroom enable NAME`  /  `/warroom disable NAME`
`/warroom chair NAME` — set the chair
`/warroom title NAME` — who writes conversation titles
`/warroom models [provider]` — known model ids
`/warroom test NAME` — ping a member with a tiny prompt
`/warroom reset` — back to environment defaults
`/warroom context PATH` — have the council read a folder on the bot's Mac (this channel); `/warroom context` shows it, `/warroom context off` removes it. Or attach files to your question.
`/warroom help`

Providers: `codex`, `claude`, `grok`, `openrouter`. Two members on the same CLI need different names. Changes apply to the next council (Slack and the TUI share the same roster file)."""

USAGE_TEXT = "Usage: `/warroom <question>` (or `/warroom help` for roster commands)"

ZERO_ARG = frozenset({"help", "roster"})
NAME_COMMANDS = frozenset({"remove", "enable", "disable", "chair", "title", "test"})


@dataclass
class SlashParse:
    kind: str  # usage | help | roster | add | ... | question
    args: List[str] = field(default_factory=list)
    question: str = ""


@dataclass
class SlackReply:
    text: str
    blocks: Optional[List[Dict]] = None
    ok: bool = True
    ping: Optional[Member] = None  # bot should ping this member, then reply_from_ping


def admin_allowlist() -> Optional[Set[str]]:
    """Slack user IDs allowed to edit the roster, or None if anyone may edit."""
    raw = os.getenv("WARROOM_ADMINS", "") or ""
    ids = {p.strip() for p in raw.split(",") if p.strip()}
    return ids or None


def can_edit_roster(user_id: str) -> bool:
    allowed = admin_allowlist()
    return allowed is None or (user_id or "") in allowed


def parse_slash_text(text: str) -> SlashParse:
    """Classify `/warroom …` text as a roster subcommand or a council question.

    No-arg commands (`help`, `roster`) and `reset` only match when that's the
    whole text (`reset confirm` also counts). `add` requires a known provider
    as the second argument. Name commands match only with 0 or 1 argument, so
    `/warroom test the new policy` stays a question.
    """
    raw = (text or "").strip()
    if not raw:
        return SlashParse("usage")
    try:
        parts = shlex.split(raw)
    except ValueError:
        parts = raw.split()
    if not parts:
        return SlashParse("usage")
    cmd, args = parts[0].lower(), parts[1:]
    if cmd == "context":
        rest = raw.split(None, 1)[1].strip() if len(raw.split(None, 1)) > 1 else ""
        low = rest.lower()
        if (not rest or low in ("off", "clear", "none", "remove", "show")
                or low in ("--folder", "--inline", "--auto") or _looks_like_path(rest)):
            return SlashParse("context", [rest] if rest else [])
        return SlashParse("question", question=raw)
    if cmd in ZERO_ARG and not args:
        return SlashParse(cmd)
    if cmd == "reset" and (not args or args == ["confirm"]):
        return SlashParse("reset", args)
    if cmd == "models" and (not args or (len(args) == 1 and args[0].lower() in PROVIDERS)):
        return SlashParse("models", [a.lower() for a in args])
    if cmd == "add":
        if not args or len(args) == 1:
            return SlashParse("add", args)  # incomplete; handler shows usage
        if 2 <= len(args) <= 4 and args[1].lower() in PROVIDERS:
            return SlashParse("add", args)
        return SlashParse("question", question=raw)
    if cmd in NAME_COMMANDS and len(args) <= 1:
        return SlashParse(cmd, args)
    return SlashParse("question", question=raw)


def _looks_like_path(rest: str) -> bool:
    t = rest.strip().strip('"').strip("'")
    for flag in ("--folder", "--inline", "--auto"):
        t = t.replace(flag, "").strip()
    return bool(t) and (t[0] in "~/.$" or "/" in t)


def help_text() -> str:
    return HELP_TEXT


def models_text(provider: Optional[str] = None,
                lister: Callable[[str], List[str]] = suggested_models) -> str:
    targets = [provider] if provider else list(PROVIDERS)
    lines = [":gear: *Known model ids*"]
    for prov in targets:
        if prov == "openrouter":
            lines.append("• `openrouter` — any OpenRouter id, e.g. `openai/gpt-5.1` (needs `OPENROUTER_API_KEY`)")
            continue
        known = lister(prov)
        extra = " (or a full `claude-…` name)" if prov == "claude" else ""
        listing = ", ".join(f"`{m}`" for m in known) if known else "_(couldn't list; any id is passed through)_"
        lines.append(f"• `{prov}` — {listing}{extra}")
    return "\n".join(lines)


def roster_text(roster: Roster, notice: Optional[str] = None, can_edit: bool = True) -> str:
    """Fallback/plain-text roster (also used by older tests)."""
    head = f":classical_building: *War Room council* — chair: *{roster.chairman}*"
    parts = [notice, head] if notice else [head]
    parts.extend(roster_lines(roster, markdown=True))
    if can_edit:
        parts.append(
            "_Buttons edit the shared roster, or use `/warroom help`. "
            "Changes apply to the next council._"
        )
    else:
        parts.append("_View only. Roster edits are limited to War Room admins._")
    return "\n".join(p for p in parts if p)


def _member_line(roster: Roster, m: MemberConfig) -> str:
    roles = []
    if m.name == roster.chairman:
        roles.append("chair")
    if roster.title_member and m.name == roster.title_member:
        roles.append("titles")
    color = HEX_TO_NAME.get((m.color or "").upper(), m.color)
    role = f"  [{', '.join(roles)}]" if roles else ""
    mark = ":white_check_mark:" if m.enabled else ":white_circle:"
    return f"{mark} *{m.name}* — `{m.spec}` ({color}){role}"


def _button(action_id: str, text: str, value: str, style: Optional[str] = None,
            confirm: Optional[Dict] = None) -> Dict:
    el: Dict = {
        "type": "button",
        "text": {"type": "plain_text", "text": text, "emoji": True},
        "action_id": action_id,
        "value": value,
    }
    if style:
        el["style"] = style
    if confirm:
        el["confirm"] = confirm
    return el


def _confirm(title: str, body: str, ok: str = "Confirm") -> Dict:
    return {
        "title": {"type": "plain_text", "text": title},
        "text": {"type": "mrkdwn", "text": body},
        "confirm": {"type": "plain_text", "text": ok},
        "deny": {"type": "plain_text", "text": "Cancel"},
    }


def roster_blocks(roster: Roster, *, can_edit: bool,
                  notice: Optional[str] = None) -> List[Dict]:
    blocks: List[Dict] = []
    if notice:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": notice}})
    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": f":classical_building: *War Room council* — chair: *{roster.chairman}*"},
    })
    if not can_edit:
        body = "\n".join(roster_lines(roster, markdown=True)) or "_No members._"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body}})
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": "_View only. Roster edits are limited to War Room admins._"}],
        })
        return blocks

    for i, m in enumerate(roster.members):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _member_line(roster, m)}})
        buttons = [
            _button(f"warroom_toggle__{i}", "Disable" if m.enabled else "Enable", m.name),
        ]
        if m.name != roster.chairman:
            buttons.append(_button(f"warroom_chair__{i}", "Set chair", m.name, style="primary"))
            buttons.append(_button(
                f"warroom_remove__{i}", "Remove", m.name, style="danger",
                confirm=_confirm("Remove member", f"Remove *{m.name}* from the council?", "Remove"),
            ))
        buttons.append(_button(f"warroom_test__{i}", "Test", m.name))
        blocks.append({"type": "actions", "block_id": f"warroom_actions_{i}", "elements": buttons})

    blocks.append({
        "type": "actions",
        "block_id": "warroom_footer_actions",
        "elements": [_button(
            "warroom_reset", "Reset to defaults", "reset", style="danger",
            confirm=_confirm(
                "Reset roster",
                "Delete the saved roster and go back to environment defaults?",
                "Reset",
            ),
        )],
    })
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn",
                      "text": "_Changes apply to the next council in Slack and the TUI. `/warroom help` for commands._"}],
    })
    return blocks


def roster_reply(roster: Roster, *, can_edit: bool, notice: Optional[str] = None,
                 ok: bool = True) -> SlackReply:
    return SlackReply(
        text=roster_text(roster, notice=notice, can_edit=can_edit),
        blocks=roster_blocks(roster, can_edit=can_edit, notice=notice),
        ok=ok,
    )


def _text_reply(text: str, ok: bool = True) -> SlackReply:
    return SlackReply(
        text=text,
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        ok=ok,
    )


def _add_member(roster: Roster, args: List[str]) -> str:
    if len(args) < 2 or len(args) > 4:
        raise RosterError(
            "Usage: `/warroom add NAME provider [model] [color]`. "
            f"Providers: {', '.join(PROVIDERS)}."
        )
    name, provider = args[0], args[1].lower()
    model = args[2] if len(args) >= 3 else None
    color_arg = args[3] if len(args) >= 4 else None
    if model in ("-", "default"):
        model = None
    if color_arg:
        color = resolve_color(color_arg)
        if not color:
            raise RosterError(f"Unknown color '{color_arg}'. Use one of {', '.join(PALETTE)}.")
    else:
        color = next_color(roster)
    roster.add(MemberConfig(name, provider, model, color, True))
    added = roster.get(name)
    spec = added.spec if added else provider
    return f":white_check_mark: Added *{added.name if added else name}* (`{spec}`)."


def _reset_roster() -> SlackReply:
    p = config_path()
    if p.exists():
        p.unlink()
        notice = ":white_check_mark: Removed the saved roster. Back to environment defaults."
    else:
        notice = ":information_source: No saved roster; already using environment defaults."
    return roster_reply(current_roster(), can_edit=True, notice=notice)


def _mutate(edit, notice: str, *, check_cli: bool, models) -> SlackReply:
    try:
        roster = load_roster()
        produced = edit(roster)
        if produced:  # edit may return a more specific notice
            notice = produced
        kw = {"models": models} if models is not None else {}
        save_roster(roster, check_cli=check_cli, **kw)
    except RosterError as e:
        return roster_reply(current_roster(), can_edit=True,
                            notice=f":x: {e}", ok=False)
    return roster_reply(roster, can_edit=True, notice=notice)


def _need_name(cmd: str, args: List[str]) -> str:
    if len(args) != 1 or not args[0].strip():
        raise RosterError(f"Usage: `/warroom {cmd} NAME`")
    return args[0]


def dispatch_slash(parsed: SlashParse, *, user_id: str, check_cli: bool = True,
                   models=None) -> SlackReply:
    """Turn a parsed `/warroom` subcommand into an ephemeral Slack reply.

    `models` is forwarded to `save_roster` (defaults to live CLI discovery).
    Question kinds are not handled here — the bot starts a council instead.
    """
    kind, args = parsed.kind, parsed.args
    if kind == "context":  # needs the channel; the bot routes it to slack_context
        return _text_reply("`/warroom context` needs a channel.", ok=False)
    if kind in ("usage", "help"):
        text = HELP_TEXT if kind == "help" else f"{USAGE_TEXT}\n\n{HELP_TEXT}"
        return _text_reply(text)
    if kind == "models":
        try:
            return _text_reply(models_text(args[0] if args else None))
        except Exception as e:
            return _text_reply(f":x: Couldn't list models: {e}", ok=False)
    if kind == "roster":
        return roster_reply(current_roster(), can_edit=can_edit_roster(user_id))

    if not can_edit_roster(user_id):
        return _text_reply(DENIED, ok=False)

    if kind == "reset":
        if args != ["confirm"]:
            return SlackReply(
                text="Reset the saved roster back to environment defaults?",
                blocks=[
                    {"type": "section",
                     "text": {"type": "mrkdwn",
                              "text": "Reset the saved roster and go back to environment defaults? This doesn't change `.env`."}},
                    {"type": "actions", "block_id": "warroom_footer_actions", "elements": [
                        _button("warroom_reset", "Reset to defaults", "reset", style="danger",
                                confirm=_confirm(
                                    "Reset roster",
                                    "Delete the saved roster and go back to environment defaults?",
                                    "Reset",
                                )),
                    ]},
                ],
            )
        return _reset_roster()

    if kind == "test":
        try:
            name = _need_name("test", args)
            roster = current_roster()
            mem = roster.get(name)
            if mem is None:
                raise RosterError(f"No council member named '{name}'.")
            return SlackReply(
                text=ping_started_text(mem.to_member()),
                ok=True,
                ping=mem.to_member(),
            )
        except RosterError as e:
            return roster_reply(current_roster(), can_edit=True,
                                notice=f":x: {e}", ok=False)

    def edit(roster: Roster) -> Optional[str]:
        if kind == "add":
            return _add_member(roster, args)
        name = _need_name(kind, args)
        if kind == "remove":
            gone = roster.remove(name)
            return f":white_check_mark: Removed *{gone.name}*."
        if kind == "enable":
            m = roster.set_enabled(name, True)
            return f":white_check_mark: Enabled *{m.name}*."
        if kind == "disable":
            m = roster.set_enabled(name, False)
            return f":white_check_mark: Disabled *{m.name}*."
        if kind == "chair":
            m = roster.set_chair(name)
            return f":white_check_mark: Chair is now *{m.name}*."
        if kind == "title":
            m = roster.set_title_member(name)
            return f":white_check_mark: *{m.name}* will write titles."
        raise RosterError(f"Unknown command '{kind}'.")

    return _mutate(edit, "", check_cli=check_cli, models=models)


def handle_action(action_id: str, value: str, user_id: str, *,
                  check_cli: bool = True, models=None) -> SlackReply:
    """Handle a Block Kit button from the roster message."""
    if not can_edit_roster(user_id):
        return _text_reply(DENIED, ok=False)
    if action_id == "warroom_reset":
        return _reset_roster()

    kind = action_id.split("__", 1)[0].removeprefix("warroom_")
    name = value or ""
    if kind == "toggle":
        roster = current_roster()
        m = roster.get(name)
        if m is None:
            return roster_reply(roster, can_edit=True,
                                notice=f":x: No council member named '{name}'.", ok=False)
        sub = "disable" if m.enabled else "enable"
        return dispatch_slash(SlashParse(sub, [name]), user_id=user_id,
                              check_cli=check_cli, models=models)
    if kind in ("chair", "remove", "test"):
        return dispatch_slash(SlashParse(kind, [name]), user_id=user_id,
                              check_cli=check_cli, models=models)
    return _text_reply(f":x: Unknown action `{action_id}`.", ok=False)


def ping_started_text(member: Member) -> str:
    spec = f"{member.provider}:{member.model}" if member.model else member.provider
    return f":hourglass_flowing_sand: Pinging *{member.name}* (`{spec}`)…"


def reply_from_ping(member: Member, ok: bool, text: str, *, can_edit: bool) -> SlackReply:
    status = ":white_check_mark: *OK*" if ok else ":x: *FAILED*"
    body = (text or "").strip() or "(no output)"
    if len(body) > 300:
        body = body[:300].rstrip() + "…"
    notice = f"{status} — *{member.name}*\n```{body}```"
    return roster_reply(current_roster(), can_edit=can_edit, notice=notice, ok=ok)
