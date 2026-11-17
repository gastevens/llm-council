"""War Room terminal UI (Textual).

    uv run python -m backend.tui            # real council (codex / claude / grok CLIs)
    uv run python -m backend.tui --fake     # canned answers, no CLI calls
    uv run python -m backend.tui --ask "Should we self-host email?"   # ask on open
    uv run python -m backend.tui --context ~/Documents/Proposal   # discuss a folder

Type a question at the bottom and press Enter. Stages render as they finish:
Stage 1 one tab per member, Stage 2 peer rankings + collapsible critiques,
Stage 3 the chairman's verdict. Follow-up questions in the same session carry
the earlier questions and verdicts as context.

Context folder: type `/context PATH` (add `--folder` or `--inline` to force a
mode), `/context` to show it, `/context off` to drop it. The header shows the
folder, file count and mode; a new session (ctrl+n) clears it.

Keys: ctrl+e council settings (add/remove members, pick the chair)
      ctrl+n new session · ctrl+s save transcript (~/Documents/War Room/)
      q (outside the input) / ctrl+c / ctrl+q quit · esc leaves the input box

Closing the terminal window (SIGHUP) or SIGTERM kills any running CLI
subprocesses before exiting, so nothing is left behind.
"""

import argparse
import asyncio
import logging
import os
import random
import re
import signal
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

from textual import work  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.theme import Theme  # noqa: E402
from textual.containers import Horizontal, Vertical, VerticalScroll  # noqa: E402
from textual.widgets import (  # noqa: E402
    Collapsible, DataTable, Footer, Header, Input, Markdown, Static, TabbedContent, TabPane,
)

from . import council  # noqa: E402
from .config import TIMEOUT  # noqa: E402
from .context import ContextError, FolderContext, build_context, describe, stage1_prompt  # noqa: E402
from .council_config import Roster, RosterError, current_roster, load_roster  # noqa: E402
from .providers import Member, kill_active_processes, query_member, use_context  # noqa: E402
from .slack_format import build_followup_question, deanonymize  # noqa: E402
from .tui_council import CouncilScreen  # noqa: E402

TEAL, ORANGE, BLUE, PURPLE = "#20B8A4", "#F86522", "#1788F8", "#8F61F6"
BG, FG, DIM = "#1F1F1F", "#FFFFFF", "#8C8C8C"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

Query = Callable[..., Awaitable[Optional[Dict[str, Any]]]]


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "member"


def transcript_dir() -> Path:
    return Path(os.getenv("WARROOM_TRANSCRIPT_DIR") or Path.home() / "Documents" / "War Room")


# ---------------------------------------------------------------------------
# Member failure reasons (providers log a warning when a CLI call fails)
# ---------------------------------------------------------------------------

class ErrorTap(logging.Handler):
    """Remembers the latest failure message per member name."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.errors: Dict[str, str] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.args and len(record.args) >= 3:
                self.errors[str(record.args[0])] = str(record.args[2])
        except Exception:
            pass


ERRORS = ErrorTap()
_plog = logging.getLogger("council.providers")
_plog.addHandler(ERRORS)
_plog.propagate = False  # stderr output would scribble over the TUI


# ---------------------------------------------------------------------------
# Council engine (UI-independent; reports progress through `events`)
# ---------------------------------------------------------------------------

class Events:
    """Progress callbacks. The TUI forwards these onto its event loop."""

    def member_started(self, stage: int, name: str) -> None: ...
    def member_done(self, stage: int, name: str, text: Optional[str], secs: float, error: Optional[str]) -> None: ...
    def stage2_done(self, result: Dict[str, Any]) -> None: ...
    def stage3_done(self, result: Dict[str, Any]) -> None: ...
    def failed(self, message: str) -> None: ...


async def run_council(prompt: str, events: Events, query: Query = query_member,
                      roster: Optional[Roster] = None,
                      context: Optional[FolderContext] = None) -> Dict[str, Any]:
    """Run the 3 stages on `prompt` (already folded with follow-up context),
    optionally about a context folder (see backend.context)."""
    with use_context(context):
        return await _run_council(prompt, events, query, roster or current_roster(), context)


async def _run_council(prompt: str, events: Events, query: Query, roster: Roster,
                       context: Optional[FolderContext]) -> Dict[str, Any]:
    members = roster.active_members()
    chairman = roster.chair_member()
    result: Dict[str, Any] = {"answers": {}, "errors": {}, "stage1": [], "stage2": [],
                              "label_to_model": {}, "aggregate": [], "failed_reviewers": [],
                              "stage3": None}
    phase = {"stage": 1}

    async def tracked(member: Member, messages, timeout: float):
        stage = phase["stage"]
        events.member_started(stage, member.name)
        t0 = time.monotonic()
        ERRORS.errors.pop(member.name, None)
        try:
            resp = await query(member, messages, timeout=timeout)
        except Exception as e:  # queries normally swallow errors, but be safe
            ERRORS.errors[member.name] = f"{type(e).__name__}: {e}"
            resp = None
        text = (resp or {}).get("content") if resp else None
        err = None if text else (ERRORS.errors.get(member.name) or "no answer")
        events.member_done(stage, member.name, text, time.monotonic() - t0, err)
        return resp

    # Stage 1, member by member as each finishes
    messages = [{"role": "user", "content": stage1_prompt(prompt, context)}]

    async def ask(m: Member):
        resp = await tracked(m, messages, TIMEOUT)
        return m, resp

    for fut in asyncio.as_completed([ask(m) for m in members]):
        m, resp = await fut
        text = (resp or {}).get("content") if resp else None
        result["answers"][m.name] = text
        if not text:
            result["errors"][m.name] = ERRORS.errors.get(m.name) or "no answer"
    result["stage1"] = [{"model": m.name, "response": result["answers"][m.name]}
                        for m in members if result["answers"].get(m.name)]
    if not result["stage1"]:
        events.failed("Every council member failed to answer. Check that their CLIs are installed "
                      "and signed in (ctrl+e → Test), then try again.")
        return result

    # Stages 2 and 3 reuse backend.council; route their member calls through `tracked`.
    original = council.query_member
    council.query_member = tracked
    try:
        phase["stage"] = 2
        stage2, label_to_model = await council.stage2_collect_rankings(prompt, result["stage1"], members, context)
        reviewed = {r["model"] for r in stage2}
        result.update(stage2=stage2, label_to_model=label_to_model,
                      aggregate=council.calculate_aggregate_rankings(stage2, label_to_model),
                      failed_reviewers=[m.name for m in members if m.name not in reviewed])
        events.stage2_done(result)
        phase["stage"] = 3
        result["stage3"] = await council.stage3_synthesize_final(prompt, result["stage1"], stage2, chairman,
                                                                context)
    finally:
        council.query_member = original
    events.stage3_done(result)
    return result


# ---------------------------------------------------------------------------
# Fake mode
# ---------------------------------------------------------------------------

def make_fake_query(delay: float = 0.8, fail: Sequence[str] = ()) -> Query:
    """Canned answers (backend.slack_bot's fake), with optional delay and failures."""
    from .slack_bot import _fake_query

    async def fake(member: Member, messages, timeout: float):
        if delay:
            await asyncio.sleep(delay * random.uniform(0.5, 1.5))
        prompt = messages[0]["content"]
        stage1 = "Chairman of an LLM Council" not in prompt and "FINAL RANKING" not in prompt
        if stage1 and member.name in fail:
            logging.getLogger("council.providers").warning(
                "%s (%s) failed: %s", member.name, "fake", "simulated failure (--fake-fail)")
            return None
        if stage1 and member.name == "Grok":
            return {"content": "# Grok says\n\nShort version: **don't self-host** unless you enjoy "
                               "deliverability puzzles.\n\n```bash\ndig +short MX example.com\n```"}
        return await _fake_query(member, messages, timeout)

    return fake


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------

def transcript_markdown(turns: List[Dict[str, Any]], started: datetime) -> str:
    out = [f"# War Room session — {started:%Y-%m-%d %H:%M}", ""]
    for i, t in enumerate(turns, 1):
        r = t.get("result") or {}
        out += [f"## Question {i}", "", t["question"], ""]
        names = t.get("members") or list((r.get("answers") or {}).keys())
        out += [f"_Council: {', '.join(names)} — chair: {t.get('chair', '?')}_", ""]
        ctx = t.get("context")
        if ctx:
            mode = "inline" if ctx["mode"] == "inline" else "folder access"
            out += [f"_Context folder: `{ctx['root']}` ({mode}, {len(ctx['files'])} files)_", ""]
            out += [f"<details><summary>Context files</summary>", ""]
            out += [f"- `{f}`" for f in ctx["files"][:500]]
            if len(ctx["files"]) > 500:
                out.append(f"- … and {len(ctx['files']) - 500} more")
            out += ["", "</details>", ""]
        out += ["### Stage 1 · Individual answers", ""]
        for name in names:
            text = (r.get("answers") or {}).get(name)
            out += [f"#### {name}", "", text or f"_Failed: {(r.get('errors') or {}).get(name, 'no answer')}_", ""]
        if r.get("stage2"):
            out += ["### Stage 2 · Peer review", "", "| # | Member | Avg rank | Votes |", "|---|---|---|---|"]
            for n, a in enumerate(r.get("aggregate") or [], 1):
                out.append(f"| {n} | {a['model']} | {a['average_rank']} | {a.get('rankings_count', 0)} |")
            out.append("")
            for rv in r["stage2"]:
                body = deanonymize(rv.get("ranking", ""), r.get("label_to_model") or {})
                out += [f"#### Critique by {rv['model']}", "", body, ""]
        if r.get("stage3"):
            out += [f"### Stage 3 · Chairman's verdict ({r['stage3'].get('model')})", "",
                    r["stage3"].get("response", ""), ""]
        elif t.get("error"):
            out += ["### Error", "", t["error"], ""]
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class MemberBadge(Static):
    """One member's live status: spinner while working, ✓ / ✗ when done."""

    def __init__(self, name: str, color: str, label: Optional[str] = None, **kw) -> None:
        super().__init__("", **kw)
        self.member = name
        self.badge_color = color
        self.label_text = label or name
        self.badge_state = "idle"
        self.status = "waiting"
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.1, self._paint, pause=True)
        self._paint()

    def set_state(self, state: str, status: str) -> None:
        self.badge_state, self.status = state, status
        if self._timer is not None:
            (self._timer.resume if state == "busy" else self._timer.pause)()
        self._paint()

    def _paint(self) -> None:
        icon = {"busy": SPINNER[int(time.monotonic() * 10) % len(SPINNER)], "done": "✓",
                "failed": "✗"}.get(self.badge_state, "·")
        icon_color = {"failed": "#FF5F5F", "idle": DIM}.get(self.badge_state, self.badge_color)
        self.update(f"[{icon_color}]{icon}[/] [bold {self.badge_color}]{self.label_text}[/] [{DIM}]{self.status}[/]")


class TurnView(Vertical):
    """One question and its three stages."""

    def __init__(self, n: int, question: str, followup: bool, roster: Roster) -> None:
        super().__init__(id=f"turn-{n}", classes="turn")
        self.roster = roster
        self.members = list(roster.enabled)
        self.chair_name = roster.chairman
        self.n = n
        self.question = question
        self.followup = followup
        self.badges: Dict[str, MemberBadge] = {}
        self.no_answer: set = set()

    def pane_id(self, name: str) -> str:
        return f"t{self.n}-{slug(name)}"

    def compose(self) -> ComposeResult:
        tag = "follow-up" if self.followup else "question"
        yield Static(f"[{PURPLE}]❯[/] [bold]{_escape(self.question)}[/]  [{DIM}]{tag}[/]", classes="question")
        with Horizontal(classes="badges"):
            for m in self.members:
                b = MemberBadge(m.name, m.color, id=f"b{self.n}-{slug(m.name)}")
                self.badges[m.name] = b
                yield b
            self.chair = MemberBadge(self.chair_name, PURPLE, label=f"Chair · {self.chair_name}",
                                     id=f"b{self.n}-chair", classes="chair")
            yield self.chair
        yield Static("STAGE 1 · INDIVIDUAL ANSWERS", classes="stage-title")
        with TabbedContent(id=f"s1-{self.n}", classes="answers"):
            for m in self.members:
                with TabPane(self._tab_label(m.name, "busy"), id=self.pane_id(m.name)):
                    yield Markdown(f"*{m.name} is thinking…*", id=f"md-{self.pane_id(m.name)}")
        yield Static("STAGE 2 · PEER REVIEW", classes="stage-title")
        self.stage2_box = Vertical(Static(f"[{DIM}]Waiting for stage 1…[/]", classes="placeholder"),
                                   id=f"s2-{self.n}", classes="stage2")
        yield self.stage2_box
        yield Static("STAGE 3 · CHAIRMAN'S VERDICT", classes="stage-title")
        self.verdict = Markdown(f"*Waiting for the council…*", id=f"verdict-{self.n}")
        box = Vertical(self.verdict, classes="verdict")
        box.border_title = f"Verdict · {self.chair_name}"
        yield box

    def _tab_label(self, name: str, state: str) -> str:
        mark = {"busy": "…", "done": " ✓", "failed": " ✗"}[state]
        return f"[{self.roster.color(name)}]●[/] {name}{mark}"

    # -- progress ----------------------------------------------------------

    def member_started(self, stage: int, name: str) -> None:
        if stage == 3:
            self.chair.set_state("busy", "deliberating…")
        elif name in self.badges:
            self.badges[name].set_state("busy", "answering…" if stage == 1 else "reviewing…")

    async def member_done(self, stage: int, name: str, text: Optional[str], secs: float,
                          error: Optional[str]) -> None:
        if stage == 3:
            self.chair.set_state("done" if text else "failed", f"verdict in {secs:.0f}s" if text else "failed")
            return
        badge = self.badges.get(name)
        if stage == 1:
            if not text:
                self.no_answer.add(name)
            if badge:
                badge.set_state("done" if text else "failed",
                          f"answered in {secs:.0f}s" if text else "failed")
            md = self.query_one(f"#md-{self.pane_id(name)}", Markdown)
            if text:
                await md.update(text)
            else:
                await md.update(f"**{name} failed to answer.**\n\n`{error or 'no answer'}`")
            tabs = self.query_one(TabbedContent)
            tabs.get_tab(self.pane_id(name)).label = self._tab_label(name, "done" if text else "failed")
        elif badge:
            no_answer = name in self.no_answer
            state = "failed" if (no_answer or not text) else "done"
            status = "reviewed" if text else "review failed"
            badge.set_state(state, f"no answer · {status}" if no_answer else status)

    async def stage2_done(self, r: Dict[str, Any]) -> None:
        await self.stage2_box.remove_children()
        table = DataTable(id=f"rank-{self.n}", cursor_type="none", zebra_stripes=True)
        table.add_columns("#", "Member", "Avg rank", "Votes", "Ranked by")
        ranked_by: Dict[str, List[str]] = {}
        for rv in r["stage2"]:
            for pos, lbl in enumerate(rv.get("parsed_ranking", []), 1):
                model = r["label_to_model"].get(lbl)
                if model:
                    ranked_by.setdefault(model, []).append(f"{rv['model']}:{pos}")
        medals = ["🥇", "🥈", "🥉"]
        for i, a in enumerate(r["aggregate"]):
            table.add_row(medals[i] if i < 3 else str(i + 1),
                          f"[bold {self.roster.color(a['model'])}]{a['model']}[/]",
                          f"{a['average_rank']:.2f}", str(a.get("rankings_count", 0)),
                          ", ".join(ranked_by.get(a["model"], [])))
        widgets: List[Any] = [Static(f"[{DIM}]Answers were anonymized; lower average position is better.[/]")]
        widgets.append(table if r["aggregate"] else Static("[italic]No parsable rankings came back.[/]"))
        for name in r.get("failed_reviewers") or []:
            widgets.append(Static(f"[#FF5F5F]✗[/] {name} didn't submit a review."))
        for rv in r["stage2"]:
            body = deanonymize(rv.get("ranking", ""), r["label_to_model"])
            widgets.append(Collapsible(Markdown(body), title=f"Critique by {rv['model']}", collapsed=True,
                                       classes="critique"))
        await self.stage2_box.mount_all(widgets)

    async def stage3_done(self, r: Dict[str, Any]) -> None:
        s3 = r.get("stage3") or {}
        await self.verdict.update(s3.get("response") or "_The chairman did not return a verdict._")
        self.verdict.scroll_visible()

    async def failed(self, message: str) -> None:
        await self.stage2_box.remove_children()
        await self.stage2_box.mount(Static(f"[#FF5F5F]✗ {_escape(message)}[/]"))
        await self.verdict.update(f"**No verdict.** {message}")
        self.chair.set_state("failed", "no verdict")


def _escape(text: str) -> str:
    return text.replace("[", r"\[")


class _ThreadEvents(Events):
    """Hands engine callbacks from the worker thread to the UI loop."""

    def __init__(self, app: "WarRoomApp", turn: TurnView) -> None:
        self.app, self.turn = app, turn

    def _call(self, fn, *args) -> None:
        try:
            self.app.call_from_thread(fn, *args)
        except Exception:
            pass  # app is shutting down

    def member_started(self, stage, name):
        self._call(self.turn.member_started, stage, name)

    def member_done(self, stage, name, text, secs, error):
        self._call(self.turn.member_done, stage, name, text, secs, error)

    def stage2_done(self, result):
        self._call(self.turn.stage2_done, result)

    def stage3_done(self, result):
        self._call(self.turn.stage3_done, result)

    def failed(self, message):
        self._call(self.turn.failed, message)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def welcome_text(roster: Roster) -> str:
    return (
        f"[bold {TEAL}]●[/]\n[bold {ORANGE}]●[/]   [bold]>_[/]   [bold {PURPLE}]●[/]\n[bold {BLUE}]●[/]\n\n"
        "[bold]Welcome to the War Room.[/]\n"
        f"[{DIM}]Ask a question below. {', '.join(m.name for m in roster.enabled)} answer independently, "
        f"review each other anonymously, and {roster.chairman} delivers the verdict.\n"
        "Follow-ups keep the earlier verdicts as context. ctrl+n starts fresh, ctrl+s saves a transcript, "
        "ctrl+e edits the council.\n"
        "Type /context ~/some/folder to have the council discuss the files in a folder (/context off to drop it).[/]"
    )


def roster_bar_text(roster: Roster, context: Optional[FolderContext] = None) -> str:
    dots = "  ".join(f"[{m.color}]●[/] {_escape(m.name)}" for m in roster.enabled)
    bar = f"[{DIM}]COUNCIL[/]  {dots}    [{PURPLE}]◆[/] [{DIM}]chair[/] [bold]{_escape(roster.chairman)}[/]"
    if context is not None:
        bar += f"    [{ORANGE}]▣[/] [{DIM}]context[/] {_escape(context.summary())}"
    return bar


CONTEXT_OFF = frozenset({"off", "clear", "none", "remove", "drop"})


def parse_context_command(text: str) -> Optional[Dict[str, Any]]:
    """Parse `/context …` typed in the input. None if the text isn't a /context command.

    /context                 -> {"action": "show"}
    /context off             -> {"action": "off"}
    /context PATH [--folder|--inline|--auto]  -> {"action": "set", "path": PATH, "mode": …}
    """
    raw = text.strip()
    if not (raw == "/context" or raw.startswith("/context ")):
        return None
    rest = raw[len("/context"):].strip()
    if not rest:
        return {"action": "show"}
    if rest.lower() in CONTEXT_OFF:
        return {"action": "off"}
    mode = None
    flags = {"--folder": "folder", "--inline": "inline", "--auto": "auto"}
    for w in rest.split():
        if w.lower() in flags:
            mode = flags[w.lower()]
    path = rest
    if mode is not None:  # rebuild the path without the flag(s), keeping inner spaces
        for flag in flags:
            path = re.sub(rf"(^|\s){re.escape(flag)}(?=\s|$)", " ", path, flags=re.I)
        path = path.strip()
    path = path.strip().strip('"').strip("'").replace("\\ ", " ")
    if not path:
        return {"action": "error", "message": "Usage: /context PATH [--folder|--inline]"}
    return {"action": "set", "path": path, "mode": mode}


WAR_ROOM_THEME = Theme(
    name="war-room", primary=PURPLE, secondary=TEAL, accent=BLUE, warning=ORANGE, error="#FF5F5F",
    success=TEAL, foreground=FG, background=BG, surface="#262626", panel="#2A2A2A", dark=True,
)


class WarRoomApp(App):
    TITLE = "War Room"
    CSS = f"""
    Screen {{ background: {BG}; color: {FG}; }}
    * {{ scrollbar-background: #242424; scrollbar-background-hover: #242424; scrollbar-background-active: #242424;
         scrollbar-color: #4A4A4A; scrollbar-color-hover: {PURPLE}; scrollbar-color-active: {PURPLE}; }}
    Header {{ background: {BG}; color: {FG}; }}
    HeaderTitle {{ text-style: bold; }}
    Footer {{ background: #161616; }}
    #log {{ padding: 0 2; scrollbar-size-vertical: 1; }}
    #welcome {{ padding: 1 2; height: auto; }}
    .turn {{ height: auto; margin: 1 0 1 0; }}
    .question {{ background: #2A2A2A; padding: 0 1; border-left: thick {PURPLE}; }}
    .badges {{ height: 1; margin: 1 0 0 0; }}
    MemberBadge {{ width: auto; margin-right: 4; }}
    .stage-title {{ color: {DIM}; text-style: bold; margin: 1 0 0 0; }}
    .answers {{ height: auto; }}
    .answers ContentSwitcher {{ height: auto; }}
    .answers TabPane {{ height: auto; max-height: 24; overflow-y: auto; padding: 0 1; }}
    Tabs {{ background: {BG}; }}
    Underline > .underline--bar {{ color: {PURPLE}; background: #333333; }}
    Markdown {{ background: {BG}; padding: 0; margin: 0; }}
    .stage2 {{ height: auto; }}
    .stage2 DataTable {{ height: auto; max-height: 12; margin: 1 0; background: {BG}; }}
    .critique {{ background: {BG}; padding: 0; border-top: none; }}
    .critique Markdown {{ background: {BG}; }}
    .verdict {{ height: auto; border: round {PURPLE}; border-title-color: {PURPLE};
                border-title-style: bold; padding: 0 1; background: #251E35; }}
    .verdict Markdown {{ background: #251E35; }}
    #roster-bar {{ height: 1; padding: 0 2; background: #191919; }}
    #ask {{ dock: bottom; margin: 0 1 0 1; border: tall {PURPLE}; background: #262626; }}
    #ask:focus {{ border: tall {PURPLE}; }}
    """
    BINDINGS = [
        Binding("ctrl+e", "council", "Council", priority=True),
        Binding("ctrl+n", "new_session", "New session", priority=True),
        Binding("ctrl+s", "save_transcript", "Save transcript", priority=True),
        Binding("q", "quit_app_q", "Quit"),
        Binding("ctrl+c", "quit_app", "Quit", show=False, priority=True),
        Binding("ctrl+q", "quit_app", "Quit", show=False, priority=True),
        Binding("escape", "leave_input", "Leave input", show=False),
    ]

    def __init__(self, fake: bool = False, fake_delay: float = 0.8, fake_fail: Sequence[str] = (),
                 save: bool = True, ask: Optional[str] = None, context: Optional[str] = None,
                 context_mode: Optional[str] = None, launch_cwd: Optional[str] = None) -> None:
        super().__init__()
        self.initial_question = ask
        self.initial_context = context
        self.initial_context_mode = context_mode
        self.launch_cwd = launch_cwd or os.getcwd()
        self.context: Optional[FolderContext] = None
        self.fake = fake
        self.query_fn: Query = make_fake_query(fake_delay, fake_fail) if fake else query_member
        self.persist = save and not fake
        self.running = False
        self.roster_warning: Optional[str] = None
        try:
            self.roster: Roster = load_roster()
        except RosterError as e:
            self.roster = current_roster()
            self.roster_warning = f"Council config ignored: {e}"
        self._new_state()

    @property
    def chat_screen(self):
        """The chat screen (queries must not hit the council settings screen)."""
        return self.screen_stack[0]

    def _new_state(self) -> None:
        self.history: List[Dict[str, str]] = []
        self.turns: List[Dict[str, Any]] = []
        self.started = datetime.now()
        self.conversation_id: Optional[str] = None
        self.turn_count = getattr(self, "turn_count", 0)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(roster_bar_text(self.roster, self.context), id="roster-bar")
        with VerticalScroll(id="log"):
            yield Static(welcome_text(self.roster), id="welcome")
        yield Input(placeholder="Ask the council…  (Enter to send)", id="ask")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(WAR_ROOM_THEME)
        self.theme = "war-room"
        self._refresh_roster_ui()
        if self.roster_warning:
            self.notify(self.roster_warning, severity="warning", timeout=10)
        self.chat_screen.query_one("#ask", Input).focus()
        if self.initial_question or self.initial_context:
            self.call_after_refresh(self._startup)

    async def _startup(self) -> None:
        if self.initial_context:
            await self.set_context(self.initial_context, self.initial_context_mode)
        if self.initial_question:
            await self._ask_initial()

    async def _ask_initial(self) -> None:
        inp = self.chat_screen.query_one("#ask", Input)
        inp.value = self.initial_question or ""
        await inp.action_submit()

    # -- asking -------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        question = event.value.strip()
        if not question or event.input.id != "ask":
            return
        cmd = parse_context_command(question)
        if cmd is not None:
            event.input.value = ""
            await self.context_command(cmd)
            return
        if self.running:
            self.notify("The council is still in session; wait for the verdict.", severity="warning")
            return
        event.input.value = ""
        prompt = build_followup_question(self.history, question)
        self.turn_count += 1
        log = self.chat_screen.query_one("#log", VerticalScroll)
        for w in log.query("#welcome"):
            await w.remove()
        roster = self.roster.copy()  # roster edits apply from the next question
        turn = TurnView(self.turn_count, question, followup=bool(self.history), roster=roster)
        await log.mount(turn)
        log.scroll_end(animate=False)
        self.running = True
        event.input.placeholder = "Council in session…"
        record = {"question": question, "prompt": prompt, "result": None, "error": None,
                  "roster": roster, "members": [m.name for m in roster.enabled], "chair": roster.chairman,
                  "context_obj": self.context,
                  "context": None if self.context is None else {
                      "root": str(self.context.root), "mode": self.context.mode,
                      "files": [f.path for f in self.context.files]}}
        self.turns.append(record)
        self.run_turn(turn, record)

    @work(thread=True, exclusive=True, group="council")
    def run_turn(self, turn: TurnView, record: Dict[str, Any]) -> None:
        events = _ThreadEvents(self, turn)
        try:
            record["result"] = asyncio.run(run_council(record["prompt"], events, self.query_fn,
                                                      record["roster"], record.get("context_obj")))
        except Exception as e:  # never leave the UI stuck "in session"
            record["error"] = f"The council hit an error: {type(e).__name__}: {str(e)[:300]}"
            events.failed(record["error"])
        try:
            self.call_from_thread(self._turn_finished, record)
        except Exception:
            pass

    def _turn_finished(self, record: Dict[str, Any]) -> None:
        self.running = False
        inp = self.chat_screen.query_one("#ask", Input)
        inp.placeholder = "Ask a follow-up…  (Enter to send · ctrl+n for a new session)"
        r = record.get("result") or {}
        verdict = (r.get("stage3") or {}).get("response")
        if verdict:
            self.history += [{"role": "user", "text": record["question"]},
                             {"role": "council", "text": verdict}]
            if self.persist:
                self._persist(record)
        self.bell()

    def _persist(self, record: Dict[str, Any]) -> None:
        try:
            from . import storage
            if self.conversation_id is None:
                self.conversation_id = str(uuid.uuid4())
                storage.create_conversation(self.conversation_id)
                title = record["question"] if len(record["question"]) <= 50 else record["question"][:47] + "..."
                storage.update_conversation_title(self.conversation_id, title)
            r = record["result"]
            storage.add_user_message(self.conversation_id, record["question"])
            storage.add_assistant_message(self.conversation_id, r["stage1"], r["stage2"], r["stage3"])
        except Exception as e:
            self.notify(f"Couldn't save the conversation: {e}", severity="warning")

    # -- actions ------------------------------------------------------------

    async def action_new_session(self) -> None:
        if self.running:
            self.notify("The council is still in session; wait for the verdict first.", severity="warning")
            return
        log = self.chat_screen.query_one("#log", VerticalScroll)
        await log.remove_children()
        await log.mount(Static(welcome_text(self.roster), id="welcome"))
        self._new_state()
        had_context = self.context is not None
        self.context = None
        self._refresh_roster_ui()
        inp = self.chat_screen.query_one("#ask", Input)
        inp.placeholder = "Ask the council…  (Enter to send)"
        inp.focus()
        self.notify("New session started." + (" Context folder cleared." if had_context else ""))

    # -- context folder ---------------------------------------------------------

    async def context_command(self, cmd: Dict[str, Any]) -> None:
        action = cmd["action"]
        if action == "show":
            if self.context is None:
                self.notify("No context folder. Type /context PATH to add one.")
            else:
                self.notify(describe(self.context), timeout=10)
            return
        if action == "error":
            self.notify(cmd["message"], severity="warning")
            return
        if self.running:
            self.notify("The council is still in session; change the context after the verdict.",
                        severity="warning")
            return
        if action == "off":
            had = self.context is not None
            self.context = None
            self._refresh_roster_ui()
            self.notify("Context folder removed." if had else "No context folder was set.")
            return
        await self.set_context(cmd["path"], cmd.get("mode"))

    async def set_context(self, path: str, mode: Optional[str] = None) -> bool:
        self.notify(f"Reading {path}…", timeout=3)
        try:
            ctx = await asyncio.to_thread(build_context, path, None, mode, self.launch_cwd)
        except ContextError as e:
            self.notify(f"Context not set: {e}", severity="error", timeout=10)
            return False
        except Exception as e:  # never crash the UI on a weird folder
            self.notify(f"Context not set: {type(e).__name__}: {e}", severity="error", timeout=10)
            return False
        self.context = ctx
        self._refresh_roster_ui()
        later = " Applies from the next question." if self.running else ""
        self.notify(describe(ctx) + later, timeout=10)
        return True

    def action_save_transcript(self) -> Optional[Path]:
        done = [t for t in self.turns if t.get("result") or t.get("error")]
        if not done:
            self.notify("Nothing to save yet.", severity="warning")
            return None
        folder = transcript_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^\w\- ]+", "", done[0]["question"])[:50].strip() or "session"
            path = folder / f"{self.started:%Y-%m-%d %H%M} - {name}.md"
            path.write_text(transcript_markdown(done, self.started), encoding="utf-8")
        except OSError as e:
            self.notify(f"Couldn't save the transcript: {e}", severity="error")
            return None
        self.last_transcript = path
        self.notify(f"Saved transcript to {path}")
        return path

    # -- council settings -----------------------------------------------------

    council_models = None  # tests inject a model lister here

    def check_action(self, action: str, parameters) -> Optional[bool]:
        # On the settings screen, its own keys win (ctrl+s saves the roster there).
        if isinstance(self.screen, CouncilScreen) and action in (
                "new_session", "save_transcript", "council", "quit_app_q"):
            return False
        return True

    def action_council(self) -> None:
        self.push_screen(CouncilScreen(self.roster, query=self.query_fn, check_cli=not self.fake,
                                       models=self.council_models), self._council_closed)

    def _council_closed(self, roster: Optional[Roster]) -> None:
        if roster is None:
            return
        self.roster = roster.copy()
        self._refresh_roster_ui()
        later = " The council already in session keeps its old roster." if self.running else ""
        self.notify(f"Council saved: {roster.summary()}. Applies to the next question.{later}")

    def _refresh_roster_ui(self) -> None:
        ctx = getattr(self, "context", None)
        self.sub_title = (self.roster.summary() + (f"  ·  ▣ {ctx.summary()}" if ctx else "")
                          + ("  [FAKE MODE]" if self.fake else ""))
        self.chat_screen.query_one("#roster-bar", Static).update(roster_bar_text(self.roster, ctx))
        for w in self.chat_screen.query("#welcome"):
            w.update(welcome_text(self.roster))

    def action_leave_input(self) -> None:
        self.set_focus(None)

    def action_quit_app_q(self) -> None:
        self.action_quit_app()

    def action_quit_app(self) -> None:
        kill_active_processes(signal.SIGTERM)
        self.exit()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _shutdown_children() -> None:
    if kill_active_processes(signal.SIGTERM):
        time.sleep(0.5)
        kill_active_processes(signal.SIGKILL)


def _on_hangup(signum, frame) -> None:
    # Window closed / killed: take the CLI subprocesses with us, then leave at once.
    _shutdown_children()
    os._exit(128 + signum)


def main() -> None:
    parser = argparse.ArgumentParser(description="War Room terminal UI.")
    parser.add_argument("--fake", action="store_true", help="canned answers, no CLI calls")
    parser.add_argument("--fake-delay", type=float, default=0.8, help="seconds per fake answer (default 0.8)")
    parser.add_argument("--fake-fail", action="append", default=[], metavar="MEMBER",
                        help="with --fake: make this member fail stage 1 (repeatable)")
    parser.add_argument("--ask", metavar="QUESTION", help="ask this question as soon as the UI opens")
    parser.add_argument("--no-save", action="store_true", help="don't store the session in data/conversations")
    parser.add_argument("--context", metavar="PATH", help="folder (or file) for the council to discuss")
    parser.add_argument("--context-mode", choices=("auto", "inline", "folder"), default=None,
                        help="inline (paste files), folder (read-only file tools) or auto (default)")
    args = parser.parse_args()

    launch_cwd = os.environ.get("WARROOM_LAUNCH_CWD") or os.getcwd()
    os.chdir(REPO_ROOT)  # storage uses a repo-relative data directory
    for sig in (signal.SIGHUP, signal.SIGTERM):
        signal.signal(sig, _on_hangup)
    app = WarRoomApp(fake=args.fake, fake_delay=args.fake_delay, fake_fail=args.fake_fail,
                     save=not args.no_save, ask=args.ask, context=args.context,
                     context_mode=args.context_mode, launch_cwd=launch_cwd)
    code = 0
    try:
        app.run()
        code = app.return_code or 0
    finally:
        _shutdown_children()
    sys.stdout.flush()
    os._exit(code)  # don't wait for a council thread that's still unwinding


if __name__ == "__main__":
    main()
