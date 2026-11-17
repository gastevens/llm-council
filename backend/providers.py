"""Council member providers.

Each council member is reached either through a locally installed, subscription-
authenticated coding CLI (Codex CLI -> ChatGPT plan, Claude Code -> Claude plan,
Grok Build -> grok.com plan) or, optionally, through OpenRouter.

Every CLI call runs as a one-shot, non-interactive subprocess inside a fresh,
empty temporary directory with tools disabled (or, for Codex, a read-only
sandbox), so council members answer from the model only and never touch files.

Exception: while a *folder-mode* context is active (see ``use_context`` and
``backend.context``), CLI members run with that folder as their working
directory and READ-ONLY file tools so they can open the files themselves:

    codex   --sandbox read-only --cd FOLDER, web search disabled
            (it reads with shell commands; the sandbox blocks writes and network)
    claude  --tools Read,Glob,Grep, --permission-mode dontAsk (no Bash/Edit/Write/Web)
    grok    --tools read_file,list_dir,grep --sandbox read-only --permission-mode dontAsk
            --disable-web-search --no-subagents

OpenRouter members have no file access, so they get the files pasted inline
(truncated to the budget) instead. Folder-mode calls get COUNCIL_TIMEOUT x
COUNCIL_CONTEXT_TIMEOUT_FACTOR (default 2).
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

log = logging.getLogger("council.providers")

# CLI subprocesses currently running. Each one is started in its own session
# (so it never grabs the caller's terminal), which also means a terminal hangup
# does not reach it; front ends call kill_active_processes() on shutdown.
_ACTIVE: Set[asyncio.subprocess.Process] = set()


def kill_active_processes(sig: int = signal.SIGTERM) -> int:
    """Signal every running CLI subprocess (and its process group). Thread-safe enough
    to call from a signal handler or another thread; returns how many were signalled."""
    n = 0
    for proc in list(_ACTIVE):
        if proc.returncode is not None:
            continue
        try:
            os.killpg(proc.pid, sig)  # start_new_session=True -> pgid == pid
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(proc.pid, sig)
            except ProcessLookupError:
                continue
        n += 1
    return n


@dataclass(frozen=True)
class Member:
    name: str              # display name shown in the UI, e.g. "ChatGPT"
    provider: str          # "codex" | "claude" | "grok" | "openrouter"
    model: Optional[str] = None  # optional model override / OpenRouter model id


def _messages_to_prompt(messages: List[Dict[str, str]]) -> str:
    """Flatten chat messages into a single prompt for one-shot CLIs."""
    if len(messages) == 1:
        return messages[0]["content"]
    parts = []
    for m in messages:
        parts.append(f"{m['role'].upper()}:\n{m['content']}")
    return "\n\n".join(parts)


# Prepended to every CLI prompt so coding agents answer in chat rather than
# trying to inspect a (non-existent) codebase.
CLI_PREAMBLE = (
    "You are a member of a council of AI models answering a user's question. "
    "Answer directly from your own knowledge in Markdown. Do not run commands, "
    "read or write files, or ask follow-up questions.\n\n"
)


FOLDER_PREAMBLE = (
    "You are a member of a council of AI models answering a user's question about the files "
    "in your current working directory. You may list, read and search those files with your "
    "read-only tools (for a shell, only read-only commands such as ls, cat, head, rg). Do not "
    "create, modify or delete files, install anything, browse the web, or ask follow-up "
    "questions. Answer in Markdown and cite the file paths you rely on.\n\n"
)

# The context folder for the council run in progress (a backend.context.FolderContext).
# asyncio tasks copy context variables when they are created, so setting this
# around a council run reaches every member call made inside it.
_CONTEXT: ContextVar[Optional[Any]] = ContextVar("council_context", default=None)


@contextmanager
def use_context(ctx: Optional[Any]):
    """Make `ctx` the active context folder for member calls inside the block."""
    token = _CONTEXT.set(ctx)
    try:
        yield ctx
    finally:
        _CONTEXT.reset(token)


def active_context() -> Optional[Any]:
    return _CONTEXT.get()


def folder_timeout(timeout: float) -> float:
    try:
        factor = float(os.getenv("COUNCIL_CONTEXT_TIMEOUT_FACTOR", "2") or 2)
    except ValueError:
        factor = 2.0
    return timeout * max(1.0, factor)


def _which(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        # launchd / GUI launches often lack Homebrew on PATH
        for d in ("/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin")):
            candidate = os.path.join(d, binary)
            if os.path.exists(candidate):
                return candidate
        raise FileNotFoundError(f"{binary} CLI not found on PATH")
    return path


async def _run(cmd: List[str], stdin_text: Optional[str], cwd: str, timeout: float) -> str:
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    _ACTIVE.add(proc)
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin_text.encode() if stdin_text is not None else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        _killpg(proc)
        await proc.wait()
        raise TimeoutError(f"{os.path.basename(cmd[0])} timed out after {timeout:.0f}s")
    except asyncio.CancelledError:
        _killpg(proc)
        raise
    finally:
        _ACTIVE.discard(proc)
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip()[-500:]
        raise RuntimeError(f"{os.path.basename(cmd[0])} exited {proc.returncode}: {tail}")
    return out.decode(errors="replace")


def _killpg(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def codex_cmd(member: Member, workdir: str, out_file: str, folder: Optional[str] = None) -> List[str]:
    cmd = [
        _which("codex"), "exec",
        "--skip-git-repo-check", "--ephemeral",
        "--sandbox", "read-only",
        "--color", "never",
        "--cd", folder or workdir,
        "--output-last-message", out_file,
    ]
    if folder:
        cmd += ["-c", 'web_search="disabled"']
    if member.model:
        cmd += ["--model", member.model]
    cmd.append("-")  # read prompt from stdin
    return cmd


CLAUDE_READ_TOOLS = "Read,Glob,Grep"
CLAUDE_DENY = "Bash,Edit,MultiEdit,Write,NotebookEdit,WebFetch,WebSearch,Task"


def claude_cmd(member: Member, folder: Optional[str] = None) -> List[str]:
    cmd = [_which("claude"), "-p"]
    if folder:
        cmd += [
            "--tools", CLAUDE_READ_TOOLS,
            "--allowedTools", CLAUDE_READ_TOOLS,
            "--disallowedTools", CLAUDE_DENY,
            "--permission-mode", "dontAsk",
            "--add-dir", folder,
        ]
    else:
        cmd += ["--tools", ""]
    cmd += [
        "--strict-mcp-config",
        "--setting-sources", "",
        "--no-session-persistence",
        "--output-format", "json",
    ]
    if member.model:
        cmd += ["--model", member.model]
    return cmd


GROK_READ_TOOLS = "read_file,list_dir,grep"


def grok_cmd(member: Member, prompt_file: str, workdir: str, folder: Optional[str] = None) -> List[str]:
    cmd = [_which("grok"), "--prompt-file", prompt_file]
    if folder:
        cmd += ["--tools", GROK_READ_TOOLS, "--permission-mode", "dontAsk", "--sandbox", "read-only"]
    else:
        cmd += ["--tools", ""]
    cmd += [
        "--disable-web-search",
        "--no-subagents",
        "--verbatim",
        "--cwd", folder or workdir,
        "--output-format", "json",
    ]
    if member.model:
        cmd += ["--model", member.model]
    return cmd


async def _query_codex(member: Member, prompt: str, workdir: str, timeout: float,
                       folder: Optional[str] = None) -> str:
    out_file = os.path.join(workdir, ".codex-last-message.txt")
    cmd = codex_cmd(member, workdir, out_file, folder)
    stdout = await _run(cmd, prompt, folder or workdir, timeout)
    if os.path.exists(out_file):
        with open(out_file, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    return stdout.strip()


async def _query_claude(member: Member, prompt: str, workdir: str, timeout: float,
                        folder: Optional[str] = None) -> str:
    stdout = await _run(claude_cmd(member, folder), prompt, folder or workdir, timeout)
    data = json.loads(stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude error: {str(data.get('result'))[:300]}")
    return (data.get("result") or "").strip()


async def _query_grok(member: Member, prompt: str, workdir: str, timeout: float,
                      folder: Optional[str] = None) -> str:
    # The prompt file lives in the scratch dir, never inside the shared folder.
    prompt_file = os.path.join(workdir, ".grok-prompt.txt")
    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write(prompt)
    stdout = await _run(grok_cmd(member, prompt_file, workdir, folder), None, folder or workdir, timeout)
    data = json.loads(stdout)
    return (data.get("text") or "").strip()


async def _query_openrouter(member: Member, messages: List[Dict[str, str]], timeout: float) -> str:
    from .openrouter import query_model as openrouter_query
    resp = await openrouter_query(member.model, messages, timeout=timeout)
    if resp is None:
        raise RuntimeError("OpenRouter request failed")
    return resp.get("content") or ""


_CLI_HANDLERS = {
    "codex": _query_codex,
    "claude": _query_claude,
    "grok": _query_grok,
}


def _inline_fallback(messages: List[Dict[str, str]], ctx: Any) -> List[Dict[str, str]]:
    """For members without file tools in folder mode: paste what fits instead."""
    from .context import inline_block
    note = ("(You have no file access, so as much of the folder as fits is pasted here; "
            "files marked 'Not included' weren't available to you.)\n\n")
    first = dict(messages[0])
    first["content"] = note + inline_block(ctx) + "\n\n---\n\n" + first["content"]
    return [first] + list(messages[1:])


async def query_member(
    member: Member,
    messages: List[Dict[str, str]],
    timeout: float,
) -> Optional[Dict[str, Any]]:
    """Query one council member. Returns {'content': ...} or None on failure."""
    ctx = _CONTEXT.get()
    folder = str(ctx.root if ctx.root.is_dir() else ctx.root.parent) if (
        ctx is not None and getattr(ctx, "mode", None) == "folder") else None
    try:
        if member.provider == "openrouter":
            if folder:
                messages = _inline_fallback(messages, ctx)
            content = await _query_openrouter(member, messages, timeout)
        else:
            handler = _CLI_HANDLERS[member.provider]
            preamble = FOLDER_PREAMBLE if folder else CLI_PREAMBLE
            prompt = preamble + _messages_to_prompt(messages)
            with tempfile.TemporaryDirectory(prefix="llm-council-") as workdir:
                if folder:
                    content = await handler(member, prompt, workdir, folder_timeout(timeout), folder)
                else:
                    content = await handler(member, prompt, workdir, timeout)
        if not content:
            raise RuntimeError("empty response")
        return {"content": content}
    except Exception as e:  # graceful degradation, like the original client
        print(f"Error querying {member.name} via {member.provider}: {e}")
        log.warning("%s (%s) failed: %s", member.name, member.provider, e)
        return None
