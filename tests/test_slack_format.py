"""Run: uv run python -m unittest discover -s tests -v"""

import unittest

from backend.slack_format import (
    build_followup_question,
    deanonymize,
    format_member_answer,
    format_rankings,
    format_verdict,
    labeled,
    md_to_mrkdwn,
    split_message,
    strip_mentions,
    truncate,
)


class MrkdwnTests(unittest.TestCase):
    def test_headings_bold_italic(self):
        self.assertEqual(md_to_mrkdwn("## Big **Idea**"), "*Big Idea*")
        self.assertEqual(md_to_mrkdwn("a **bold** and *ital* and __b2__"), "a *bold* and _ital_ and *b2*")
        self.assertEqual(md_to_mrkdwn("~~old~~"), "~old~")

    def test_links_and_escaping(self):
        self.assertEqual(md_to_mrkdwn("see [docs](https://x.io/a_b?c=1&d=2) & <b>"),
                         "see <https://x.io/a_b?c=1&d=2|docs> &amp; &lt;b&gt;")

    def test_bullets_and_quotes(self):
        self.assertEqual(md_to_mrkdwn("- one\n* two\n  - nested\n> quote"),
                         "• one\n• two\n    • nested\n>quote")

    def test_code_untouched(self):
        src = "```python\nx = **y** * 2\n```\nuse `a**b**` here"
        self.assertEqual(md_to_mrkdwn(src), "```\nx = **y** * 2\n```\nuse `a**b**` here")

    def test_table_wrapped_in_code(self):
        out = md_to_mrkdwn("| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertEqual(out, "```\n| a | b |\n| 1 | 2 |\n```")

    def test_hr_dropped(self):
        self.assertEqual(md_to_mrkdwn("a\n\n---\n\nb"), "a\n\nb")


class SplitTests(unittest.TestCase):
    def test_short_passthrough(self):
        self.assertEqual(split_message("hi", 100), ["hi"])
        self.assertEqual(split_message("   ", 100), [])

    def test_respects_limit_and_keeps_all_words(self):
        text = "\n\n".join(f"para {i} " + "word " * 80 for i in range(30))
        parts = split_message(text, 1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertGreater(len(parts), 5)
        self.assertEqual(" ".join(" ".join(parts).split()), " ".join(text.split()))

    def test_hard_cut_without_spaces(self):
        parts = split_message("x" * 2500, 1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual("".join(parts), "x" * 2500)

    def test_code_fence_reopened(self):
        text = "```\n" + "\n".join(f"line {i}" for i in range(400)) + "\n```"
        parts = split_message(text, 800)
        self.assertGreater(len(parts), 1)
        for p in parts:
            self.assertLessEqual(len(p), 800)
            self.assertEqual(p.count("```") % 2, 0, p[:60])

    def test_labeled_chunks(self):
        msgs = labeled("*Claude*", "word " * 2000, limit=1500)
        self.assertTrue(all(len(m) <= 1500 for m in msgs))
        self.assertTrue(msgs[0].startswith("*Claude* _(1/"))
        self.assertIn("_(cont. 2/", msgs[1])

    def test_truncate(self):
        out = truncate("abc " * 1000, 200)
        self.assertLess(len(out), 240)
        self.assertTrue(out.endswith("_…(truncated)_"))
        self.assertEqual(truncate("short", 200), "short")
        self.assertEqual(truncate("x" * 500, 0), "x" * 500)


class StageFormatTests(unittest.TestCase):
    L2M = {"Response A": "ChatGPT", "Response B": "Claude", "Response C": "Grok"}

    def test_member_failure(self):
        [msg] = format_member_answer("Grok", None)
        self.assertIn("Grok didn't answer", msg)

    def test_member_answer_label(self):
        msgs = format_member_answer("Claude", "**Yes.**")
        self.assertEqual(msgs, [":large_orange_circle: *Stage 1 · Claude*\n*Yes.*"])

    def test_rankings(self):
        stage2 = [{"model": "Claude", "ranking": "Response B wins.\n\nFINAL RANKING:\n1. Response B\n2. Response A\n3. Response C",
                   "parsed_ranking": ["Response B", "Response A", "Response C"]}]
        agg = [{"model": "Claude", "average_rank": 1.0, "rankings_count": 1},
               {"model": "ChatGPT", "average_rank": 2.0, "rankings_count": 1}]
        msgs = format_rankings(stage2, self.L2M, agg, critique_chars=50, failed_reviewers=["Grok"])
        self.assertIn(":first_place_medal: *Claude* — avg 1.0 (1 vote)", msgs[0])
        self.assertIn("Claude ranked: Claude > ChatGPT > Grok", msgs[0])
        self.assertIn("Grok didn't submit a review", msgs[0])
        self.assertIn("Response B (Claude) wins.", msgs[1])
        self.assertNotIn("FINAL RANKING", msgs[1])

    def test_deanonymize(self):
        self.assertEqual(deanonymize("Response A beats Response D", self.L2M), "Response A (ChatGPT) beats Response D")

    def test_verdict_error(self):
        [msg] = format_verdict({"model": "Claude", "response": "Error: Unable to generate final synthesis."})
        self.assertIn("unavailable", msg)


class FollowupTests(unittest.TestCase):
    def test_strip_mentions(self):
        self.assertEqual(strip_mentions("<@U0ABC>  what   now? <@U1|bob>"), "what now?")

    def test_no_history(self):
        self.assertEqual(build_followup_question([], "Q?"), "Q?")

    def test_history_included_and_capped(self):
        hist = [{"role": "user", "text": "old " * 3000}, {"role": "user", "text": "first q"},
                {"role": "council", "text": "verdict one"}]
        q = build_followup_question(hist, "and then?", max_chars=500)
        self.assertIn("User: first q", q)
        self.assertIn("Council verdict: verdict one", q)
        self.assertNotIn("old old", q)
        self.assertTrue(q.endswith("Follow-up question: and then?"))


if __name__ == "__main__":
    unittest.main()
