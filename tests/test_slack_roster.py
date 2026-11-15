"""Slack roster commands and Block Kit — no tokens or network."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ROSTER_DIR = tempfile.mkdtemp(prefix="warroom-slack-roster-")
os.environ["WARROOM_COUNCIL_CONFIG"] = str(Path(_ROSTER_DIR) / "council.json")
os.environ.pop("WARROOM_ADMINS", None)

from backend import council_config as cc  # noqa: E402
from backend.slack_roster import (  # noqa: E402
    DENIED,
    SlashParse,
    admin_allowlist,
    can_edit_roster,
    dispatch_slash,
    handle_action,
    models_text,
    parse_slash_text,
    reply_from_ping,
    roster_blocks,
    roster_text,
)

MODELS = {"codex": ["gpt-6.1-sol", "gpt-5.6-luna"], "grok": ["grok-4.7", "grok-4.6"],
          "claude": list(cc.CLAUDE_ALIASES)}


def models(provider):
    return MODELS.get(provider)


def base_roster():
    return cc.Roster(members=[
        cc.MemberConfig("ChatGPT", "codex", None, "#20B8A4"),
        cc.MemberConfig("Claude", "claude", None, "#F86522"),
        cc.MemberConfig("Grok", "grok", None, "#1788F8"),
    ], chairman="Claude", title_member="Grok")


class ParseTests(unittest.TestCase):
    def test_empty_is_usage(self):
        self.assertEqual(parse_slash_text("").kind, "usage")
        self.assertEqual(parse_slash_text("   ").kind, "usage")

    def test_exact_no_arg_commands(self):
        self.assertEqual(parse_slash_text("roster").kind, "roster")
        self.assertEqual(parse_slash_text("HELP").kind, "help")
        self.assertEqual(parse_slash_text("reset").kind, "reset")
        self.assertEqual(parse_slash_text("reset confirm").args, ["confirm"])

    def test_help_sentence_is_a_question(self):
        p = parse_slash_text("help me choose a vendor")
        self.assertEqual(p.kind, "question")
        self.assertEqual(p.question, "help me choose a vendor")

    def test_models(self):
        self.assertEqual(parse_slash_text("models").kind, "models")
        self.assertEqual(parse_slash_text("models claude").args, ["claude"])
        self.assertEqual(parse_slash_text("models of the universe").kind, "question")

    def test_add(self):
        p = parse_slash_text("add Opus claude opus pink")
        self.assertEqual(p.kind, "add")
        self.assertEqual(p.args, ["Opus", "claude", "opus", "pink"])
        p = parse_slash_text('add "Fast Claude" claude sonnet')
        self.assertEqual(p.args[:2], ["Fast Claude", "claude"])
        # incomplete add still parses as add (handler shows usage)
        self.assertEqual(parse_slash_text("add Opus").kind, "add")
        # "add a new CRM?" — provider isn't known, so it's a question
        self.assertEqual(parse_slash_text("add a new CRM?").kind, "question")

    def test_name_commands_need_one_arg(self):
        self.assertEqual(parse_slash_text("chair Claude").kind, "chair")
        self.assertEqual(parse_slash_text("disable Grok").args, ["Grok"])
        self.assertEqual(parse_slash_text("test").kind, "test")
        self.assertEqual(parse_slash_text("test the new policy").kind, "question")
        self.assertEqual(parse_slash_text("Should we self-host email?").kind, "question")


class AdminTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("WARROOM_ADMINS", None)

    def test_unset_means_anyone(self):
        os.environ.pop("WARROOM_ADMINS", None)
        self.assertIsNone(admin_allowlist())
        self.assertTrue(can_edit_roster("U123"))
        os.environ["WARROOM_ADMINS"] = "  "
        self.assertTrue(can_edit_roster("U123"))

    def test_allowlist(self):
        os.environ["WARROOM_ADMINS"] = "U111, U222"
        self.assertEqual(admin_allowlist(), {"U111", "U222"})
        self.assertTrue(can_edit_roster("U111"))
        self.assertFalse(can_edit_roster("U999"))
        self.assertFalse(can_edit_roster(""))


class RosterCommandTests(unittest.TestCase):
    def setUp(self):
        self._old_cfg = os.environ.get("WARROOM_COUNCIL_CONFIG")
        self._old_admins = os.environ.get("WARROOM_ADMINS")
        self.path = Path(tempfile.mkdtemp()) / "council.json"
        os.environ["WARROOM_COUNCIL_CONFIG"] = str(self.path)
        os.environ.pop("WARROOM_ADMINS", None)
        cc.save_roster(base_roster(), self.path, check_cli=False, models=models)
        self._models = cc.available_models
        cc.available_models = models
        cc._MODEL_CACHE.update(MODELS)

    def tearDown(self):
        if self._old_cfg is None:
            os.environ.pop("WARROOM_COUNCIL_CONFIG", None)
        else:
            os.environ["WARROOM_COUNCIL_CONFIG"] = self._old_cfg
        if self._old_admins is None:
            os.environ.pop("WARROOM_ADMINS", None)
        else:
            os.environ["WARROOM_ADMINS"] = self._old_admins
        cc.available_models = self._models
        cc._MODEL_CACHE.clear()

    def dispatch(self, text, user="U1"):
        return dispatch_slash(parse_slash_text(text), user_id=user,
                              check_cli=False, models=models)

    def test_roster_text_and_blocks(self):
        r = cc.load_roster(self.path)
        text = roster_text(r)
        self.assertIn("chair: *Claude*", text)
        self.assertIn(":white_check_mark: *Grok*", text)
        blocks = roster_blocks(r, can_edit=True)
        action_ids = [el["action_id"]
                      for b in blocks if b.get("type") == "actions"
                      for el in b["elements"]]
        self.assertIn("warroom_toggle__0", action_ids)
        self.assertIn("warroom_test__0", action_ids)
        self.assertIn("warroom_reset", action_ids)
        # chair (Claude, index 1) has no Set chair / Remove buttons
        self.assertNotIn("warroom_chair__1", action_ids)
        self.assertNotIn("warroom_remove__1", action_ids)
        self.assertIn("warroom_chair__0", action_ids)
        self.assertIn("warroom_remove__2", action_ids)
        # view-only has no buttons
        view = roster_blocks(r, can_edit=False)
        self.assertFalse(any(b.get("type") == "actions" for b in view))
        self.assertIn("View only", json.dumps(view))

    def test_add_enable_chair_remove(self):
        reply = self.dispatch("add Opus claude opus pink")
        self.assertTrue(reply.ok, reply.text)
        self.assertIn("Added *Opus*", reply.text)
        self.assertIn("`claude:opus`", reply.text)
        loaded = cc.load_roster(self.path)
        self.assertIsNotNone(loaded.get("Opus"))
        self.assertEqual(loaded.get("Opus").color, "#F93697")

        reply = self.dispatch("disable Opus")
        self.assertTrue(reply.ok, reply.text)
        self.assertFalse(cc.load_roster(self.path).get("Opus").enabled)

        reply = self.dispatch("chair Opus")
        self.assertTrue(reply.ok, reply.text)
        self.assertEqual(cc.load_roster(self.path).chairman, "Opus")

        # can't remove the chair
        reply = self.dispatch("remove Opus")
        self.assertFalse(reply.ok)
        self.assertIn("chair", reply.text.lower())

        reply = self.dispatch("chair Claude")
        self.assertTrue(reply.ok)
        reply = self.dispatch("remove Opus")
        self.assertTrue(reply.ok, reply.text)
        self.assertIsNone(cc.load_roster(self.path).get("Opus"))

    def test_add_quoted_name_and_default_color(self):
        reply = self.dispatch('add "Fast Claude" claude sonnet')
        self.assertTrue(reply.ok, reply.text)
        m = cc.load_roster(self.path).get("Fast Claude")
        self.assertEqual(m.model, "sonnet")
        self.assertTrue(m.color.startswith("#"))

    def test_duplicate_name_and_bad_provider_rejected(self):
        reply = self.dispatch("add Claude claude opus")
        self.assertFalse(reply.ok)
        self.assertIn("already a member", reply.text)
        # parse treats unknown provider as a question; dispatch isn't used.
        # An add with a bad model is rejected:
        reply = self.dispatch("add Nova grok grok-9")
        self.assertFalse(reply.ok)
        self.assertIn("grok-9", reply.text)

    def test_disable_below_minimum_is_rejected(self):
        self.dispatch("disable Grok")
        reply = self.dispatch("disable ChatGPT")
        self.assertFalse(reply.ok)
        self.assertIn("at least 2", reply.text)
        # file unchanged: Grok still off, ChatGPT still on
        r = cc.load_roster(self.path)
        self.assertTrue(r.get("ChatGPT").enabled)
        self.assertFalse(r.get("Grok").enabled)

    def test_title_and_models_and_help(self):
        reply = self.dispatch("title ChatGPT")
        self.assertTrue(reply.ok, reply.text)
        self.assertEqual(cc.load_roster(self.path).title_member, "ChatGPT")
        reply = self.dispatch("models claude")
        self.assertIn("`opus`", reply.text)
        reply = self.dispatch("help")
        self.assertIn("/warroom add", reply.text)
        reply = dispatch_slash(SlashParse("usage"), user_id="U1", check_cli=False, models=models)
        self.assertIn("Usage:", reply.text)

    def test_test_returns_pending_ping(self):
        reply = self.dispatch("test Grok")
        self.assertTrue(reply.ok)
        self.assertIsNotNone(reply.ping)
        self.assertEqual(reply.ping.name, "Grok")
        self.assertIn("Pinging *Grok*", reply.text)
        reply = self.dispatch("test Nobody")
        self.assertFalse(reply.ok)
        self.assertIsNone(reply.ping)

    def test_reset_asks_then_confirm_deletes(self):
        reply = self.dispatch("reset")
        self.assertTrue(self.path.exists())
        self.assertIn("warroom_reset", json.dumps(reply.blocks))
        reply = self.dispatch("reset confirm")
        self.assertTrue(reply.ok)
        self.assertFalse(self.path.exists())

    def test_admin_allowlist_blocks_edits_not_views(self):
        os.environ["WARROOM_ADMINS"] = "UADMIN"
        view = self.dispatch("roster", user="UPEON")
        self.assertTrue(view.ok)
        self.assertIn("View only", view.text)
        self.assertFalse(any(b.get("type") == "actions" for b in view.blocks))
        denied = self.dispatch("add Opus claude opus", user="UPEON")
        self.assertFalse(denied.ok)
        self.assertIn("admins", denied.text.lower())
        self.assertIsNone(cc.load_roster(self.path).get("Opus"))
        ok = self.dispatch("add Opus claude opus", user="UADMIN")
        self.assertTrue(ok.ok, ok.text)
        self.assertIsNotNone(cc.load_roster(self.path).get("Opus"))

    def test_buttons_toggle_chair_remove_reset(self):
        r = handle_action("warroom_toggle__2", "Grok", "U1", check_cli=False, models=models)
        self.assertTrue(r.ok, r.text)
        self.assertFalse(cc.load_roster(self.path).get("Grok").enabled)
        # toggle back
        r = handle_action("warroom_toggle__2", "Grok", "U1", check_cli=False, models=models)
        self.assertTrue(cc.load_roster(self.path).get("Grok").enabled)

        r = handle_action("warroom_chair__0", "ChatGPT", "U1", check_cli=False, models=models)
        self.assertTrue(r.ok, r.text)
        self.assertEqual(cc.load_roster(self.path).chairman, "ChatGPT")

        r = handle_action("warroom_remove__2", "Grok", "U1", check_cli=False, models=models)
        # MIN_ENABLED is 2, removing Grok leaves ChatGPT+Claude = ok
        self.assertTrue(r.ok, r.text)
        self.assertIsNone(cc.load_roster(self.path).get("Grok"))

        r = handle_action("warroom_test__0", "ChatGPT", "U1", check_cli=False, models=models)
        self.assertTrue(r.ok)
        self.assertEqual(r.ping.name, "ChatGPT")

        r = handle_action("warroom_reset", "reset", "U1", check_cli=False, models=models)
        self.assertTrue(r.ok)
        self.assertFalse(self.path.exists())

    def test_button_denied_for_non_admin(self):
        os.environ["WARROOM_ADMINS"] = "UADMIN"
        r = handle_action("warroom_toggle__0", "ChatGPT", "UPEON", check_cli=False, models=models)
        self.assertFalse(r.ok)
        self.assertIn("admins", r.text.lower())
        self.assertTrue(cc.load_roster(self.path).get("ChatGPT").enabled)

    def test_reply_from_ping(self):
        member = cc.load_roster(self.path).get("Claude").to_member()
        ok = reply_from_ping(member, True, "OK", can_edit=True)
        self.assertIn("*OK*", ok.text)
        self.assertIn("Claude", ok.text)
        fail = reply_from_ping(member, False, "timeout " * 80, can_edit=True)
        self.assertIn("*FAILED*", fail.text)
        self.assertLessEqual(len(fail.text), 2000)
        self.assertTrue(fail.text.count("```") >= 2)

    def test_models_text_openrouter(self):
        text = models_text("openrouter")
        self.assertIn("OPENROUTER_API_KEY", text)


class SlackBotWiringTests(unittest.TestCase):
    """Invoke the Bolt command/action handlers with a fake client."""

    def setUp(self):
        self._old_cfg = os.environ.get("WARROOM_COUNCIL_CONFIG")
        self._old_token = os.environ.get("SLACK_BOT_TOKEN")
        self.path = Path(tempfile.mkdtemp()) / "council.json"
        os.environ["WARROOM_COUNCIL_CONFIG"] = str(self.path)
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-not-real"
        os.environ.pop("WARROOM_ADMINS", None)
        cc.save_roster(base_roster(), self.path, check_cli=False, models=models)
        cc._MODEL_CACHE.update(MODELS)

    def tearDown(self):
        if self._old_cfg is None:
            os.environ.pop("WARROOM_COUNCIL_CONFIG", None)
        else:
            os.environ["WARROOM_COUNCIL_CONFIG"] = self._old_cfg
        if self._old_token is None:
            os.environ.pop("SLACK_BOT_TOKEN", None)
        else:
            os.environ["SLACK_BOT_TOKEN"] = self._old_token
        cc._MODEL_CACHE.clear()

    def test_command_and_action_handlers(self):
        from backend import slack_bot
        app = slack_bot.build_app(token_verification_enabled=False)
        self.assertGreaterEqual(len(app._listeners), 4)

        responses, acked = [], []

        def ack():
            acked.append(True)

        def respond(*args, **kwargs):
            if args and not kwargs:
                kwargs = {"text": args[0]}
            responses.append(kwargs)

        # Find the /warroom listener callable.
        on_command = None
        on_action = None
        for listeners in app._listeners.values() if isinstance(app._listeners, dict) else [app._listeners]:
            group = listeners if isinstance(listeners, list) else [listeners]
            for item in group:
                fn = getattr(item, "ack_function", None) or getattr(item, "func", None) or item
                name = getattr(fn, "__name__", "")
                if name == "on_command":
                    on_command = fn
                elif name == "on_roster_action":
                    on_action = fn
        # Bolt may wrap listeners; fall back to matching registered commands/actions.
        if on_command is None or on_action is None:
            # Inspect listener matches more loosely.
            names = []
            def walk(obj, depth=0):
                if depth > 4:
                    return
                if callable(obj) and getattr(obj, "__name__", "") in ("on_command", "on_roster_action"):
                    names.append(obj)
                if isinstance(obj, (list, tuple)):
                    for x in obj:
                        walk(x, depth + 1)
                elif isinstance(obj, dict):
                    for x in obj.values():
                        walk(x, depth + 1)
                else:
                    for attr in ("ack_function", "lazy_functions", "func", "listener", "fn"):
                        if hasattr(obj, attr):
                            walk(getattr(obj, attr), depth + 1)
            walk(app._listeners)
            for fn in names:
                if fn.__name__ == "on_command":
                    on_command = fn
                elif fn.__name__ == "on_roster_action":
                    on_action = fn
        self.assertIsNotNone(on_command, "couldn't find on_command listener")

        class FakeClient:
            def __init__(self):
                self.posts = []

            def chat_postMessage(self, **kwargs):
                self.posts.append(kwargs)
                return {"ts": "111.222", "channel": kwargs["channel"]}

        client = FakeClient()
        with patch("backend.slack_roster.dispatch_slash", wraps=lambda parsed, user_id, **k: dispatch_slash(
                parsed, user_id=user_id, check_cli=False, models=models)):
            on_command(ack=ack, command={"text": "roster", "user_id": "U1", "channel_id": "C1"},
                       client=client, respond=respond)
        self.assertTrue(acked)
        self.assertTrue(responses)
        self.assertIn("chair: *Claude*", responses[0]["text"])
        self.assertTrue(responses[0].get("blocks"))
        self.assertEqual(client.posts, [])  # roster is ephemeral, not a channel post

        # A real question should post to the channel (don't actually run the council).
        acked.clear(); responses.clear()
        with patch("backend.slack_bot.run_council_thread"):
            on_command(ack=ack, command={"text": "Should we self-host email?", "user_id": "U1",
                                         "channel_id": "C1"},
                       client=client, respond=respond)
        self.assertEqual(len(client.posts), 1)
        self.assertIn("Should we self-host email?", client.posts[0]["text"])

        if on_action is not None:
            acked.clear(); responses.clear()
            with patch("backend.slack_roster.handle_action", wraps=lambda action_id, value, user_id, **k: handle_action(
                    action_id, value, user_id, check_cli=False, models=models)):
                on_action(ack=ack, body={"user": {"id": "U1"}},
                          action={"action_id": "warroom_chair__0", "value": "ChatGPT"},
                          respond=respond)
            self.assertTrue(acked)
            self.assertIn("Chair is now *ChatGPT*", responses[0]["text"])
            self.assertEqual(cc.load_roster(self.path).chairman, "ChatGPT")


if __name__ == "__main__":
    unittest.main()
