"""Council settings screen for the War Room TUI (ctrl+e).

Edits a copy of the roster; Save validates and writes council.json, Cancel
throws the edits away. Changes apply to the next question.
"""

import time
from typing import Callable, List, Optional

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button, DataTable, Footer, Input, RadioButton, RadioSet, Select, Static,
)

from .council_config import (
    CLAUDE_ALIASES, HEX_TO_NAME, PALETTE, PROVIDER_LABELS, PROVIDERS, MemberConfig, ModelLister,
    Roster, RosterError, available_models, next_color, normalize_member, ping_member, save_roster,
    suggested_models, validate, validate_member,
)

PURPLE, BG, DIM, RED, TEAL, AMBER = "#8F61F6", "#1F1F1F", "#8C8C8C", "#FB2A41", "#20B8A4", "#F5922C"


def _esc(text: str) -> str:
    return text.replace("[", r"\[")


class CouncilScreen(Screen):
    """Add / remove / enable members, reorder them, and pick the chair."""

    DEFAULT_CSS = f"""
    CouncilScreen {{ background: {BG}; padding: 0 2; }}
    CouncilScreen #cs-title {{ height: auto; margin: 1 0 0 0; }}
    CouncilScreen #cs-body {{ height: 1fr; margin: 1 0 0 0; }}
    CouncilScreen #cs-left {{ width: 3fr; height: 1fr; margin-right: 2; }}
    CouncilScreen #cs-right {{ width: 2fr; height: auto; border: round #3A3A3A; padding: 0 1;
                              border-title-color: {DIM}; }}
    CouncilScreen .cs-label {{ color: {DIM}; text-style: bold; margin: 1 0 0 0; }}
    CouncilScreen #cs-table {{ height: auto; max-height: 14; background: {BG}; }}
    CouncilScreen .cs-buttons {{ height: auto; margin: 1 0 0 0; }}
    CouncilScreen .cs-buttons Button {{ margin-right: 1; min-width: 8; }}
    CouncilScreen #chair-box {{ height: auto; }}
    CouncilScreen RadioSet {{ background: {BG}; border: round #3A3A3A; width: 100%; height: auto; }}
    CouncilScreen RadioSet:focus {{ border: round {PURPLE}; }}
    CouncilScreen #cs-right Input, CouncilScreen #cs-right Select {{ margin: 0 0 1 0; }}
    CouncilScreen #model-hint {{ color: {DIM}; margin: 0 0 1 1; height: auto; }}
    CouncilScreen #cs-msg {{ height: auto; min-height: 1; margin: 1 0 0 0; }}
    CouncilScreen #cs-actions {{ height: auto; margin: 0 0 1 0; }}
    CouncilScreen #cs-actions Button {{ margin-right: 2; }}
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("space", "toggle", "On/off"),
        Binding("c", "make_chair", "Make chair"),
        Binding("t", "test", "Test CLI"),
        Binding("left_square_bracket", "move(-1)", "Move up"),
        Binding("right_square_bracket", "move(1)", "Move down"),
        Binding("delete", "remove", "Remove"),
        Binding("a", "focus_add", "Add"),
    ]

    def __init__(self, roster: Roster, query=None, check_cli: bool = True,
                 models: Optional[ModelLister] = None) -> None:
        super().__init__()
        self.work = roster.copy()
        self.query_fn = query
        self.check_cli = check_cli
        self.models: ModelLister = models or available_models
        self._suggest: Callable[[str], List[str]] = (
            (lambda p: self.models(p) or []) if models else suggested_models)
        self.saved_to = None

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(f"[bold]Council settings[/]  [{DIM}]changes apply to the next question · "
                     f"ctrl+s save · esc cancel[/]", id="cs-title")
        with Horizontal(id="cs-body"):
            with Vertical(id="cs-left"):
                yield Static("MEMBERS", classes="cs-label")
                table = DataTable(id="cs-table", cursor_type="row", zebra_stripes=True)
                table.add_columns("On", "Name", "Provider", "Model", "Color", "Role")
                yield table
                with Horizontal(classes="cs-buttons"):
                    yield Button("On/off", id="btn-toggle")
                    yield Button("▲", id="btn-up")
                    yield Button("▼", id="btn-down")
                    yield Button("Remove", id="btn-remove", variant="error")
                    yield Button("Test CLI", id="btn-test")
                yield Static("CHAIR  (any member, on or off)", classes="cs-label")
                yield Vertical(id="chair-box")
            right = Vertical(id="cs-right")
            right.border_title = "Add a member"
            with right:
                yield Input(placeholder="Name, e.g. Opus", id="add-name")
                yield Select([(PROVIDER_LABELS[p], p) for p in PROVIDERS], value="claude",
                             allow_blank=False, id="add-provider")
                yield Input(placeholder="Model (blank = CLI default)", id="add-model")
                yield Static("", id="model-hint")
                yield Select([(f"[{hexv}]●[/] {name}", hexv) for name, hexv in PALETTE.items()],
                             value=next_color(self.work), allow_blank=False, id="add-color")
                yield Button("Add member", id="btn-add", variant="primary")
        yield Static("", id="cs-msg")
        with Horizontal(id="cs-actions"):
            yield Button("Save", id="btn-save", variant="success")
            yield Button("Cancel", id="btn-cancel")
        yield Footer()

    async def on_mount(self) -> None:
        self._fill_table()
        await self._rebuild_chair()
        self._update_hint("claude")
        self.query_one("#cs-table", DataTable).focus()

    # -- rendering ------------------------------------------------------------

    def _fill_table(self, select: Optional[str] = None) -> None:
        table = self.query_one("#cs-table", DataTable)
        current = select or self.selected_name()
        table.clear()
        for m in self.work.members:
            roles = []
            if m.name == self.work.chairman:
                roles.append(f"[{PURPLE}]◆ chair[/]")
            if self.work.title_member and m.name == self.work.title_member:
                roles.append(f"[{DIM}]titles[/]")
            table.add_row(f"[{TEAL}]✓[/]" if m.enabled else f"[{DIM}]·[/]",
                          f"[bold {m.color}]{_esc(m.name)}[/]" if m.enabled else f"[{DIM}]{_esc(m.name)}[/]",
                          m.provider, m.model or f"[{DIM}]default[/]",
                          f"[{m.color}]●[/] {HEX_TO_NAME.get(m.color.upper(), m.color)}",
                          " ".join(roles))
        if current and self.work.get(current):
            table.move_cursor(row=self.work.index(current))

    async def _rebuild_chair(self) -> None:
        box = self.query_one("#chair-box", Vertical)
        await box.remove_children()
        buttons = [RadioButton(m.name, value=(m.name == self.work.chairman)) for m in self.work.members]
        await box.mount(RadioSet(*buttons, id="chair-set"))

    def _msg(self, text: str, kind: str = "info") -> None:
        color = {"error": RED, "warn": AMBER, "ok": TEAL}.get(kind, DIM)
        self.query_one("#cs-msg", Static).update(f"[{color}]{_esc(text)}[/]")

    def _update_hint(self, provider: str) -> None:
        if provider == "openrouter":
            hint = "Model is required: an OpenRouter id, e.g. openai/gpt-5.1"
        else:
            known = self._suggest(provider)
            hint = f"Known: {', '.join(known)}" if known else "Any model id the CLI accepts"
            if provider == "claude":
                hint += " (or a full claude-… name)"
        self.query_one("#model-hint", Static).update(_esc(hint))

    def selected_name(self) -> Optional[str]:
        table = self.query_one("#cs-table", DataTable)
        if not self.work.members or table.row_count == 0:
            return None
        i = min(max(table.cursor_row, 0), len(self.work.members) - 1)
        return self.work.members[i].name

    # -- events -----------------------------------------------------------------

    @on(Select.Changed, "#add-provider")
    def _provider_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self._update_hint(event.value)

    @on(RadioSet.Changed, "#chair-set")
    def _chair_changed(self, event: RadioSet.Changed) -> None:
        if 0 <= event.index < len(self.work.members):
            name = self.work.members[event.index].name
            if name != self.work.chairman:
                self.work.set_chair(name)
                self._fill_table()
                self._msg(f"{name} is now the chair.", "ok")

    @on(Button.Pressed)
    async def _button(self, event: Button.Pressed) -> None:
        actions = {"btn-toggle": self.action_toggle, "btn-remove": self.action_remove,
                   "btn-test": self.action_test, "btn-add": self.action_add,
                   "btn-save": self.action_save, "btn-cancel": self.action_cancel}
        bid = event.button.id or ""
        if bid in ("btn-up", "btn-down"):
            await self.action_move(-1 if bid == "btn-up" else 1)
        elif bid in actions:
            result = actions[bid]()
            if hasattr(result, "__await__"):
                await result

    @on(Input.Submitted, "#add-name, #add-model")
    async def _submit_add(self, event: Input.Submitted) -> None:
        event.stop()
        await self.action_add()

    # -- actions ----------------------------------------------------------------

    def action_toggle(self) -> None:
        name = self.selected_name()
        if not name:
            return
        m = self.work.get(name)
        m.enabled = not m.enabled
        self._fill_table(name)
        n = len(self.work.enabled)
        warn = f" Only {n} on; the council needs at least 2." if n < 2 else ""
        self._msg(f"{name} is {'on' if m.enabled else 'off'}.{warn}", "warn" if warn else "ok")

    async def action_remove(self) -> None:
        name = self.selected_name()
        if not name:
            return
        try:
            self.work.remove(name)
        except RosterError as e:
            self._msg(str(e), "error")
            return
        self._fill_table()
        await self._rebuild_chair()
        self._msg(f"Removed {name}.", "ok")

    async def action_make_chair(self) -> None:
        name = self.selected_name()
        if name:
            self.work.set_chair(name)
            self._fill_table(name)
            await self._rebuild_chair()
            self._msg(f"{name} is now the chair.", "ok")

    async def action_move(self, delta: int) -> None:
        name = self.selected_name()
        if name:
            self.work.move(name, delta)
            self._fill_table(name)
            await self._rebuild_chair()

    def action_focus_add(self) -> None:
        self.query_one("#add-name", Input).focus()

    async def action_add(self) -> None:
        name_in = self.query_one("#add-name", Input)
        model_in = self.query_one("#add-model", Input)
        provider = self.query_one("#add-provider", Select).value
        color = self.query_one("#add-color", Select).value
        new = normalize_member(MemberConfig(name_in.value, str(provider), model_in.value or None,
                                            str(color), True))
        errors, warnings = validate_member(new, self.work, check_cli=self.check_cli, models=self.models)
        if errors:
            self._msg(" ".join(errors), "error")
            return
        self.work.members.append(new)
        name_in.value = ""
        model_in.value = ""
        self.query_one("#add-color", Select).value = next_color(self.work)
        self._fill_table(new.name)
        await self._rebuild_chair()
        text = f"Added {new.name} ({new.spec})."
        self._msg(text + (" " + " ".join(warnings) if warnings else ""), "warn" if warnings else "ok")
        self.query_one("#cs-table", DataTable).focus()

    def action_test(self) -> None:
        name = self.selected_name()
        m = self.work.get(name) if name else None
        if m is None:
            return
        self._msg(f"Pinging {m.name} ({m.spec})…")
        self._ping(m)

    @work(exclusive=True, group="ping")
    async def _ping(self, m: MemberConfig) -> None:
        t0 = time.monotonic()
        ok, text = await ping_member(m.to_member(), timeout=120, query=self.query_fn)
        secs = time.monotonic() - t0
        first = " ".join(text.split())[:80]
        if ok:
            self._msg(f"✓ {m.name} answered in {secs:.0f}s: {first}", "ok")
        else:
            self._msg(f"✗ {m.name} failed after {secs:.0f}s: {first}", "error")

    def action_save(self) -> None:
        errors, warnings = validate(self.work, check_cli=self.check_cli, models=self.models)
        if errors:
            self._msg(" ".join(errors), "error")
            return
        try:
            self.saved_to = save_roster(self.work, check_cli=self.check_cli, models=self.models)
        except (RosterError, OSError) as e:
            self._msg(str(e), "error")
            return
        self.dismiss(self.work)

    def action_cancel(self) -> None:
        self.dismiss(None)
