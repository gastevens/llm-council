"""Council member providers.

Each council member is reached either through a locally installed, subscription-
authenticated coding CLI (Codex CLI -> ChatGPT plan, Claude Code -> Claude plan,
Grok Build -> grok.com plan) or, optionally, through OpenRouter.

Every CLI call runs as a one-shot, non-interactive subprocess inside a fresh,
empty temporary directory with tools disabled (or, for Codex, a read-only
sandbox), so council members answer from the model only and never touch files.
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import tempfile
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


async def _query_codex(member: Member, prompt: str, workdir: str, timeout: float) -> str:
    out_file = os.path.join(workdir, ".codex-last-message.txt")
    cmd = [
        _which("codex"), "exec",
        "--skip-git-repo-check", "--ephemeral",
        "--sandbox", "read-only",
        "--color", "never",
        "--cd", workdir,
        "--output-last-message", out_file,
    ]
    if member.model:
        cmd += ["--model", member.model]
    cmd.append("-")  # read prompt from stdin
    stdout = await _run(cmd, prompt, workdir, timeout)
    if os.path.exists(out_file):
        with open(out_file, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    return stdout.strip()


async def _query_claude(member: Member, prompt: str, workdir: str, timeout: float) -> str:
    cmd = [
        _which("claude"), "-p",
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--no-session-persistence",
        "--output-format", "json",
    ]
    if member.model:
        cmd += ["--model", member.model]
    stdout = await _run(cmd, prompt, workdir, timeout)
    data = json.loads(stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude error: {str(data.get('result'))[:300]}")
    return (data.get("result") or "").strip()


async def _query_grok(member: Member, prompt: str, workdir: str, timeout: float) -> str:
    prompt_file = os.path.join(workdir, ".grok-prompt.txt")
    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write(prompt)
    cmd = [
        _which("grok"),
        "--prompt-file", prompt_file,
        "--tools", "",
        "--disable-web-search",
        "--no-subagents",
        "--verbatim",
        "--cwd", workdir,
        "--output-format", "json",
    ]
    if member.model:
        cmd += ["--model", member.model]
    stdout = await _run(cmd, None, workdir, timeout)
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


async def query_member(
    member: Member,
    messages: List[Dict[str, str]],
    timeout: float,
) -> Optional[Dict[str, Any]]:
    """Query one council member. Returns {'content': ...} or None on failure."""
    try:
        if member.provider == "openrouter":
            content = await _query_openrouter(member, messages, timeout)
        else:
            handler = _CLI_HANDLERS[member.provider]
            prompt = CLI_PREAMBLE + _messages_to_prompt(messages)
            with tempfile.TemporaryDirectory(prefix="llm-council-") as workdir:
                content = await handler(member, prompt, workdir, timeout)
        if not content:
            raise RuntimeError("empty response")
        return {"content": content}
    except Exception as e:  # graceful degradation, like the original client
        print(f"Error querying {member.name} via {member.provider}: {e}")
        log.warning("%s (%s) failed: %s", member.name, member.provider, e)
        return None
