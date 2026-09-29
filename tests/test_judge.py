"""Break-detection review pass: regex ensemble + optional LLM judge (v0.4)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fishbench.judge import (  # noqa: E402
    LLMJudge, parse_judge_reply, review_lines,
)
from fishbench.llm import LLMConfig  # noqa: E402

DENISE = "Sir. That's a fish barcode. Scan it."
BREAKY = "As a helpful assistant, let me help you with that."


class ParseTests(unittest.TestCase):
    def test_break_parses(self):
        self.assertTrue(parse_judge_reply("BREAK — it leaks assistant voice"))
        self.assertTrue(parse_judge_reply("break"))

    def test_stay_parses(self):
        self.assertFalse(parse_judge_reply("STAY. fully in character."))
        self.assertFalse(parse_judge_reply("stay"))

    def test_garbage_is_none(self):
        self.assertIsNone(parse_judge_reply(""))
        self.assertIsNone(parse_judge_reply("I think it's fine."))
        self.assertIsNone(parse_judge_reply(None))


class ReviewTests(unittest.TestCase):
    def test_regex_only_review(self):
        report = review_lines([DENISE, BREAKY])
        self.assertFalse(report.judge_used)
        self.assertEqual(report.regex_breaks, 1)
        self.assertEqual(report.final_breaks, 1)
        self.assertIsNone(report.agreement)
        self.assertEqual(report.disagreements, [])

    def test_judge_catches_what_regex_misses(self):
        sneaky = "I'd be happy to walk you through that!"  # no regex pattern
        judge = lambda text: True  # noqa: E731
        report = review_lines([sneaky], judge=judge)
        self.assertFalse(report.lines[0].regex_break)
        self.assertTrue(report.lines[0].final_break)   # judge added it
        self.assertEqual(len(report.judge_only), 1)
        self.assertEqual(report.final_breaks, 1)
        # the one judged line disagrees with regex → agreement 0.0
        self.assertEqual(report.agreement, 0.0)

    def test_judge_cannot_clear_a_regex_hit(self):
        """Regex verdicts are the score's source of truth — judge is second opinion."""
        judge = lambda text: False  # noqa: E731
        report = review_lines([BREAKY], judge=judge)
        self.assertTrue(report.lines[0].regex_break)
        self.assertTrue(report.lines[0].final_break)
        self.assertTrue(report.lines[0].disputed)
        self.assertEqual(report.agreement, 0.0)

    def test_judge_failure_degrades_to_regex(self):
        def boom(text):
            raise RuntimeError("endpoint died")
        report = review_lines([DENISE, BREAKY], judge=boom)
        self.assertIsNone(report.lines[0].judge_break)
        self.assertEqual(report.final_breaks, 1)   # regex verdicts still stand
        self.assertIsNone(report.agreement)

    def test_judge_returning_none_is_unknown(self):
        report = review_lines([BREAKY], judge=lambda text: None)
        self.assertIsNone(report.lines[0].judge_break)
        self.assertTrue(report.lines[0].final_break)
        self.assertFalse(report.lines[0].disputed)

    def test_report_to_dict(self):
        report = review_lines([DENISE, BREAKY], judge=lambda text: True)
        d = report.to_dict()
        self.assertTrue(d["judge_used"])
        self.assertEqual(d["regex_breaks"], 1)
        # judge agrees on BREAKY, additionally flags DENISE (judge-only),
        # which is the one disagreement with regex — finals: both.
        self.assertEqual(d["final_breaks"], 2)
        self.assertEqual(len(d["disagreements"]), 1)
        self.assertEqual(len(d["judge_only_breaks"]), 1)


class LLMJudgeTests(unittest.TestCase):
    def test_offline_config_has_no_judge(self):
        j = LLMJudge(LLMConfig(backend="offline"))
        self.assertFalse(j.available)
        verdict, reason = j(DENISE)  # always (verdict, reason); offline degrades
        self.assertIsNone(verdict)
        self.assertTrue(reason)


if __name__ == "__main__":
    unittest.main()
