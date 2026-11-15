"""Council roster: load/save/validation, CLI, and the TUI settings screen (fake mode)."""

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_ROSTER_DIR = tempfile.mkdtemp(prefix="warroom-cc-test-")
os.environ["WARROOM_COUNCIL_CONFIG"] = str(Path(_ROSTER_DIR) / "council.json")

from backend import council_config as cc  # noqa: E402

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


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "council.json"

    def test_env_default_when_no_file(self):
        r = cc.load_roster(self.path)
        self.assertEqual(r.source, "env")
        self.assertGreaterEqual(len(r.enabled), 2)
        self.assertIsNotNone(r.get(r.chairman))

    def test_save_load_roundtrip_with_two_claudes(self):
        r = base_roster()
        r.add(cc.MemberConfig("Opus", "claude", "opus", "pink"))
        r.add(cc.MemberConfig("Sonnet", "claude", "sonnet", "#23c275", enabled=False))
        r.set_chair("Sonnet")  # chair may be a disabled member
        cc.save_roster(r, self.path, check_cli=False, models=models)
        data = json.loads(self.path.read_text())
        self.assertEqual(data["chairman"], "Sonnet")
        loaded = cc.load_roster(self.path)
        self.assertEqual(loaded.source, "file")
        self.assertEqual([m.name for m in loaded.enabled], ["ChatGPT", "Claude", "Grok", "Opus"])
        self.assertEqual(loaded.get("opus").color, "#F93697")
        self.assertEqual(loaded.chair_member().model, "sonnet")
        self.assertEqual(loaded.title_member_obj().name, "Grok")

    def test_validation(self):
        r = base_roster()
        r.set_enabled("ChatGPT", False)
        r.set_enabled("Grok", False)
        errors, _ = cc.validate(r, check_cli=False, models=models)
        self.assertTrue(any("at least 2" in e for e in errors))
        with self.assertRaises(cc.RosterError):
            cc.save_roster(r, self.path, check_cli=False, models=models)
        self.assertFalse(self.path.exists())

        r = base_roster()
        dup = cc.MemberConfig("claude", "claude", "opus", "#F93697")
        self.assertTrue(cc.validate_member(dup, r, check_cli=False, models=models)[0])
        bad_provider = cc.MemberConfig("X", "gemini", None, "#F93697")
        self.assertTrue(cc.validate_member(bad_provider, r, check_cli=False, models=models)[0])
        bad_color = cc.MemberConfig("X", "grok", None, "#123456")
        self.assertTrue(cc.validate_member(bad_color, r, check_cli=False, models=models)[0])
        bad_grok = cc.MemberConfig("X", "grok", "grok-9", "#F93697")
        self.assertIn("grok-9", " ".join(cc.validate_member(bad_grok, r, check_cli=False, models=models)[0]))
        ok_grok = cc.MemberConfig("X", "grok", "grok-4.6", "#F93697")
        self.assertEqual(cc.validate_member(ok_grok, r, check_cli=False, models=models), ([], []))
        odd_claude = cc.MemberConfig("Y", "claude", "mystery", "#F93697")
        e, w = cc.validate_member(odd_claude, r, check_cli=False, models=models)
        self.assertEqual(e, [])
        self.assertTrue(w)  # unknown claude alias is only a warning
        full_claude = cc.MemberConfig("Z", "claude", "claude-opus-4-5", "#F93697")
        self.assertEqual(cc.validate_member(full_claude, r, check_cli=False, models=models), ([], []))
        no_model_or = cc.MemberConfig("OR", "openrouter", None, "#F93697")
        self.assertTrue(cc.validate_member(no_model_or, r, check_cli=False, models=models)[0])

    def test_chair_and_title_rules(self):
        r = base_roster()
        with self.assertRaises(cc.RosterError):
            r.remove("Claude")  # chair
        r.remove("Grok")  # title member -> falls back to chair
        self.assertEqual(r.title_member, "Claude")
        r.chairman = "Nobody"
        errors, _ = cc.validate(r, check_cli=False, models=models)
        self.assertTrue(any("chair" in e.lower() for e in errors))

    def test_broken_file_falls_back(self):
        self.path.write_text("{not json")
        with self.assertRaises(cc.RosterError):
            cc.load_roster(self.path)
        os.environ["WARROOM_COUNCIL_CONFIG"] = str(self.path)
        try:
            self.assertEqual(cc.current_roster().source, "env")
        finally:
            os.environ["WARROOM_COUNCIL_CONFIG"] = str(Path(_ROSTER_DIR) / "council.json")

    def test_move(self):
        r = base_roster()
        r.move("Grok", -1)
        self.assertEqual([m.name for m in r.members], ["ChatGPT", "Grok", "Claude"])

    def test_council_functions_follow_roster(self):
        from backend import council
        r = base_roster()
        r.add(cc.MemberConfig("Opus", "claude", "opus", "pink"))
        r.set_enabled("Grok", False)
        r.set_chair("Opus")
        seen = []

        async def fake(member, messages, timeout):
            seen.append((member.name, member.model))
            if "Chairman of an LLM Council" in messages[0]["content"]:
                return {"content": "verdict"}
            if "FINAL RANKING" in messages[0]["content"]:
                return {"content": "FINAL RANKING:\n1. Response A"}
            return {"content": f"{member.name} answer"}

        orig = council.query_member
        council.query_member = fake
        try:
            s1, s2, s3, _ = asyncio.run(council.run_full_council("Q", roster=r))
        finally:
            council.query_member = orig
        self.assertEqual([x["model"] for x in s1], ["ChatGPT", "Claude", "Opus"])
        self.assertEqual(s3["model"], "Opus")
        self.assertNotIn("Grok", {n for n, _ in seen})
        self.assertIn(("Opus", "opus"), seen)


class SlackRosterTests(unittest.TestCase):
    def test_roster_text(self):
        from backend import slack_bot
        r = base_roster()
        r.add(cc.MemberConfig("Opus", "claude", "opus", "pink", enabled=False))
        r.set_chair("Opus")
        text = slack_bot.roster_text(r)
        self.assertIn("chair: *Opus*", text)
        self.assertIn("`claude:opus`", text)
        self.assertIn(":white_circle: *Opus*", text)
        self.assertIn(":white_check_mark: *Grok*", text)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "council.json"
        os.environ["WARROOM_COUNCIL_CONFIG"] = str(self.path)
        self._models = cc.available_models
        cc.available_models = models  # validate_member's default is bound at def time; patch cache instead
        cc._MODEL_CACHE.update(MODELS)

    def tearDown(self):
        os.environ["WARROOM_COUNCIL_CONFIG"] = str(Path(_ROSTER_DIR) / "council.json")
        cc.available_models = self._models
        cc._MODEL_CACHE.clear()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cc.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_flow(self):
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("environment defaults", out)
        code, out, _ = self.run_cli("add", "Opus", "--provider", "claude", "--model", "opus", "--color", "pink")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.path.exists())
        self.assertIn("Opus", out)
        code, _, _ = self.run_cli("chair", "Opus")
        self.assertEqual(code, 0)
        self.assertEqual(cc.load_roster().chairman, "Opus")
        code, _, err = self.run_cli("remove", "Opus")
        self.assertEqual(code, 2)
        self.assertIn("chair", err)
        self.assertEqual(self.run_cli("disable", "Grok")[0], 0)
        self.assertFalse(cc.load_roster().get("Grok").enabled)
        code, _, err = self.run_cli("disable", "ChatGPT")  # would leave Claude + Opus = 2 -> ok
        self.assertEqual(code, 0, err)
        code, _, err = self.run_cli("disable", "Claude")  # only Opus left -> refused
        self.assertEqual(code, 2)
        self.assertIn("at least 2", err)
        code, _, err = self.run_cli("add", "Bad", "--provider", "grok", "--model", "grok-9")
        self.assertEqual(code, 2)
        self.assertEqual(self.run_cli("reset")[0], 0)
        self.assertFalse(self.path.exists())


class SettingsScreenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path = Path(tempfile.mkdtemp()) / "council.json"
        os.environ["WARROOM_COUNCIL_CONFIG"] = str(self.path)

    async def asyncTearDown(self):
        os.environ["WARROOM_COUNCIL_CONFIG"] = str(Path(_ROSTER_DIR) / "council.json")

    async def test_edit_roster_and_ask(self):
        from textual.widgets import DataTable, Input, Select, Static
        from backend import tui
        from backend.tui_council import CouncilScreen

        app = tui.WarRoomApp(fake=True, fake_delay=0)
        app.council_models = models
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.press("ctrl+e")
            await pilot.pause()
            screen = app.screen
            self.assertIsInstance(screen, CouncilScreen)
            table = screen.query_one("#cs-table", DataTable)
            self.assertEqual(table.row_count, 3)

            # add two Claude models
            for name, model in (("Opus", "opus"), ("Sonnet", "sonnet")):
                screen.query_one("#add-name", Input).value = name
                screen.query_one("#add-provider", Select).value = "claude"
                screen.query_one("#add-model", Input).value = model
                await pilot.click("#btn-add")
                await pilot.pause()
            self.assertEqual(table.row_count, 5)
            self.assertEqual(screen.work.get("Sonnet").model, "sonnet")

            # bad model is refused with a message
            screen.query_one("#add-name", Input).value = "Bad"
            screen.query_one("#add-provider", Select).value = "grok"
            screen.query_one("#add-model", Input).value = "grok-9"
            await pilot.click("#btn-add")
            await pilot.pause()
            self.assertIsNone(screen.work.get("Bad"))
            self.assertIn("grok-9", str(screen.query_one("#cs-msg", Static).render()))

            # select Grok (row 2): disable it, then remove it
            table.focus()
            table.move_cursor(row=2)
            await pilot.press("space")
            self.assertFalse(screen.work.get("Grok").enabled)
            await pilot.press("delete")
            await pilot.pause()
            self.assertIsNone(screen.work.get("Grok"))

            # chair via the radio set: pick Sonnet
            radio = screen.query_one("#chair-set")
            sonnet_idx = [m.name for m in screen.work.members].index("Sonnet")
            radio.query("RadioButton")[sonnet_idx].value = True
            await pilot.pause()
            self.assertEqual(screen.work.chairman, "Sonnet")
            # disable Sonnet: a chair doesn't have to sit on the council
            table.focus()
            table.move_cursor(row=sonnet_idx)
            await pilot.press("space")

            # quick test in fake mode
            table.move_cursor(row=0)
            await pilot.press("t")
            for _ in range(50):
                await pilot.pause(0.05)
                if "answered" in str(screen.query_one("#cs-msg", Static).render()):
                    break
            self.assertIn("ChatGPT answered", str(screen.query_one("#cs-msg", Static).render()))

            # can't save with fewer than 2 enabled
            for name in ("ChatGPT", "Claude"):
                screen.work.get(name).enabled = False
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertIs(app.screen, screen)
            self.assertIn("at least 2", str(screen.query_one("#cs-msg", Static).render()))
            for name in ("ChatGPT", "Claude"):
                screen.work.get(name).enabled = True

            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, CouncilScreen)
            saved = cc.load_roster(self.path)
            self.assertEqual([m.name for m in saved.enabled], ["ChatGPT", "Claude", "Opus"])
            self.assertEqual(saved.chairman, "Sonnet")
            self.assertIn("chair: Sonnet", app.sub_title)
            self.assertIn("Opus", str(app.chat_screen.query_one("#roster-bar", Static).render()))

            # next question uses the new roster: tabs follow it
            app.chat_screen.query_one("#ask", Input).value = "Tabs or spaces?"
            await pilot.press("enter")
            for _ in range(200):
                await pilot.pause(0.05)
                if not app.running:
                    break
            turn = app.chat_screen.query_one("#turn-1")
            self.assertEqual([m.name for m in turn.members], ["ChatGPT", "Claude", "Opus"])
            self.assertEqual(turn.chair_name, "Sonnet")
            turn.query_one(f"#{turn.pane_id('Opus')}")
            self.assertEqual(app.turns[0]["result"]["stage3"]["model"], "Sonnet")

    async def test_cancel_discards(self):
        from backend import tui
        app = tui.WarRoomApp(fake=True, fake_delay=0)
        app.council_models = models
        async with app.run_test(size=(150, 50)) as pilot:
            await pilot.press("ctrl+e")
            await pilot.pause()
            app.screen.work.get("Grok").enabled = False
            await pilot.press("escape")
            await pilot.pause()
            self.assertTrue(app.roster.get("Grok").enabled)
            self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
