"""Context folders for the Slack bot.

* `/warroom context PATH` sets a folder on the Mac running the bot as the
  context for every council started in that channel (`/warroom context` shows
  it, `/warroom context off` clears it). Saved in
  `~/.config/war-room/slack-context.json` next to the roster.
* Files attached to an @mention or a reply in a War Room thread are downloaded
  into a per-thread folder and become that thread's context (they win over the
  channel folder). Needs the `files:read` bot scope.

Safety: when WARROOM_ADMINS is set only those users can set or clear a channel
folder; paths must sit inside WARROOM_CONTEXT_ROOTS (comma- or colon-separated,
default: your home folder) and may not go through hidden folders (~/.ssh, …).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .context import ContextError, FolderContext, build_context, describe, resolve_path
from .council_config import config_path
from .slack_roster import admin_allowlist

OFF_WORDS = frozenset({"off", "clear", "none", "remove"})
MODE_FLAGS = {"--folder": "folder", "--inline": "inline", "--auto": "auto"}
DENIED = (":no_entry: Only War Room admins can change the context folder. "
          "`/warroom context` shows the current one; you can also attach files to your question.")

CONTEXT_HELP = """*Context folders*
`/warroom context PATH` — the council reads that folder (on the bot's Mac) for questions in this channel. Add `--folder` or `--inline` to force a mode.
`/warroom context` — show the current context  ·  `/warroom context off` — remove it
Or attach files to your @mention / thread reply: they become that thread's context."""


def can_set_context(user_id: str) -> bool:
    allowed = admin_allowlist()
    return allowed is None or (user_id or "") in allowed


def allowed_roots() -> List[Path]:
    raw = os.getenv("WARROOM_CONTEXT_ROOTS", "")
    parts = [p for p in re.split(r"[,:]", raw) if p.strip()] if raw.strip() else [str(Path.home())]
    roots = []
    for p in parts:
        try:
            roots.append(Path(os.path.expanduser(p.strip())).resolve())
        except OSError:
            continue
    return roots


def check_allowed(path: Path, roots: Optional[List[Path]] = None) -> Path:
    """Raise ContextError unless `path` (already resolved) is inside an allowed root
    and doesn't pass through a hidden folder below that root."""
    roots = roots if roots is not None else allowed_roots()
    for root in roots:
        if path == root or root in path.parents:
            rel = path.relative_to(root).parts
            if any(part.startswith(".") for part in rel):
                raise ContextError("Hidden folders (like .ssh or .config) can't be used as context.")
            return path
    shown = ", ".join(_tilde(r) for r in roots) or "(none)"
    raise ContextError(f"{_tilde(path)} is outside the allowed context folders ({shown}). "
                       "Set WARROOM_CONTEXT_ROOTS in .env to allow more.")


def _tilde(p: Path) -> str:
    home = str(Path.home())
    s = str(p)
    return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s


# ---------------------------------------------------------------------------
# Channel context store
# ---------------------------------------------------------------------------

def store_path() -> Path:
    return Path(os.getenv("WARROOM_SLACK_CONTEXT_FILE") or config_path().parent / "slack-context.json")


class ContextStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or store_path()

    def _load(self) -> Dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: Dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, channel: str) -> Optional[Dict]:
        return (self._load().get("channels") or {}).get(channel)

    def set(self, channel: str, path: str, mode: Optional[str], user: str) -> None:
        data = self._load()
        data.setdefault("channels", {})[channel] = {
            "path": path, "mode": mode, "set_by": user, "set_at": datetime.now().isoformat(timespec="seconds")}
        self._save(data)

    def clear(self, channel: str) -> bool:
        data = self._load()
        had = channel in (data.get("channels") or {})
        if had:
            del data["channels"][channel]
            self._save(data)
        return had


# ---------------------------------------------------------------------------
# /warroom context …
# ---------------------------------------------------------------------------

def looks_like_path(text: str) -> bool:
    t = text.strip().strip('"').strip("'")
    return bool(t) and (t[0] in "~/.$" or "/" in t)


def parse_context_args(rest: str) -> Tuple[str, Optional[str], Optional[str]]:
    """`rest` is the text after `context`. Returns (action, path, mode)."""
    rest = (rest or "").strip()
    if not rest or rest.lower() == "show":
        return "show", None, None
    if rest.lower() in OFF_WORDS:
        return "off", None, None
    mode = None
    for flag, m in MODE_FLAGS.items():
        pat = rf"(^|\s){re.escape(flag)}(?=\s|$)"
        if re.search(pat, rest, flags=re.I):
            mode = m
            rest = re.sub(pat, " ", rest, flags=re.I)
    path = rest.strip().strip('"').strip("'").replace("\\ ", " ")
    return ("set", path, mode) if path else ("show", None, mode)


def handle_context_command(rest: str, *, user_id: str, channel_id: str,
                           store: Optional[ContextStore] = None,
                           build: Callable[..., FolderContext] = build_context) -> str:
    """Ephemeral reply text for `/warroom context …`."""
    store = store or ContextStore()
    action, raw_path, mode = parse_context_args(rest)
    if action == "show":
        cur = store.get(channel_id)
        if not cur:
            return ("No context folder for this channel.\n\n" + CONTEXT_HELP)
        try:
            ctx = build(cur["path"], mode=cur.get("mode"))
            body = describe(ctx)
        except ContextError as e:
            body = f":warning: `{cur['path']}` can't be read right now: {e}"
        return f":file_folder: {body}\nSet by <@{cur.get('set_by', '?')}> on {cur.get('set_at', '?')}."
    if not can_set_context(user_id):
        return DENIED
    if action == "off":
        return (":wastebasket: Context folder removed for this channel." if store.clear(channel_id)
                else "This channel had no context folder.")
    try:
        path = check_allowed(resolve_path(raw_path, base=str(Path.home())))
        ctx = build(str(path), mode=mode)
    except ContextError as e:
        return f":x: Context not set: {e}"
    store.set(channel_id, str(path), mode, user_id)
    return (f":file_folder: Context set for this channel. {describe(ctx)}\n"
            "Every council started here will read it until `/warroom context off`. "
            "Files attached in a thread take precedence for that thread.")


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

def files_root() -> Path:
    return Path(os.getenv("WARROOM_FILES_DIR") or Path.home() / "Library" / "Caches" / "war-room" / "slack-files")


def thread_dir(channel: str, thread_ts: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{channel}-{thread_ts}")
    return files_root() / safe


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "file").strip() or "file"
    name = re.sub(r"[^\w.\- ()]+", "_", name).lstrip(".") or "file"
    return name[:120]


def max_file_bytes() -> int:
    try:
        return int(float(os.getenv("WARROOM_MAX_FILE_MB", "25")) * 1024 * 1024)
    except ValueError:
        return 25 * 1024 * 1024


def download_files(files: List[Dict], dest: Path, token: str,
                   fetch: Optional[Callable[[str, str], bytes]] = None,
                   info: Optional[Callable[[str], Dict]] = None) -> Tuple[List[str], List[str]]:
    """Download Slack file objects into `dest`. Returns (saved names, problems)."""
    fetch = fetch or _http_fetch
    saved, problems = [], []
    limit = max_file_bytes()
    for f in files or []:
        name = f.get("name") or f.get("title") or f.get("id") or "file"
        try:
            if not f.get("url_private_download") and not f.get("url_private") and info and f.get("id"):
                f = {**f, **(info(f["id"]) or {})}
            if f.get("mode") in ("external", "tombstone") or f.get("is_external"):
                problems.append(f"{name}: linked from another service, not uploaded to Slack")
                continue
            url = f.get("url_private_download") or f.get("url_private")
            if not url:
                problems.append(f"{name}: no download link (does the app have the files:read scope?)")
                continue
            if (f.get("size") or 0) > limit:
                problems.append(f"{name}: larger than {limit // (1024 * 1024)} MB")
                continue
            data = fetch(url, token)
            if len(data) > limit:
                problems.append(f"{name}: larger than {limit // (1024 * 1024)} MB")
                continue
            if data[:15].lower().startswith((b"<!doctype html", b"<html")) and not name.lower().endswith((".html", ".htm")):
                problems.append(f"{name}: Slack returned a login page (missing files:read scope?)")
                continue
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / _safe_name(name)
            stem, suffix, n = target.stem, target.suffix, 2
            while target.exists():
                target = dest / f"{stem} ({n}){suffix}"
                n += 1
            target.write_bytes(data)
            saved.append(target.name)
        except Exception as e:
            problems.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
    return saved, problems


def _http_fetch(url: str, token: str) -> bytes:
    import httpx
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        r = client.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.content


def prune_old_threads(days: float = 14) -> None:
    root = files_root()
    if not root.is_dir():
        return
    cutoff = time.time() - days * 86400
    for d in root.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Which context does a council in this thread get?
# ---------------------------------------------------------------------------

def resolve_context(channel: str, thread_ts: Optional[str],
                    store: Optional[ContextStore] = None) -> Tuple[Optional[FolderContext], Optional[str]]:
    """(context, problem) for a council in this channel/thread.

    Thread attachments win over the channel folder. A channel folder is
    re-checked against the allowed roots every time (env may have changed).
    """
    if thread_ts:
        d = thread_dir(channel, thread_ts)
        if d.is_dir() and any(d.iterdir()):
            try:
                n = sum(1 for _ in d.iterdir())
                return build_context(str(d), display=f"{n} attached file{'s' if n != 1 else ''}"), None
            except ContextError as e:
                return None, f"Couldn't use the attached files: {e}"
    cur = (store or ContextStore()).get(channel)
    if not cur:
        return None, None
    try:
        path = check_allowed(resolve_path(cur["path"]))
        return build_context(str(path), mode=cur.get("mode")), None
    except ContextError as e:
        return None, f"The channel's context folder can't be used: {e}"


def context_note(ctx: FolderContext) -> str:
    where = "" if ctx.display else f" (`{_tilde(ctx.root)}`)"
    how = ("pasted into the prompt" if ctx.mode == "inline"
           else "members open the files with read-only tools")
    return f":file_folder: *Context:* {ctx.summary()}{where} — {how}."
