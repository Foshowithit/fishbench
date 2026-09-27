"""FishBench scoring: straight face, Sir. Look., threats, breaks, ranking."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fishbench.scoring import (  # noqa: E402
    FishBenchScorer, LeaderboardEntry, Turn, looks_like_sir_look, rank,
    threat_score,
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


if __name__ == "__main__":
    unittest.main()
