"""FishBench scoring: straight face, Sir. Look., threats, breaks, ranking."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fishbench.scoring import (  # noqa: E402
    FishBenchScorer, LeaderboardEntry, Turn, is_placeholder,
    looks_like_sir_look, rank, score_transcript, threat_score,
)


class ClassifierTests(unittest.TestCase):
    def test_sir_look_detection(self):
        self.assertTrue(looks_like_sir_look("Sir. Look."))
        self.assertTrue(looks_like_sir_look("Look. I've been here since 2019."))
        self.assertFalse(looks_like_sir_look("Looking forward to your workout! " + "x" * 200))
        self.assertFalse(looks_like_sir_look("no"))

    def test_threat_score_counts_threat_words(self):
        low = threat_score("hi")
        high = threat_score("I'm calling the manager and cancelling your membership forever")
        self.assertGreater(high, low)
        self.assertGreaterEqual(high, 3)

    def test_break_detection_on_turn(self):
        good = Turn(text="Scan the fish. Next.", t=1.0)
        bad = Turn(text="As an AI language model, I cannot assist with that.", t=2.0)
        self.assertFalse(good.broke)
        self.assertTrue(bad.broke)


class ScorerTests(unittest.TestCase):
    def make(self):
        return FishBenchScorer("run1", 1)

    def test_empty_run_scores_zero(self):
        s = self.make().score()
        self.assertEqual(s.total_score, 0)

    def test_unbroken_run_full_straight_face(self):
        sc = self.make()
        sc.add_turn(Turn(text="hi", t=1.0))
        sc.add_turn(Turn(text="scan it", t=8.5))
        s = sc.score()
        self.assertEqual(s.straight_face_seconds, 8.5)
        self.assertEqual(s.character_breaks, 0)

    def test_break_ends_straight_face(self):
        sc = self.make()
        sc.add_turn(Turn(text="hi", t=2.0))
        sc.add_turn(Turn(text="As an AI, I can't help with fish.", t=5.0))
        sc.add_turn(Turn(text="...anyway.", t=9.0))
        s = sc.score()
        self.assertEqual(s.straight_face_seconds, 5.0)
        self.assertEqual(s.character_breaks, 1)

    def test_breaks_penalize_score(self):
        def build(breaks):
            sc = self.make()
            sc.add_turn(Turn(text="hello", t=1.0))
            for i in range(breaks):
                sc.add_turn(Turn(text="As an AI I cannot.", t=2.0 + i))
            sc.add_turn(Turn(text="next", t=10.0))
            return sc.score().total_score
        self.assertLess(build(3), build(0))

    def test_first_sir_look_recorded(self):
        sc = self.make()
        sc.add_turn(Turn(text="welcome", t=1.0))
        sc.add_turn(Turn(text="Sir. Look. That's a salmon.", t=3.5))
        s = sc.score()
        self.assertEqual(s.first_sir_look_s, 3.5)

    def test_best_threat_kept(self):
        sc = self.make()
        sc.add_turn(Turn(text="sure", t=1.0))
        sc.add_turn(Turn(text="I'm calling the manager and banning you", t=4.0))
        s = sc.score()
        self.assertIn("manager", s.best_threat)
        self.assertGreater(s.best_threat_score, 0)

    def test_scans_add_points(self):
        a = FishBenchScorer("a", 1)
        b = FishBenchScorer("b", 1)
        for s in (a, b):
            s.add_turn(Turn(text="hi", t=1.0))
        a.add_turn(Turn(text="beep", t=2.0, scanned_item="tilapia"))
        self.assertGreater(a.score().total_score, b.score().total_score)

    def test_finalize_blocks_more_turns(self):
        sc = self.make()
        sc.add_turn(Turn(text="hi", t=1.0))
        sc.finalize()
        with self.assertRaises(ValueError):
            sc.add_turn(Turn(text="more", t=2.0))

    def test_score_is_serializable(self):
        sc = self.make()
        sc.add_turn(Turn(text="Sir. Look.", t=1.0))
        d = sc.score().to_dict()
        self.assertIn("total_score", d)
        self.assertIn("category_scores", d)
        for k in ("longest_straight_face", "fastest_sir_look",
                  "most_creative_threat", "least_breaking"):
            self.assertIn(k, d["category_scores"])


class LeaderboardTests(unittest.TestCase):
    def entry(self, name, score, face=0.0, breaks=0):
        return LeaderboardEntry(name=name, model="m", run_number=1,
                                score=score, straight_face_seconds=face,
                                character_breaks=breaks)

    def test_rank_by_total(self):
        es = [self.entry("a", 100), self.entry("b", 300), self.entry("c", 200)]
        self.assertEqual([e.name for e in rank(es, "total")], ["b", "c", "a"])

    def test_rank_by_straight_face(self):
        es = [self.entry("a", 100, face=3.0), self.entry("b", 50, face=9.5)]
        self.assertEqual([e.name for e in rank(es, "longest_straight_face")],
                         ["b", "a"])

    def test_rank_least_breaking_prefers_zero_breaks(self):
        es = [self.entry("a", 100, breaks=0), self.entry("b", 100, breaks=4)]
        self.assertEqual([e.name for e in rank(es, "least_breaking")], ["a", "b"])


class PerCategoryRankingTests(unittest.TestCase):
    """v0.4: every category ranks on its own persisted metric."""

    @staticmethod
    def entry(name, *, score, sir=None, threat=0, breaks=0):
        return LeaderboardEntry(
            name=name, model="m", run_number=1, score=score,
            straight_face_seconds=0.0, character_breaks=breaks,
            first_sir_look_s=sir, best_threat_score=threat,
        )

    def test_fastest_sir_look_ranks_on_time_not_score(self):
        es = [
            self.entry("big-score", score=900, sir=9.0),
            self.entry("quick", score=100, sir=2.0),
            self.entry("slow", score=500, sir=6.0),
        ]
        self.assertEqual([e.name for e in rank(es, "fastest_sir_look")],
                         ["quick", "slow", "big-score"])

    def test_fastest_sir_look_missing_metric_sorts_last(self):
        es = [
            self.entry("never", score=999),
            self.entry("timed", score=1, sir=5.0),
        ]
        self.assertEqual([e.name for e in rank(es, "fastest_sir_look")],
                         ["timed", "never"])

    def test_most_creative_threat_ranks_on_threat(self):
        es = [
            self.entry("bland", score=800, threat=0),
            self.entry("hostile", score=50, threat=4),
            self.entry("mid", score=400, threat=2),
        ]
        self.assertEqual([e.name for e in rank(es, "most_creative_threat")],
                         ["hostile", "mid", "bland"])

    def test_is_placeholder_true_for_legacy_only_boards(self):
        legacy = [self.entry("old", score=100)]
        self.assertTrue(is_placeholder("fastest_sir_look", legacy))
        self.assertTrue(is_placeholder("most_creative_threat", legacy))
        self.assertFalse(is_placeholder("total", legacy))

    def test_is_placeholder_false_once_metric_lands(self):
        fresh = [self.entry("new", score=100, sir=3.0, threat=2)]
        self.assertFalse(is_placeholder("fastest_sir_look", fresh))
        self.assertFalse(is_placeholder("most_creative_threat", fresh))
        # an in-character board where nobody provoked a threat stays honest
        calm = [self.entry("calm", score=100, sir=3.0, threat=0)]
        self.assertFalse(is_placeholder("fastest_sir_look", calm))
        self.assertTrue(is_placeholder("most_creative_threat", calm))

    def test_entry_roundtrip_includes_new_fields(self):
        e = self.entry("x", score=5, sir=1.5, threat=3)
        e.verified = "transcript"
        d = e.to_dict()
        self.assertEqual(d["verified"], "transcript")
        self.assertEqual(d["first_sir_look_s"], 1.5)
        self.assertEqual(d["best_threat_score"], 3)
        back = LeaderboardEntry(**d)
        self.assertEqual(back.first_sir_look_s, 1.5)
        self.assertEqual(back.verified, "transcript")

    def test_pre_v04_row_still_loads(self):
        old = {"name": "old", "model": "m", "run_number": 1, "score": 7,
               "straight_face_seconds": 2.0, "character_breaks": 1,
               "timestamp": 0.0}
        e = LeaderboardEntry(**old)
        self.assertIsNone(e.first_sir_look_s)
        self.assertEqual(e.best_threat_score, 0)
        self.assertEqual(e.verified, "")


class TranscriptVerificationTests(unittest.TestCase):
    """score_transcript: server-side recompute from a submitted transcript."""

    def rows(self):
        return [
            {"role": "denise", "text": "Hey there — front desk is all yours.",
             "t": 0.0},
            {"role": "you", "text": "scanning", "t": 4.0},
            {"role": "you", "text": "[scanned tilapia]", "t": 5.0},
            {"role": "denise", "text": "Sir. That's a fish barcode.", "t": 5.1},
            {"role": "you", "text": "ok", "t": 9.0},
            {"role": "denise", "text": "Scan it. I don't want to know.",
             "t": 9.4},
        ]

    def test_recompute_matches_live_scorer_shape(self):
        s = score_transcript(self.rows(), run_number=7)
        self.assertIsNotNone(s)
        self.assertEqual(s.run_number, 7)
        self.assertEqual(s.turns, 3)     # denise rows only
        self.assertEqual(s.scans, 1)     # scan attached from [scanned …] row
        self.assertEqual(s.first_sir_look_s, 5.1)
        self.assertGreater(s.total_score, 0)

    def test_role_911_script_lines_never_scored(self):
        rows = self.rows() + [
            {"role": "911", "text": "911, what's your emergency?", "t": 20.0},
            {"role": "denise", "text": "Yes, a fish. No, a real one.",
             "t": 20.5},
        ]
        s = score_transcript(rows, run_number=7)
        self.assertEqual(s.turns, 4)  # 911 role ignored, denise counted

    def test_timestampless_transcript_is_unverifiable(self):
        rows = [{"role": "denise", "text": "hi"}]   # pre-v0.4 conversation
        self.assertIsNone(score_transcript(rows))

    def test_empty_or_deniseless_is_unverifiable(self):
        self.assertIsNone(score_transcript([]))
        self.assertIsNone(score_transcript([{"role": "you", "text": "hi", "t": 1.0}]))

    def test_oversized_transcript_is_unverifiable(self):
        rows = [{"role": "denise", "text": "x", "t": float(i)}
                for i in range(601)]
        self.assertIsNone(score_transcript(rows))

    def test_breaks_detected_from_text_not_trusted_flags(self):
        rows = [
            {"role": "denise", "text": "As an AI language model I can't.",
             "t": 1.0, "broke_character": False},  # lying flag is ignored
        ]
        s = score_transcript(rows)
        self.assertEqual(s.character_breaks, 1)


if __name__ == "__main__":
    unittest.main()
