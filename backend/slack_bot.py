"""Slack front end for the War Room council (Slack Bolt, Socket Mode).

    uv run python -m backend.slack_bot                  # connect to Slack
    uv run python -m backend.slack_bot --dry-run "Q"    # run the council, print the Slack posts
    uv run python -m backend.slack_bot --dry-run --fake "Q"   # canned answers, no CLI calls

Triggers:
  * @War Room <question> in any channel the bot is in (intended: #war-room)
  * /warroom <question>    (/warroom help lists roster commands: roster, add,
    remove, enable, disable, chair, title, models, test, reset)
  * any plain reply inside a War Room thread = follow-up question (earlier
    questions and verdicts from the thread are included as context)
  * /warroom context PATH | off — a folder on this Mac the council reads for
    questions in that channel; files attached to a question become that
    thread's context (see backend.slack_context)

Tokens come from the environment or the repo's .env file and are never printed:
  SLACK_BOT_TOKEN  (xoxb-...)  bot token from "OAuth & Permissions"
  SLACK_APP_TOKEN  (xapp-...)  app-level token with connections:write

Optional:
  WARROOM_MAX_CONCURRENT  councils allowed to run at once (default 1; others queue)
  WARROOM_CRITIQUE_CHARS  max chars of each Stage 2 critique to post (default 1200, 0 = full)
  WARROOM_ADMINS          comma-separated Slack user IDs allowed to edit the roster
                          and set context folders (unset = anyone in the workspace)
  WARROOM_CONTEXT_ROOTS   folders `/warroom context` may point into (default: $HOME)
  WARROOM_MAX_FILE_MB     largest attachment downloaded as context (default 25)
"""

import argparse
import asyncio
import html
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")  # before config import so launchd runs see .env

from .config import TIMEOUT  # noqa: E402
from .council_config import Roster, config_path, current_roster, ping_member  # noqa: E402
from . import slack_context, slack_roster  # noqa: E402
from .context import FolderContext, stage1_prompt  # noqa: E402
from .council import (  # noqa: E402
    calculate_aggregate_rankings,
    stage2_collect_rankings,
    stage3_synthesize_final,
)
from .providers import Member, query_member, use_context  # noqa: E402
from .slack_format import (  # noqa: E402
    VERDICT_PREFIX,
    build_followup_question,
    convening_text,
    format_member_answer,
    format_rankings,
    format_verdict,
    strip_mentions,
)

log = logging.getLogger("warroom")

MAX_CONCURRENT = max(1, int(os.getenv("WARROOM_MAX_CONCURRENT", "1")))
CRITIQUE_CHARS = int(os.getenv("WARROOM_CRITIQUE_CHARS", "1200"))
CONVENING_PREFIX = ":classical_building: Convening the council"
DEFAULT_FILES_QUESTION = ("Review the attached files: summarize what they contain and flag anything "
                          "important, risky or inconsistent.")
SLASH_ROOT_MARK = "asked the war room:"

Post = Callable[[str], None]


# ---------------------------------------------------------------------------
# Council run that posts as each stage finishes
# ---------------------------------------------------------------------------

async def deliberate(question: str, post: Post, critique_chars: int = CRITIQUE_CHARS,
                     query: Callable[..., Awaitable] = query_member,
                     roster: Optional[Roster] = None,
                     context: Optional[FolderContext] = None) -> Dict:
    """Run the 3-stage council, calling post(text) for every Slack message.

    Stage 1 answers are posted one per member as soon as each finishes.
    Returns the stage 3 result dict.
    """
    with use_context(context):
        return await _deliberate(question, post, critique_chars, query, roster, context)


async def _deliberate(question: str, post: Post, critique_chars: int, query: Callable[..., Awaitable],
                      roster: Optional[Roster], context: Optional[FolderContext]) -> Dict:
    async def say(texts: List[str]) -> None:
        for t in texts:
            await asyncio.to_thread(post, t)

    roster = roster or current_roster()
    members = roster.active_members()
    chairman = roster.chair_member()
    messages = [{"role": "user", "content": stage1_prompt(question, context)}]

    async def ask(m: Member):
        return m, await query(m, messages, timeout=TIMEOUT)

    answers: Dict[str, Optional[str]] = {}
    for fut in asyncio.as_completed([ask(m) for m in members]):
        member, resp = await fut
        text = (resp or {}).get("content") if resp else None
        answers[member.name] = text
        await say(format_member_answer(member.name, text))

    stage1 = [{"model": m.name, "response": answers[m.name]}
              for m in members if answers.get(m.name)]
    if not stage1:
        await say([":x: Every council member failed to answer. Check that the CLIs are signed in on the Mac mini, then try again."])
        return {"model": "error", "response": "All models failed to respond."}

    stage2, label_to_model = await stage2_collect_rankings(question, stage1, members, context)
    aggregate = calculate_aggregate_rankings(stage2, label_to_model)
    reviewed = {r["model"] for r in stage2}
    failed = [m.name for m in members if m.name not in reviewed]
    await say(format_rankings(stage2, label_to_model, aggregate, critique_chars, failed))

    stage3 = await stage3_synthesize_final(question, stage1, stage2, chairman, context)
    await say(format_verdict(stage3))
    return stage3


def run_council_thread(question: str, post: Post, slots: threading.BoundedSemaphore,
                       prepare: Optional[Callable[[], None]] = None,
                       resolve: Optional[Callable[[], tuple]] = None) -> None:
    """Blocking wrapper used from a worker thread.

    `prepare` runs first (e.g. downloading attachments); `resolve` returns
    (FolderContext | None, problem | None) for this channel/thread.
    """
    if prepare is not None:
        try:
            prepare()
        except Exception as e:
            log.exception("preparing the council failed")
            post(f":warning: Couldn't fetch the attached files: `{type(e).__name__}: {str(e)[:200]}`")
    if not slots.acquire(blocking=False):
        post(":hourglass_flowing_sand: Another council is in session; yours is queued and will start when it finishes.")
        slots.acquire()
    try:
        roster = current_roster()  # read per question, so roster edits apply to the next one
        post(convening_text([m.name for m in roster.enabled], roster.chairman))
        ctx = None
        if resolve is not None:
            try:
                ctx, problem = resolve()
            except Exception as e:
                ctx, problem = None, f"{type(e).__name__}: {str(e)[:200]}"
            if problem:
                post(f":warning: {problem} Answering without it.")
            if ctx is not None:
                post(slack_context.context_note(ctx))
        asyncio.run(deliberate(question, post, roster=roster, context=ctx))
    except Exception as e:  # never leave the thread hanging silently
        log.exception("council run failed")
        post(f":x: The council hit an error: `{type(e).__name__}: {str(e)[:300]}`")
    finally:
        slots.release()


# ---------------------------------------------------------------------------
# Slack wiring
# ---------------------------------------------------------------------------

def thread_history(client, channel: str, thread_ts: str, bot_user_id: str,
                   exclude_ts: Optional[str]) -> Optional[List[Dict[str, str]]]:
    """Return earlier turns if this is a War Room thread, else None."""
    msgs = []
    cursor = None
    while True:
        resp = client.conversations_replies(channel=channel, ts=thread_ts, limit=200, cursor=cursor)
        msgs.extend(resp.get("messages", []))
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    is_ours = any(m.get("user") == bot_user_id and (m.get("text") or "").startswith(CONVENING_PREFIX) for m in msgs)
    if not is_ours:
        return None
    history: List[Dict[str, str]] = []
    for m in msgs:
        if m.get("ts") == exclude_ts:
            continue
        text = html.unescape(m.get("text") or "")  # Slack returns &amp; &lt; &gt;
        if m.get("user") == bot_user_id or m.get("bot_id"):
            if SLASH_ROOT_MARK in text:  # root post created by /warroom
                q = text.split(SLASH_ROOT_MARK, 1)[1].strip().lstrip(">").strip()
                if q:
                    history.append({"role": "user", "text": q})
            elif text.startswith(VERDICT_PREFIX):
                body = text.split("\n", 1)[1] if "\n" in text else ""
                if history and history[-1]["role"] == "council":
                    history[-1]["text"] += "\n" + body  # continuation chunk
                else:
                    history.append({"role": "council", "text": body})
        else:
            q = strip_mentions(text)
            if q:
                history.append({"role": "user", "text": q})
    return history


def roster_text(roster: Roster) -> str:
    """Plain-text roster (kept for callers/tests); see slack_roster for blocks."""
    return slack_roster.roster_text(roster)


def run_ping(member, user_id: str, respond) -> None:
    """Ping a member (blocking, in a worker thread) and replace the ephemeral message."""
    try:
        ok, text = asyncio.run(ping_member(member))
    except Exception as e:
        ok, text = False, f"{type(e).__name__}: {e}"
    reply = slack_roster.reply_from_ping(member, ok, text,
                                         can_edit=slack_roster.can_edit_roster(user_id))
    respond(text=reply.text, blocks=reply.blocks, replace_original=True, response_type="ephemeral")


def send_reply(reply, user_id: str, respond, replace: bool) -> None:
    respond(text=reply.text, blocks=reply.blocks, replace_original=replace, response_type="ephemeral")
    if reply.ping is not None:
        threading.Thread(target=run_ping, args=(reply.ping, user_id, respond), daemon=True).start()


def build_app(**app_kwargs):
    from slack_bolt import App

    app = App(token=os.environ["SLACK_BOT_TOKEN"], **app_kwargs)
    slots = threading.BoundedSemaphore(MAX_CONCURRENT)
    store = slack_context.ContextStore()
    slack_context.prune_old_threads()

    def start(client, channel: str, thread_ts: str, question: str,
              files: Optional[List[Dict]] = None) -> None:
        def post(text: str) -> None:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text,
                                    unfurl_links=False, unfurl_media=False)

        def prepare() -> None:
            if not files:
                return
            saved, problems = slack_context.download_files(
                files, slack_context.thread_dir(channel, thread_ts), os.environ["SLACK_BOT_TOKEN"],
                info=lambda fid: (client.files_info(file=fid).get("file") or {}))
            if saved:
                post(f":paperclip: Saved {len(saved)} attachment{'s' if len(saved) != 1 else ''} as "
                     f"context for this thread: " + ", ".join(f"`{n}`" for n in saved))
            for p in problems:
                post(f":warning: Skipped {p}")

        def resolve():
            return slack_context.resolve_context(channel, thread_ts, store)

        threading.Thread(target=run_council_thread, args=(question, post, slots, prepare, resolve),
                         daemon=True).start()

    def with_context(client, event, bot_user_id: str, question: str) -> str:
        thread_ts = event.get("thread_ts")
        if not thread_ts or thread_ts == event.get("ts"):
            return question
        try:
            history = thread_history(client, event["channel"], thread_ts, bot_user_id, event.get("ts"))
        except Exception:
            log.exception("couldn't read thread history; answering without context")
            return question
        return build_followup_question(history or [], question)

    @app.event("app_mention")
    def on_mention(event, client, context):
        bot_user_id = context.get("bot_user_id")
        question = strip_mentions(event.get("text", ""))
        thread_ts = event.get("thread_ts") or event["ts"]
        files = event.get("files") or []
        if not question and files:
            question = DEFAULT_FILES_QUESTION
        if not question:
            client.chat_postMessage(channel=event["channel"], thread_ts=thread_ts,
                                    text="Ask me something, e.g. `@War Room Should we self-host email?`")
            return
        start(client, event["channel"], thread_ts, with_context(client, event, bot_user_id, question), files)

    @app.event("message")
    def on_message(event, client, context):
        # Plain replies inside a War Room thread are follow-ups.
        bot_user_id = context.get("bot_user_id")
        if event.get("subtype") not in (None, "file_share") or event.get("bot_id") \
                or event.get("user") == bot_user_id:
            return
        thread_ts = event.get("thread_ts")
        if not thread_ts or thread_ts == event.get("ts"):
            return
        text = event.get("text") or ""
        if bot_user_id and f"<@{bot_user_id}" in text:
            return  # handled by app_mention
        question = strip_mentions(text)
        files = event.get("files") or []
        if not question and files:
            question = DEFAULT_FILES_QUESTION
        if not question:
            return
        try:
            history = thread_history(client, event["channel"], thread_ts, bot_user_id, event.get("ts"))
        except Exception:
            log.exception("couldn't read thread history")
            return
        if history is None:
            return  # not a War Room thread
        start(client, event["channel"], thread_ts, build_followup_question(history, question), files)

    @app.command("/warroom")
    def on_command(ack, command, client, respond):
        ack()
        parsed = slack_roster.parse_slash_text(command.get("text") or "")
        if parsed.kind == "context":
            text = slack_context.handle_context_command(
                parsed.args[0] if parsed.args else "", user_id=command.get("user_id", ""),
                channel_id=command.get("channel_id", ""), store=store)
            respond(text=text, response_type="ephemeral")
            return
        if parsed.kind != "question":
            reply = slack_roster.dispatch_slash(parsed, user_id=command.get("user_id", ""))
            send_reply(reply, command.get("user_id", ""), respond, replace=False)
            return
        question = parsed.question
        try:
            root = client.chat_postMessage(
                channel=command["channel_id"],
                text=f"<@{command['user_id']}> {SLASH_ROOT_MARK}\n>{question}",
            )
        except Exception as e:
            err = getattr(getattr(e, "response", None), "get", lambda *_: None)("error")
            if err in ("not_in_channel", "channel_not_found"):
                respond("I'm not in this channel yet. Invite me with `/invite @War Room`, then try again.")
            else:
                respond(f"Couldn't start the council: `{err or type(e).__name__}`")
            return
        start(client, command["channel_id"], root["ts"], question)

    import re as _re

    @app.action(_re.compile(r"^warroom_(toggle|chair|remove|test)__\d+$|^warroom_reset$"))
    def on_roster_action(ack, body, action, respond):
        ack()
        user_id = (body.get("user") or {}).get("id", "")
        reply = slack_roster.handle_action(action.get("action_id", ""), action.get("value", ""), user_id)
        send_reply(reply, user_id, respond, replace=True)

    return app


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def _fake_query(member: Member, messages, timeout: float):
    await asyncio.sleep(0.05)
    prompt = messages[0]["content"]
    if "Chairman of an LLM Council" in prompt:
        return {"content": "## Verdict\n\n**Use a hosted provider.** See [Fastmail](https://www.fastmail.com).\n\n- cheaper\n- safer"}
    if "FINAL RANKING" in prompt:
        return {"content": "Response A is thorough but long.\nResponse B is **crisp**.\n\nFINAL RANKING:\n1. Response B\n2. Response A"}
    if member.name == "Grok":
        return None  # exercise the failure path
    return {"content": f"# {member.name} says\n\nSome *thoughts* with `code` & <tags>.\n\n" + ("Long paragraph. " * 300)}


def dry_run(question: str, fake: bool) -> None:
    n = 0

    def post(text: str) -> None:
        nonlocal n
        n += 1
        print(f"\n----- Slack message {n} ({len(text)} chars) -----\n{text}")

    roster = current_roster()
    post(convening_text([m.name for m in roster.enabled], roster.chairman))
    if fake:
        import backend.council as council
        council.query_member = _fake_query  # stage 2/3 helpers look this up at call time
        asyncio.run(deliberate(question, post, query=_fake_query, roster=roster))
    else:
        asyncio.run(deliberate(question, post, roster=roster))
    print(f"\n===== {n} messages would be posted =====")


def main() -> None:
    parser = argparse.ArgumentParser(description="War Room Slack bot (Socket Mode).")
    parser.add_argument("--dry-run", metavar="QUESTION", help="run the council and print the Slack messages instead of connecting")
    parser.add_argument("--fake", action="store_true", help="with --dry-run: use canned member answers (no CLI calls)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.dry_run:
        dry_run(args.dry_run, args.fake)
        return

    missing = [k for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN") if not os.getenv(k)]
    if missing:
        sys.exit(f"Missing {', '.join(missing)}. Add them to {REPO_ROOT / '.env'} (see .env.example and README).")
    if not os.environ["SLACK_BOT_TOKEN"].startswith("xoxb-") or not os.environ["SLACK_APP_TOKEN"].startswith("xapp-"):
        sys.exit("SLACK_BOT_TOKEN must start with xoxb- and SLACK_APP_TOKEN with xapp-.")
    if any(os.environ[k].endswith("...") for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")):
        sys.exit("The Slack tokens in .env are still the .env.example placeholders; paste the real ones.")

    from slack_bolt.adapter.socket_mode import SocketModeHandler

    app = build_app()
    roster = current_roster()
    log.info("War Room connecting via Socket Mode (members: %s; chairman: %s; roster: %s)",
             ", ".join(m.name for m in roster.enabled), roster.chairman,
             config_path() if roster.source == "file" else "env defaults")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
