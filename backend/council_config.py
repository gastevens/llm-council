"""War Room council roster: who sits on the council, who chairs, who writes titles.

The roster lives in ~/.config/war-room/council.json (override the path with
WARROOM_COUNCIL_CONFIG). When the file doesn't exist, the roster comes from the
COUNCIL_MEMBERS / COUNCIL_CHAIRMAN / COUNCIL_TITLE_MEMBER environment defaults
(see backend/config.py). The TUI, the Slack bot, ask.py and the web backend all
read it at the start of each question, so edits apply to the next question.

    uv run python -m backend.council_config list
    uv run python -m backend.council_config add Opus --provider claude --model opus --color pink
    uv run python -m backend.council_config remove Opus
    uv run python -m backend.council_config enable|disable NAME
    uv run python -m backend.council_config chair NAME
    uv run python -m backend.council_config title NAME
    uv run python -m backend.council_config models [PROVIDER]
    uv run python -m backend.council_config test NAME      # tiny prompt to check the CLI works
    uv run python -m backend.council_config reset          # delete the file, back to env defaults
"""

import argparse
import copy
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .providers import Member, _which

log = logging.getLogger("council.config")

# Grok Bot palette
PALETTE: Dict[str, str] = {
    "teal": "#20B8A4", "orange": "#F86522", "blue": "#1788F8", "purple": "#8F61F6",
    "pink": "#F93697", "green": "#23C275", "amber": "#F5922C", "red": "#FB2A41",
    "brown": "#A16A3E", "gray": "#777777",
}
HEX_TO_NAME = {v.upper(): k for k, v in PALETTE.items()}
DEFAULT_COLORS = {"ChatGPT": "#20B8A4", "Claude": "#F86522", "Grok": "#1788F8"}

PROVIDERS = ("codex", "claude", "grok", "openrouter")
PROVIDER_LABELS = {"codex": "Codex CLI (ChatGPT plan)", "claude": "Claude Code (Claude plan)",
                   "grok": "Grok Build (grok.com plan)", "openrouter": "OpenRouter API (needs key)"}
CLAUDE_ALIASES = ("opus", "sonnet", "haiku", "fable", "opusplan", "default")
MIN_ENABLED = 2
MAX_NAME = 24


class RosterError(ValueError):
    pass


@dataclass
class MemberConfig:
    name: str
    provider: str
    model: Optional[str] = None
    color: str = "#20B8A4"
    enabled: bool = True

    def to_member(self) -> Member:
        return Member(name=self.name, provider=self.provider, model=self.model or None)

    @property
    def spec(self) -> str:
        return f"{self.provider}:{self.model}" if self.model else self.provider


@dataclass
class Roster:
    members: List[MemberConfig] = field(default_factory=list)
    chairman: str = ""
    title_member: Optional[str] = None
    source: str = "env"  # "file" or "env" (not saved)

    # -- lookups ----------------------------------------------------------
    def get(self, name: str) -> Optional[MemberConfig]:
        key = (name or "").strip().lower()
        return next((m for m in self.members if m.name.lower() == key), None)

    def index(self, name: str) -> int:
        m = self.get(name)
        if m is None:
            raise RosterError(f"No council member named '{name}'.")
        return self.members.index(m)

    @property
    def enabled(self) -> List[MemberConfig]:
        return [m for m in self.members if m.enabled]

    def active_members(self) -> List[Member]:
        return [m.to_member() for m in self.enabled]

    def chair_config(self) -> MemberConfig:
        m = self.get(self.chairman)
        if m is None:
            raise RosterError(f"Chair '{self.chairman}' is not a council member.")
        return m

    def chair_member(self) -> Member:
        return self.chair_config().to_member()

    def title_member_obj(self) -> Member:
        m = self.get(self.title_member or "") or self.get(self.chairman)
        return m.to_member() if m else self.chair_member()

    def color(self, name: str) -> str:
        m = self.get(name)
        return m.color if m else PALETTE["gray"]

    def copy(self) -> "Roster":
        return copy.deepcopy(self)

    def to_json(self) -> Dict:
        return {"members": [asdict(m) for m in self.members], "chairman": self.chairman,
                "title_member": self.title_member}

    def summary(self) -> str:
        names = ", ".join(m.name for m in self.enabled) or "nobody"
        return f"{names} — chair: {self.chairman}"

    # -- edits (raise RosterError) ------------------------------------------
    def add(self, m: MemberConfig) -> MemberConfig:
        m = normalize_member(m)
        errors, _ = validate_member(m, self, check_cli=False)
        if errors:
            raise RosterError("; ".join(errors))
        self.members.append(m)
        return m

    def remove(self, name: str) -> MemberConfig:
        m = self.get(name)
        if m is None:
            raise RosterError(f"No council member named '{name}'.")
        if m.name == self.chair_config().name:
            raise RosterError(f"{m.name} is the chair. Make someone else chair first.")
        self.members.remove(m)
        if self.title_member and self.title_member.lower() == m.name.lower():
            self.title_member = self.chairman
        return m

    def set_enabled(self, name: str, enabled: bool) -> MemberConfig:
        m = self.get(name)
        if m is None:
            raise RosterError(f"No council member named '{name}'.")
        m.enabled = enabled
        return m

    def set_chair(self, name: str) -> MemberConfig:
        m = self.get(name)
        if m is None:
            raise RosterError(f"No council member named '{name}'.")
        self.chairman = m.name
        return m

    def set_title_member(self, name: str) -> MemberConfig:
        m = self.get(name)
        if m is None:
            raise RosterError(f"No council member named '{name}'.")
        self.title_member = m.name
        return m

    def move(self, name: str, delta: int) -> int:
        i = self.index(name)
        j = max(0, min(len(self.members) - 1, i + delta))
        self.members.insert(j, self.members.pop(i))
        return j


# ---------------------------------------------------------------------------
# Colors, normalization
# ---------------------------------------------------------------------------

def resolve_color(value: Optional[str]) -> Optional[str]:
    """'pink' / '#f93697' / 'F93697' -> '#F93697'; None if not a palette color."""
    if not value:
        return None
    v = value.strip()
    if v.lower() in PALETTE:
        return PALETTE[v.lower()]
    hexv = ("#" + v.lstrip("#")).upper()
    return hexv if hexv in HEX_TO_NAME else None


def next_color(roster: Roster) -> str:
    used = {m.color.upper() for m in roster.members}
    for hexv in PALETTE.values():
        if hexv.upper() not in used and hexv != PALETTE["purple"]:
            return hexv
    return PALETTE["gray"]


def normalize_member(m: MemberConfig) -> MemberConfig:
    color = resolve_color(m.color) or m.color
    model = (m.model or "").strip() or None
    return MemberConfig(name=(m.name or "").strip(), provider=(m.provider or "").strip().lower(),
                        model=model, color=color, enabled=bool(m.enabled))


# ---------------------------------------------------------------------------
# Model discovery (cheap, cached)
# ---------------------------------------------------------------------------

_MODEL_CACHE: Dict[str, Optional[List[str]]] = {}


def available_models(provider: str) -> Optional[List[str]]:
    """Known model ids for a provider, or None when we can't tell cheaply.

    codex: ~/.codex/models_cache.json (what the Codex CLI itself caches)
    grok:  `grok models`
    claude: aliases from `claude --help` (full names like claude-opus-4-... also work)
    """
    if provider in _MODEL_CACHE:
        return _MODEL_CACHE[provider]
    models: Optional[List[str]] = None
    try:
        if provider == "codex":
            path = Path.home() / ".codex" / "models_cache.json"
            data = json.loads(path.read_text())
            models = [m["slug"] for m in data.get("models", []) if m.get("slug")]
        elif provider == "grok":
            out = subprocess.run([_which("grok"), "models"], capture_output=True, text=True,
                                 timeout=20, stdin=subprocess.DEVNULL).stdout
            models = re.findall(r"^\s*[*-]\s+([\w.\-:/]+)", out, re.M) or None
        elif provider == "claude":
            models = list(CLAUDE_ALIASES)
    except Exception as e:
        log.debug("couldn't list %s models: %s", provider, e)
        models = None
    _MODEL_CACHE[provider] = models
    return models


def suggested_models(provider: str) -> List[str]:
    """Models worth offering in a picker (codex hides internal ones)."""
    if provider == "codex":
        try:
            data = json.loads((Path.home() / ".codex" / "models_cache.json").read_text())
            return [m["slug"] for m in data.get("models", []) if m.get("visibility", "list") == "list"]
        except Exception:
            return []
    if provider == "claude":
        return ["opus", "sonnet", "haiku"]
    return available_models(provider) or []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

ModelLister = Callable[[str], Optional[List[str]]]


def validate_member(m: MemberConfig, roster: Roster, check_cli: bool = True,
                    models: ModelLister = available_models) -> Tuple[List[str], List[str]]:
    """Checks one member against the roster (ignoring itself). Returns (errors, warnings)."""
    errors: List[str] = []
    warnings: List[str] = []
    if not m.name:
        errors.append("Every member needs a name.")
    elif len(m.name) > MAX_NAME:
        errors.append(f"'{m.name}' is too long (max {MAX_NAME} characters).")
    elif not re.fullmatch(r"[\w][\w .+\-]*", m.name):
        errors.append(f"'{m.name}': use letters, numbers, spaces, '.', '+' or '-'.")
    if any(o is not m and o.name.lower() == m.name.lower() for o in roster.members):
        errors.append(f"There's already a member named '{m.name}'.")
    if m.provider not in PROVIDERS:
        errors.append(f"{m.name}: provider must be one of {', '.join(PROVIDERS)} (got '{m.provider}').")
        return errors, warnings
    if m.color.upper() not in HEX_TO_NAME:
        errors.append(f"{m.name}: color must be one of {', '.join(PALETTE)}.")
    if m.model and not re.fullmatch(r"[\w.\-:/\[\]]+", m.model):
        errors.append(f"{m.name}: model '{m.model}' has unexpected characters.")
    if m.provider == "openrouter":
        if not m.model:
            errors.append(f"{m.name}: OpenRouter members need a model id, e.g. openai/gpt-5.1.")
        if check_cli and not os.getenv("OPENROUTER_API_KEY"):
            errors.append(f"{m.name}: OPENROUTER_API_KEY isn't set.")
    else:
        if check_cli:
            try:
                _which(m.provider)
            except FileNotFoundError:
                errors.append(f"{m.name}: the {m.provider} CLI isn't installed.")
        if m.model:
            known = models(m.provider)
            if m.provider == "claude":
                if m.model not in CLAUDE_ALIASES and not m.model.startswith("claude-"):
                    warnings.append(f"{m.name}: '{m.model}' isn't a known Claude alias "
                                    f"({', '.join(CLAUDE_ALIASES[:3])}) or full claude-… name.")
            elif known is not None and m.model not in known:
                errors.append(f"{m.name}: {m.provider} doesn't list model '{m.model}'. "
                              f"Known: {', '.join(known)}.")
    return errors, warnings


def validate(roster: Roster, check_cli: bool = True,
             models: ModelLister = available_models) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    for m in roster.members:
        e, w = validate_member(m, roster, check_cli=check_cli and (m.enabled or m.name.lower() == roster.chairman.lower()),
                               models=models)
        errors += e
        warnings += w
    if len(roster.enabled) < MIN_ENABLED:
        errors.append(f"Enable at least {MIN_ENABLED} council members (now {len(roster.enabled)}).")
    if not roster.get(roster.chairman):
        errors.append("Pick a chair from the configured members.")
    if roster.title_member and not roster.get(roster.title_member):
        errors.append(f"Title member '{roster.title_member}' is not a configured member.")
    # de-duplicate while keeping order
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def config_path() -> Path:
    return Path(os.getenv("WARROOM_COUNCIL_CONFIG") or Path.home() / ".config" / "war-room" / "council.json")


def env_default_roster() -> Roster:
    from .config import CHAIRMAN, COUNCIL_MEMBERS, TITLE_MEMBER
    r = Roster(source="env")
    for mem in COUNCIL_MEMBERS:
        r.members.append(MemberConfig(mem.name, mem.provider, mem.model,
                                      DEFAULT_COLORS.get(mem.name) or next_color(r), True))
    for extra in (CHAIRMAN, TITLE_MEMBER):
        if not any(m.name == extra.name and m.provider == extra.provider and m.model == extra.model
                   for m in r.members):
            name = extra.name
            if r.get(name):  # same display name, different model -> disambiguate
                name = f"{extra.name} ({extra.model or extra.provider})"[:MAX_NAME]
            r.members.append(MemberConfig(name, extra.provider, extra.model,
                                          DEFAULT_COLORS.get(name) or next_color(r), False))
    def find(mem: Member) -> str:
        return next(m.name for m in r.members if m.provider == mem.provider and m.model == mem.model
                    and (m.name == mem.name or m.name.startswith(mem.name)))
    r.chairman = find(CHAIRMAN)
    r.title_member = find(TITLE_MEMBER)
    return r


def roster_from_json(data: Dict) -> Roster:
    if not isinstance(data, dict) or not isinstance(data.get("members"), list):
        raise RosterError("council.json needs a 'members' list.")
    r = Roster(source="file")
    for i, raw in enumerate(data["members"]):
        if not isinstance(raw, dict):
            raise RosterError(f"members[{i}] must be an object.")
        r.members.append(normalize_member(MemberConfig(
            name=str(raw.get("name", "")), provider=str(raw.get("provider", "")),
            model=raw.get("model"), color=str(raw.get("color") or next_color(r)),
            enabled=bool(raw.get("enabled", True)))))
    r.chairman = str(data.get("chairman") or "")
    r.title_member = data.get("title_member") or None
    return r


def load_roster(path: Optional[Path] = None) -> Roster:
    """The saved roster, or the env defaults when there's no file. Raises RosterError if
    the file is malformed or invalid (CLI checks are skipped here)."""
    p = Path(path) if path else config_path()
    if not p.exists():
        return env_default_roster()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RosterError(f"Couldn't read {p}: {e}") from e
    r = roster_from_json(data)
    errors, _ = validate(r, check_cli=False, models=lambda _p: None)
    if errors:
        raise RosterError(f"{p}: " + " ".join(errors))
    return r


def current_roster() -> Roster:
    """Roster for the next question; falls back to env defaults if the file is broken."""
    try:
        return load_roster()
    except RosterError as e:
        log.warning("Ignoring council roster: %s", e)
        return env_default_roster()


def save_roster(roster: Roster, path: Optional[Path] = None, check_cli: bool = True,
                models: ModelLister = available_models) -> Path:
    errors, _ = validate(roster, check_cli=check_cli, models=models)
    if errors:
        raise RosterError(" ".join(errors))
    p = Path(path) if path else config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(roster.to_json(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    roster.source = "file"
    return p


def roster_lines(roster: Roster, markdown: bool = False) -> List[str]:
    lines = []
    for i, m in enumerate(roster.members, 1):
        roles = []
        if m.name == roster.chairman:
            roles.append("chair")
        if roster.title_member and m.name == roster.title_member:
            roles.append("titles")
        state = "on " if m.enabled else "off"
        color = HEX_TO_NAME.get(m.color.upper(), m.color)
        role = f"  [{', '.join(roles)}]" if roles else ""
        if markdown:
            mark = ":white_check_mark:" if m.enabled else ":white_circle:"
            lines.append(f"{mark} *{m.name}* — `{m.spec}` ({color}){role}")
        else:
            lines.append(f"{i:>2}. {state}  {m.name:<{MAX_NAME}} {m.spec:<28} {color:<7}{role}")
    return lines


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

PING_PROMPT = "Reply with just the word OK."


async def ping_member(member: Member, timeout: float = 120.0, query=None) -> Tuple[bool, str]:
    """Send a tiny prompt; returns (ok, answer-or-error)."""
    from .providers import query_member

    q = query or query_member
    errs: Dict[str, str] = {}

    class Tap(logging.Handler):
        def emit(self, record):
            if record.args and len(record.args) >= 3:
                errs[str(record.args[0])] = str(record.args[2])

    plog = logging.getLogger("council.providers")
    tap = Tap(logging.WARNING)
    plog.addHandler(tap)
    try:
        resp = await q(member, [{"role": "user", "content": PING_PROMPT}], timeout=timeout)
    finally:
        plog.removeHandler(tap)
    text = (resp or {}).get("content") if resp else None
    if text:
        return True, text.strip()
    return False, errs.get(member.name, "no answer")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_roster(r: Roster) -> None:
    src = str(config_path()) if r.source == "file" else "environment defaults (no council.json yet)"
    print(f"Council roster — {src}")
    for line in roster_lines(r):
        print(line)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.council_config",
                                     description="Manage the War Room council roster.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("list", help="show the roster")
    sub.add_parser("path", help="print the roster file path")
    a = sub.add_parser("add", help="add a member")
    a.add_argument("name")
    a.add_argument("--provider", "-p", required=True, choices=PROVIDERS)
    a.add_argument("--model", "-m")
    a.add_argument("--color", "-c", help=f"one of {', '.join(PALETTE)} (default: next unused)")
    a.add_argument("--disabled", action="store_true", help="add it switched off")
    for cmd, helptext in (("remove", "remove a member"), ("enable", "switch a member on"),
                          ("disable", "switch a member off"), ("chair", "make a member the chair"),
                          ("title", "member that writes conversation titles"),
                          ("test", "ping a member with a tiny prompt"),
                          ("up", "move a member up"), ("down", "move a member down")):
        sp = sub.add_parser(cmd, help=helptext)
        sp.add_argument("name")
    m = sub.add_parser("models", help="model options per provider")
    m.add_argument("provider", nargs="?", choices=PROVIDERS)
    sub.add_parser("reset", help="delete council.json (back to env defaults)")
    args = parser.parse_args(argv)
    cmd = args.cmd or "list"

    try:
        if cmd == "path":
            print(config_path())
            return 0
        if cmd == "reset":
            p = config_path()
            if p.exists():
                p.unlink()
                print(f"Removed {p}; using environment defaults.")
            else:
                print("No council.json; already using environment defaults.")
            return 0
        if cmd == "models":
            for prov in ([args.provider] if args.provider else PROVIDERS):
                if prov == "openrouter":
                    print("openrouter: any OpenRouter model id, e.g. openai/gpt-5.1 (needs OPENROUTER_API_KEY)")
                    continue
                known = suggested_models(prov)
                extra = " (or a full claude-… model name)" if prov == "claude" else ""
                print(f"{prov}: {', '.join(known) if known else '(could not list; any id is passed to --model)'}{extra}")
            return 0

        roster = load_roster()
        if cmd == "list":
            _print_roster(roster)
            return 0
        if cmd == "test":
            import asyncio
            mem = roster.get(args.name)
            if mem is None:
                raise RosterError(f"No council member named '{args.name}'.")
            print(f"Pinging {mem.name} ({mem.spec})…")
            ok, text = asyncio.run(ping_member(mem.to_member()))
            print(("OK: " if ok else "FAILED: ") + text[:300])
            return 0 if ok else 1

        if cmd == "add":
            color = resolve_color(args.color) if args.color else next_color(roster)
            if args.color and not color:
                raise RosterError(f"Unknown color '{args.color}'. Use one of {', '.join(PALETTE)}.")
            new = MemberConfig(args.name, args.provider, args.model, color, not args.disabled)
            errors, warnings = validate_member(normalize_member(new), roster)
            if errors:
                raise RosterError(" ".join(errors))
            for w in warnings:
                print(f"warning: {w}")
            roster.add(new)
        elif cmd == "remove":
            roster.remove(args.name)
        elif cmd in ("enable", "disable"):
            roster.set_enabled(args.name, cmd == "enable")
        elif cmd == "chair":
            roster.set_chair(args.name)
        elif cmd == "title":
            roster.set_title_member(args.name)
        elif cmd in ("up", "down"):
            roster.move(args.name, -1 if cmd == "up" else 1)
        path = save_roster(roster)
        print(f"Saved {path}")
        _print_roster(roster)
        return 0
    except RosterError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
