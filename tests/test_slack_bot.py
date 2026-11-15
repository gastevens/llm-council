"""Slack wiring checks that need no real tokens or network."""

import os
import unittest

from backend import slack_bot
from backend.slack_format import VERDICT_PREFIX

BOT = "UBOT"


class FakeClient:
    def __init__(self, messages):
        self.messages = messages

    def conversations_replies(self, channel, ts, limit, cursor=None):
        return {"messages": self.messages, "response_metadata": {"next_cursor": ""}}


class ThreadHistoryTests(unittest.TestCase):
    def test_not_war_room_thread(self):
        c = FakeClient([{"ts": "1", "user": "U1", "text": "hello"}, {"ts": "2", "user": "U2", "text": "hi"}])
        self.assertIsNone(slack_bot.thread_history(c, "C", "1", BOT, "2"))

    def test_war_room_thread(self):
        c = FakeClient([
            {"ts": "1", "user": "U1", "text": f"<@{BOT}> Should we self-host email?"},
            {"ts": "2", "user": BOT, "text": slack_bot.CONVENING_PREFIX + "… blah"},
            {"ts": "3", "user": BOT, "text": ":large_green_circle: *Stage 1 · ChatGPT*\nNo."},
            {"ts": "4", "user": BOT, "text": f"{VERDICT_PREFIX} (Claude) _(1/2)_\nUse Fastmail."},
            {"ts": "5", "user": BOT, "text": f"{VERDICT_PREFIX} (Claude) _(cont. 2/2)_\nSeriously."},
            {"ts": "6", "user": "U1", "text": "What about cost?"},
        ])
        h = slack_bot.thread_history(c, "C", "1", BOT, "6")
        self.assertEqual(h, [
            {"role": "user", "text": "Should we self-host email?"},
            {"role": "council", "text": "Use Fastmail.\nSeriously."},
        ])

    def test_slash_root_is_parsed(self):
        c = FakeClient([
            {"ts": "1", "user": BOT, "text": "<@U1> asked the war room:\n&gt;Pick a CRM &amp; tools"},
            {"ts": "2", "user": BOT, "text": slack_bot.CONVENING_PREFIX + "…"},
        ])
        h = slack_bot.thread_history(c, "C", "1", BOT, None)
        self.assertEqual(h, [{"role": "user", "text": "Pick a CRM & tools"}])


class AppWiringTests(unittest.TestCase):
    def test_build_app_registers_listeners(self):
        os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-not-real")
        try:
            app = slack_bot.build_app(token_verification_enabled=False)
        finally:
            if os.environ.get("SLACK_BOT_TOKEN") == "xoxb-test-not-real":
                del os.environ["SLACK_BOT_TOKEN"]
        self.assertEqual(len(app._listeners), 3)


if __name__ == "__main__":
    unittest.main()
