"""War Room TUI checks in --fake mode (no CLI calls)."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

# Hermetic roster: env defaults (ChatGPT, Claude, Grok; chair Claude) unless a test writes one.
_ROSTER_DIR = tempfile.mkdtemp(prefix="warroom-test-")
os.environ["WARROOM_COUNCIL_CONFIG"] = str(Path(_ROSTER_DIR) / "council.json")

from textual.widgets import Collapsible, DataTable, Input, Markdown

from backend import tui
from backend.council_config import current_roster

ROSTER = current_roster()
COUNCIL_MEMBERS = ROSTER.active_members()
CHAIR = ROSTER.chairman


class Recorder(tui.Events):
    def __init__(self):
        self.calls = []

    def member_started(self, stage, name):
        self.calls.append(("start", stage, name))

    def member_done(self, stage, name, text, secs, error):
        self.calls.append(("done", stage, name, bool(text), error))

    def stage2_done(self, result):
        self.calls.append(("stage2",))

    def stage3_done(self, result):
        self.calls.append(("stage3",))

    def failed(self, message):
        self.calls.append(("failed", message))


class EngineTests(unittest.TestCase):
    def test_all_stages_and_failure(self):
        ev = Recorder()
        r = asyncio.run(tui.run_council("Q?", ev, tui.make_fake_query(0, fail=("Grok",))))
        self.assertIsNone(r["answers"]["Grok"])
        self.assertIn("simulated failure", r["errors"]["Grok"])
        self.assertEqual({s["model"] for s in r["stage1"]}, {"ChatGPT", "Claude"})
        self.assertTrue(r["stage2"])
        self.assertIn("Use a hosted provider", r["stage3"]["response"])
        self.assertIn(("done", 1, "Grok", False, r["errors"]["Grok"]), ev.calls)
        self.assertIn(("start", 3, CHAIR), ev.calls)
        self.assertLess(ev.calls.index(("stage2",)), ev.calls.index(("stage3",)))

    def test_everyone_fails(self):
        ev = Recorder()
        r = asyncio.run(tui.run_council("Q?", ev, tui.make_fake_query(0, fail=[m.name for m in COUNCIL_MEMBERS])))
        self.assertIsNone(r["stage3"])
        self.assertEqual(ev.calls[-1][0], "failed")


class ContextCommandParseTests(unittest.TestCase):
    def test_parse(self):
        p = tui.parse_context_command
        self.assertIsNone(p("What is context?"))
        self.assertIsNone(p("/contextual"))
        self.assertEqual(p("/context"), {"action": "show"})
        self.assertEqual(p("/context off"), {"action": "off"})
        self.assertEqual(p("/context ~/My Docs/plan"), {"action": "set", "path": "~/My Docs/plan", "mode": None})
        self.assertEqual(p('/context "~/a b" --folder'), {"action": "set", "path": "~/a b", "mode": "folder"})
        self.assertEqual(p("/context --inline ./x"), {"action": "set", "path": "./x", "mode": "inline"})
        self.assertEqual(p("/context --folder")["action"], "error")


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def ask(self, app, pilot, question):
        app.query_one("#ask", Input).value = question
        await pilot.press("enter")
        await pilot.pause()
        for _ in range(400):
            if not app.running:
                break
            await pilot.pause(0.05)
        self.assertFalse(app.running, "council never finished")
        await pilot.pause(0.1)

    async def test_question_followup_save_new_session(self):
        app = tui.WarRoomApp(fake=True, fake_delay=0.05, fake_fail=("Grok",))
        async with app.run_test(size=(140, 60)) as pilot:
            await self.ask(app, pilot, "Should we self-host email?")
            turn = app.query_one("#turn-1")
            # stage 1: one tab per member, failure shown gracefully
            for m in COUNCIL_MEMBERS:
                md = turn.query_one(f"#md-{turn.pane_id(m.name)}", Markdown)
                if m.name == "Grok":
                    self.assertIn("failed to answer", md.source)
                    self.assertEqual(turn.badges["Grok"].badge_state, "failed")
                else:
                    self.assertIn(f"{m.name} says", md.source)
                    self.assertEqual(turn.badges[m.name].badge_state, "done")
            # stage 2: rankings table + collapsible critiques
            table = turn.query_one(DataTable)
            self.assertGreaterEqual(table.row_count, 2)
            self.assertEqual(len(turn.query(Collapsible)), len(app.turns[0]["result"]["stage2"]))
            # stage 3: verdict
            self.assertIn("Use a hosted provider", turn.verdict.source)
            self.assertEqual(turn.chair.badge_state, "done")

            # follow-up carries the earlier question and verdict
            await self.ask(app, pilot, "What about cost?")
            prompt = app.turns[1]["prompt"]
            self.assertIn("Should we self-host email?", prompt)
            self.assertIn("Council verdict", prompt)
            self.assertIn("Follow-up question: What about cost?", prompt)
            self.assertTrue(app.query_one("#turn-2").followup)

            # ctrl+s saves a markdown transcript
            with tempfile.TemporaryDirectory() as d:
                os.environ["WARROOM_TRANSCRIPT_DIR"] = str(Path(d) / "War Room")
                try:
                    await pilot.press("ctrl+s")
                    await pilot.pause()
                    files = list((Path(d) / "War Room").glob("*.md"))
                    self.assertEqual(len(files), 1)
                    text = files[0].read_text()
                    self.assertIn("What about cost?", text)
                    self.assertIn("Chairman's verdict", text)
                    self.assertIn("Use a hosted provider", text)
                finally:
                    del os.environ["WARROOM_TRANSCRIPT_DIR"]

            # ctrl+n clears the session
            await pilot.press("ctrl+n")
            await pilot.pause()
            self.assertEqual(app.history, [])
            self.assertEqual(len(app.query(".turn")), 0)

    async def test_ask_on_open(self):
        app = tui.WarRoomApp(fake=True, fake_delay=0, ask="Tabs or spaces?")
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(200):
                await pilot.pause(0.05)
                if app.turns and not app.running:
                    break
            self.assertEqual(app.turns[0]["question"], "Tabs or spaces?")
            self.assertTrue((app.turns[0]["result"] or {}).get("stage3"))

    async def test_context_command_and_transcript(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "plan.md").write_text("Codename HERON.\n")
            (Path(d) / "budget.txt").write_text("42 dollars\n")
            seen = []
            app = tui.WarRoomApp(fake=True, fake_delay=0, launch_cwd=d)
            base_query = app.query_fn

            async def spy(member, messages, timeout):
                seen.append(messages[0]["content"])
                return await base_query(member, messages, timeout=timeout)

            app.query_fn = spy
            async with app.run_test(size=(160, 50)) as pilot:
                app.query_one("#ask", Input).value = "/context ."
                await pilot.press("enter")
                for _ in range(100):
                    await pilot.pause(0.05)
                    if app.context is not None:
                        break
                self.assertIsNotNone(app.context)
                self.assertEqual(app.context.mode, "inline")
                bar = str(app.query_one("#roster-bar").render())
                self.assertIn("2 files", bar)
                self.assertIn("inline", bar)
                self.assertEqual(app.turns, [])  # /context is not a question

                await self.ask(app, pilot, "What is the codename?")
                self.assertTrue(any("===== FILE: plan.md" in p and "HERON" in p for p in seen))
                self.assertEqual(app.turns[0]["context"]["files"], ["budget.txt", "plan.md"])
                md = tui.transcript_markdown(app.turns, app.started)
                self.assertIn("Context folder", md)
                self.assertIn("`plan.md`", md)

                app.query_one("#ask", Input).value = "/context /definitely/not/here"
                await pilot.press("enter")
                await pilot.pause(0.3)
                self.assertIsNotNone(app.context)  # bad path keeps the old context

                app.query_one("#ask", Input).value = "/context off"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsNone(app.context)

                app.query_one("#ask", Input).value = f"/context {d} --folder"
                await pilot.press("enter")
                for _ in range(100):
                    await pilot.pause(0.05)
                    if app.context is not None:
                        break
                self.assertEqual(app.context.mode, "folder")
                await pilot.press("ctrl+n")
                await pilot.pause()
                self.assertIsNone(app.context)  # new session clears the context

    async def test_context_on_launch(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.txt").write_text("hello\n")
            app = tui.WarRoomApp(fake=True, fake_delay=0, context=d, ask="Summarize")
            async with app.run_test(size=(120, 40)) as pilot:
                for _ in range(200):
                    await pilot.pause(0.05)
                    if app.turns and not app.running:
                        break
                self.assertIsNotNone(app.context)
                self.assertEqual(app.turns[0]["context"]["files"], ["a.txt"])

    async def test_quit_key(self):
        app = tui.WarRoomApp(fake=True, fake_delay=0)
        async with app.run_test() as pilot:
            await pilot.press("ctrl+c")
            await pilot.pause()
        self.assertFalse(app.is_running)


if __name__ == "__main__":
    unittest.main()
