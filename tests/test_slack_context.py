"""Slack context folders: parsing, permissions, allowed roots, attachments — no network."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_DIR = tempfile.mkdtemp(prefix="warroom-slack-ctx-")
os.environ["WARROOM_COUNCIL_CONFIG"] = str(Path(_DIR) / "council.json")
os.environ["WARROOM_FILES_DIR"] = str(Path(_DIR) / "files")
os.environ.pop("WARROOM_ADMINS", None)

from backend import slack_context as sc  # noqa: E402
from backend.slack_roster import parse_slash_text  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_slash_parse(self):
        self.assertEqual(parse_slash_text("context").kind, "context")
        self.assertEqual(parse_slash_text("context off").args, ["off"])
        self.assertEqual(parse_slash_text("context ~/Documents/My Plan").args, ["~/Documents/My Plan"])
        self.assertEqual(parse_slash_text("context /tmp/x --folder").args, ["/tmp/x --folder"])
        # prose stays a question
        q = parse_slash_text("context matters more than speed, right?")
        self.assertEqual(q.kind, "question")

    def test_context_args(self):
        self.assertEqual(sc.parse_context_args(""), ("show", None, None))
        self.assertEqual(sc.parse_context_args("OFF"), ("off", None, None))
        self.assertEqual(sc.parse_context_args("~/a b --inline"), ("set", "~/a b", "inline"))
        self.assertEqual(sc.parse_context_args('"~/x y"'), ("set", "~/x y", None))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="ctx-home-")).resolve()
        self.addCleanup(shutil.rmtree, self.home, True)
        (self.home / "Docs").mkdir()
        (self.home / "Docs" / "plan.md").write_text("Codename HERON\n")
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh" / "id").write_text("secret\n")
        self.store = sc.ContextStore(self.home / "store.json")
        self.env = patch.dict(os.environ, {"WARROOM_CONTEXT_ROOTS": str(self.home)})
        self.env.start()
        os.environ.pop("WARROOM_ADMINS", None)  # .env may set it; restored by patch.dict
        self.addCleanup(self.env.stop)

    def cmd(self, rest, user="U1", channel="C1"):
        return sc.handle_context_command(rest, user_id=user, channel_id=channel, store=self.store)

    def test_set_show_off(self):
        self.assertIn("No context folder", self.cmd(""))
        out = self.cmd(str(self.home / "Docs"))
        self.assertIn("Context set", out)
        self.assertEqual(self.store.get("C1")["path"], str(self.home / "Docs"))
        self.assertIsNone(self.store.get("C2"))  # per channel
        self.assertIn("1 file", self.cmd(""))
        self.assertIn("removed", self.cmd("off"))
        self.assertIsNone(self.store.get("C1"))

    def test_rejects_outside_roots_hidden_and_missing(self):
        outside = Path(tempfile.mkdtemp(prefix="ctx-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "a.txt").write_text("x\n")
        self.assertIn("outside the allowed", self.cmd(str(outside)))
        self.assertIn("Hidden folders", self.cmd(str(self.home / ".ssh")))
        self.assertIn("doesn't exist", self.cmd(str(self.home / "nope")))
        # sneaking out with .. still resolves outside
        self.assertIn("outside the allowed", self.cmd(str(self.home / "Docs" / ".." / ".." )))
        self.assertIsNone(self.store.get("C1"))

    def test_admins_only_when_set(self):
        with patch.dict(os.environ, {"WARROOM_ADMINS": "UADMIN"}):
            self.assertIn("Only War Room admins", self.cmd(str(self.home / "Docs"), user="U2"))
            self.assertIn("Only War Room admins", self.cmd("off", user="U2"))
            self.assertIn("No context folder", self.cmd("", user="U2"))  # viewing is fine
            self.assertIn("Context set", self.cmd(str(self.home / "Docs"), user="UADMIN"))

    def test_resolve_prefers_thread_attachments(self):
        self.cmd(str(self.home / "Docs"))
        ctx, problem = sc.resolve_context("C1", "111.1", self.store)
        self.assertIsNone(problem)
        self.assertEqual(ctx.root, self.home / "Docs")
        d = sc.thread_dir("C1", "111.1")
        d.mkdir(parents=True)
        (d / "upload.txt").write_text("attached\n")
        ctx, _ = sc.resolve_context("C1", "111.1", self.store)
        self.assertEqual(ctx.label, "1 attached file")
        self.assertIn("Context:", sc.context_note(ctx))
        # roots re-checked at use time
        with patch.dict(os.environ, {"WARROOM_CONTEXT_ROOTS": "/nonexistent-root"}):
            ctx, problem = sc.resolve_context("C1", "222.2", self.store)
        self.assertIsNone(ctx)
        self.assertIn("outside", problem)


class DownloadTests(unittest.TestCase):
    def test_download_and_limits(self):
        dest = Path(tempfile.mkdtemp(prefix="ctx-dl-")) / "t"
        self.addCleanup(shutil.rmtree, dest.parent, True)
        fetched = []

        def fetch(url, token):
            fetched.append((url, token))
            return b"<!DOCTYPE html><html>login" if "login" in url else b"hello"

        files = [
            {"name": "../../evil name.txt", "url_private_download": "https://files/1", "size": 5},
            {"name": "evil name.txt", "url_private_download": "https://files/2", "size": 5},
            {"name": "big.bin", "url_private_download": "https://files/3", "size": 10 ** 9},
            {"name": "gdoc", "mode": "external"},
            {"name": "page.txt", "url_private_download": "https://login/4", "size": 5},
            {"id": "F5", "name": "lazy.md"},
        ]
        saved, problems = sc.download_files(files, dest, "xoxb-test", fetch=fetch,
                                            info=lambda fid: {"url_private_download": "https://files/5"})
        self.assertEqual(saved, ["evil name.txt", "evil name (2).txt", "lazy.md"])
        self.assertTrue((dest / "evil name.txt").exists())
        self.assertFalse(any(".." in p.name for p in dest.iterdir()))
        self.assertEqual(len(problems), 3)
        self.assertTrue(all(tok == "xoxb-test" for _, tok in fetched))


if __name__ == "__main__":
    unittest.main()
