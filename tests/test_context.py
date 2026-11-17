"""Context folders: building, budget/mode choice, prompt text, provider commands."""

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from backend import context as cx
from backend import providers
from backend.providers import Member

MINI_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 44>>stream
BT /F1 18 Tf 20 100 Td (Quarterly plan PDF) Tj ET
endstream endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
trailer<</Root 1 0 R>>
%%EOF
"""


def make_folder(d: Path) -> Path:
    (d / "README.md").write_text("# Project\nThe codename is HERON.\n")
    (d / "notes").mkdir()
    (d / "notes" / "plan.txt").write_text("Budget: 42 dollars.\n")
    (d / "data.csv").write_text("a,b\n1,2\n")
    (d / ".secret").write_text("hidden\n")
    (d / ".git_like").mkdir()
    (d / ".git_like" / "x.txt").write_text("hidden dir\n")
    (d / "node_modules").mkdir()
    (d / "node_modules" / "lib.js").write_text("module.exports = 1\n")
    (d / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0binary")
    (d / "blob.dat").write_bytes(b"\0\1\2\3" * 100)
    (d / "empty.txt").write_text("")
    (d / "package-lock.json").write_text("{}")
    return d


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ctx-test-"))
        make_folder(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for k in ("COUNCIL_CONTEXT_MODE", "COUNCIL_CONTEXT_MAX_CHARS", "COUNCIL_CONTEXT_MAX_FILES"):
            os.environ.pop(k, None)

    def test_includes_text_and_skips_junk(self):
        ctx = cx.build_context(str(self.tmp))
        paths = [f.path for f in ctx.files]
        self.assertEqual(paths[0], "README.md")  # top-level README first
        self.assertEqual(set(paths), {"README.md", "data.csv", "notes/plan.txt"})
        skipped = dict(ctx.skipped)
        self.assertIn("logo.png", skipped)
        self.assertEqual(skipped["blob.dat"], "binary")
        self.assertEqual(skipped["empty.txt"], "empty")
        self.assertEqual(skipped["package-lock.json"], "lockfile")
        self.assertNotIn(".secret", paths)
        self.assertFalse(any("node_modules" in p for p in paths))
        self.assertEqual(ctx.mode, "inline")
        self.assertIn("3 files", ctx.summary())

    def test_respects_gitignore(self):
        if not shutil.which("git"):
            self.skipTest("git not installed")
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True)
        (self.tmp / ".gitignore").write_text("data.csv\n")
        ctx = cx.build_context(str(self.tmp))
        self.assertNotIn("data.csv", [f.path for f in ctx.files])
        self.assertIn("notes/plan.txt", [f.path for f in ctx.files])

    def test_over_budget_switches_to_folder_mode(self):
        (self.tmp / "big.md").write_text("x" * 5000)
        ctx = cx.build_context(str(self.tmp), max_chars=1000)
        self.assertEqual(ctx.mode, "folder")
        self.assertIn("too big", ctx.reason)
        # forced inline: stays inline, block is cut at the budget and lists what's missing
        ctx = cx.build_context(str(self.tmp), max_chars=1000, mode="inline")
        self.assertEqual(ctx.mode, "inline")
        block = cx.inline_block(ctx)
        self.assertLess(len(block), 3000)
        self.assertIn("Not included", block)

    def test_forced_folder_mode_and_env_default(self):
        self.assertEqual(cx.build_context(str(self.tmp), mode="folder").mode, "folder")
        with patch.dict(os.environ, {"COUNCIL_CONTEXT_MODE": "folder"}):
            self.assertEqual(cx.build_context(str(self.tmp)).mode, "folder")
        with self.assertRaises(cx.ContextError):
            cx.build_context(str(self.tmp), mode="bogus")

    def test_single_file_and_errors(self):
        ctx = cx.build_context(str(self.tmp / "README.md"))
        self.assertEqual([f.path for f in ctx.files], ["README.md"])
        with self.assertRaises(cx.ContextError):
            cx.build_context(str(self.tmp / "nope"))
        empty = self.tmp / "only-binary"
        empty.mkdir()
        (empty / "a.png").write_bytes(b"\0\0")
        with self.assertRaises(cx.ContextError):
            cx.build_context(str(empty))

    def test_relative_path_uses_base(self):
        ctx = cx.build_context("notes", base=str(self.tmp))
        self.assertEqual([f.path for f in ctx.files], ["plan.txt"])

    def test_docx(self):
        with zipfile.ZipFile(self.tmp / "memo.docx", "w") as z:
            z.writestr("word/document.xml",
                       '<w:document><w:body><w:p><w:r><w:t>Memo &amp; minutes</w:t></w:r></w:p>'
                       '<w:p><w:r><w:t>Second line</w:t></w:r></w:p></w:body></w:document>')
        ctx = cx.build_context(str(self.tmp))
        memo = next(f for f in ctx.files if f.path == "memo.docx")
        self.assertEqual(memo.kind, "docx")
        self.assertIn("Memo & minutes\nSecond line", memo.text)

    def test_pdf_optional(self):
        (self.tmp / "plan.pdf").write_bytes(MINI_PDF)
        ctx = cx.build_context(str(self.tmp))
        pdfs = [f for f in ctx.files if f.path == "plan.pdf"]
        if shutil.which("pdftotext"):
            self.assertEqual(len(pdfs), 1)
            self.assertIn("Quarterly plan PDF", pdfs[0].text)
        else:
            self.assertIn("plan.pdf", dict(ctx.skipped))
        # without any extractor the PDF is skipped with a hint, not an error
        with patch.object(cx.shutil, "which", return_value=None), \
                patch.object(cx.os.path, "exists", return_value=False), \
                patch.dict("sys.modules", {"pypdf": None}):
            ctx = cx.build_context(str(self.tmp))
        self.assertIn("pdftotext", dict(ctx.skipped)["plan.pdf"])


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ctx-prompt-"))
        make_folder(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_inline_stage1_prompt(self):
        ctx = cx.build_context(str(self.tmp), mode="inline")
        p = cx.stage1_prompt("What is the codename?", ctx)
        self.assertIn("===== FILE: README.md =====", p)
        self.assertIn("HERON", p)
        self.assertIn("cite the file paths", p)
        self.assertIn("logo.png", p)  # skipped manifest
        self.assertTrue(p.endswith("What is the codename?"))
        self.assertEqual(cx.stage1_prompt("Q", None), "Q")

    def test_folder_stage1_prompt_has_manifest_not_text(self):
        ctx = cx.build_context(str(self.tmp), mode="folder")
        p = cx.stage1_prompt("Q?", ctx)
        self.assertIn("current working", p)
        self.assertIn("- notes/plan.txt", p)
        self.assertNotIn("HERON", p)

    def test_review_prefix(self):
        ctx = cx.build_context(str(self.tmp), mode="inline")
        self.assertIn("HERON", cx.review_prefix(ctx))
        with patch.dict(os.environ, {"COUNCIL_CONTEXT_REVIEW_CHARS": "10"}):
            pre = cx.review_prefix(ctx)
        self.assertNotIn("HERON", pre)
        self.assertIn("- README.md", pre)
        self.assertEqual(cx.review_prefix(None), "")
        self.assertIn("read-only", cx.review_prefix(cx.build_context(str(self.tmp), mode="folder"), "chair"))


class ProviderCommandTests(unittest.TestCase):
    def setUp(self):
        self.which = patch.object(providers, "_which", side_effect=lambda b: f"/bin/{b}")
        self.which.start()
        self.addCleanup(self.which.stop)

    def test_plain_mode_has_no_tools(self):
        m = Member("Claude", "claude", "sonnet")
        cmd = providers.claude_cmd(m)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertNotIn("--permission-mode", cmd)
        g = providers.grok_cmd(Member("Grok", "grok"), "/s/p.txt", "/s")
        self.assertEqual(g[g.index("--tools") + 1], "")
        self.assertEqual(g[g.index("--cwd") + 1], "/s")
        c = providers.codex_cmd(Member("ChatGPT", "codex"), "/s", "/s/o.txt")
        self.assertEqual(c[c.index("--cd") + 1], "/s")
        self.assertEqual(c[c.index("--sandbox") + 1], "read-only")

    def test_folder_mode_is_read_only(self):
        claude = providers.claude_cmd(Member("Claude", "claude"), folder="/f")
        self.assertEqual(claude[claude.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(claude[claude.index("--permission-mode") + 1], "dontAsk")
        for bad in ("Bash", "Write", "Edit", "WebFetch", "WebSearch"):
            self.assertIn(bad, claude[claude.index("--disallowedTools") + 1].split(","))
        self.assertNotIn("--dangerously-skip-permissions", claude)
        grok = providers.grok_cmd(Member("Grok", "grok"), "/s/p.txt", "/s", folder="/f")
        self.assertEqual(grok[grok.index("--tools") + 1], "read_file,list_dir,grep")
        self.assertEqual(grok[grok.index("--sandbox") + 1], "read-only")
        self.assertEqual(grok[grok.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(grok[grok.index("--cwd") + 1], "/f")
        self.assertIn("--disable-web-search", grok)
        self.assertNotIn("--always-approve", grok)
        codex = providers.codex_cmd(Member("ChatGPT", "codex"), "/s", "/s/o.txt", folder="/f")
        self.assertEqual(codex[codex.index("--sandbox") + 1], "read-only")
        self.assertEqual(codex[codex.index("--cd") + 1], "/f")
        self.assertIn('web_search="disabled"', codex)
        self.assertNotIn("--search", codex)

    def _capture(self, ctx, member):
        calls = []

        async def fake_run(cmd, stdin_text, cwd, timeout):
            calls.append({"cmd": cmd, "cwd": cwd, "stdin": stdin_text, "timeout": timeout})
            if "claude" in cmd[0]:
                return '{"result": "ok"}'
            if "grok" in cmd[0]:
                return '{"text": "ok"}'
            return "ok"

        with patch.object(providers, "_run", fake_run):
            with providers.use_context(ctx):
                r = asyncio.run(providers.query_member(member, [{"role": "user", "content": "Q"}], timeout=100))
        return r, calls

    def test_query_member_runs_in_folder_only_in_folder_mode(self):
        tmp = Path(tempfile.mkdtemp(prefix="ctx-prov-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        make_folder(tmp)
        folder_ctx = cx.build_context(str(tmp), mode="folder")
        r, calls = self._capture(folder_ctx, Member("Claude", "claude"))
        self.assertEqual(r, {"content": "ok"})
        self.assertEqual(calls[0]["cwd"], str(tmp.resolve()))
        self.assertEqual(calls[0]["timeout"], 200)  # x2 in folder mode
        self.assertTrue(calls[0]["stdin"].startswith(providers.FOLDER_PREAMBLE))

        inline_ctx = cx.build_context(str(tmp), mode="inline")
        r, calls = self._capture(inline_ctx, Member("Claude", "claude"))
        self.assertNotEqual(calls[0]["cwd"], str(tmp.resolve()))
        self.assertEqual(calls[0]["timeout"], 100)
        self.assertTrue(calls[0]["stdin"].startswith(providers.CLI_PREAMBLE))

        r, calls = self._capture(None, Member("Grok", "grok"))
        self.assertNotIn(str(tmp.resolve()), calls[0]["cmd"])

    def test_openrouter_gets_inline_fallback_in_folder_mode(self):
        tmp = Path(tempfile.mkdtemp(prefix="ctx-or-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        make_folder(tmp)
        ctx = cx.build_context(str(tmp), mode="folder")
        seen = {}

        async def fake_or(member, messages, timeout):
            seen["prompt"] = messages[0]["content"]
            return "ok"

        with patch.object(providers, "_query_openrouter", fake_or), providers.use_context(ctx):
            asyncio.run(providers.query_member(Member("x", "openrouter", "x/y"),
                                               [{"role": "user", "content": "Q"}], timeout=10))
        self.assertIn("no file access", seen["prompt"])
        self.assertIn("HERON", seen["prompt"])


class CouncilWiringTests(unittest.TestCase):
    def test_full_council_passes_context_to_every_stage(self):
        from backend import council
        tmp = Path(tempfile.mkdtemp(prefix="ctx-council-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        make_folder(tmp)
        ctx = cx.build_context(str(tmp), mode="inline")
        prompts = []

        async def fake_query(member, messages, timeout):
            p = messages[0]["content"]
            prompts.append(p)
            if "FINAL RANKING" in p:
                return {"content": "FINAL RANKING:\n1. Response A\n2. Response B"}
            return {"content": f"{member.name}: see README.md"}

        members = [Member("A", "claude"), Member("B", "grok")]

        class R:
            def active_members(self):
                return members

            def chair_member(self):
                return members[0]

        with patch.object(council, "query_member", fake_query):
            s1, s2, s3, meta = asyncio.run(council.run_full_council("Codename?", R(), context=ctx))
        self.assertEqual(len(s1), 2)
        self.assertTrue(all("HERON" in p for p in prompts))  # stage 1, 2 and 3 all see the files
        self.assertIn("Cite the file paths", prompts[-1])
        self.assertEqual(meta["context"]["mode"], "inline")


if __name__ == "__main__":
    unittest.main()
