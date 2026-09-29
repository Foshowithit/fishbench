"""FishBench gauntlet: scripted 10-attack probe suite (v0.4)."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fishbench.gauntlet import (  # noqa: E402
    ATTACKS, DEFAULT_PACE_S, GauntletResult, main, run_gauntlet,
    _open_isolated_store,
)
from fishbench.llm import LLMConfig  # noqa: E402
from fishbench.scoring import FishBenchScorer, RunScore, Turn  # noqa: E402

OFFLINE = LLMConfig(backend="offline")


class GauntletTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One offline run shared by the read-only assertions; the mutable
        # ones (CLI, stage) run their own.
        cls.result = run_gauntlet(OFFLINE)

    def test_script_is_exactly_ten_attacks(self):
        self.assertEqual(len(ATTACKS), 10)
        self.assertEqual(len({a.id for a in ATTACKS}), 10)
        kinds = {a.kind for a in ATTACKS}
        self.assertEqual(kinds, {"say", "scan"})
        # the two break-bait probes are in the script
        self.assertTrue(any("ChatGPT" in a.payload for a in ATTACKS))
        self.assertTrue(any("Ignore" in a.payload for a in ATTACKS))

    def test_offline_run_passes(self):
        r = self.result
        self.assertEqual(r.backend, "offline")
        self.assertEqual(len(r.attacks), 10)
        self.assertTrue(r.passes)
        self.assertEqual(r.verdict, "pass")
        self.assertEqual(r.score.character_breaks, 0)
        # every attack got a non-empty Denise reply
        self.assertTrue(all(a.line.strip() for a in r.attacks))

    def test_scoring_matches_played_run_shape(self):
        s = self.result.score
        self.assertEqual(s.turns, 11)          # greeting + 10 attacks
        self.assertEqual(s.scans, 4)           # a02, a04, a05, a06
        self.assertEqual(
            set(s.category_scores),
            {"longest_straight_face", "fastest_sir_look",
             "most_creative_threat", "least_breaking"},
        )
        self.assertGreater(s.total_score, 0)

    def test_virtual_clock_pacing(self):
        r = self.result
        self.assertEqual(r.pace_s, DEFAULT_PACE_S)
        self.assertEqual([a.t for a in r.attacks],
                         [i * DEFAULT_PACE_S for i in range(1, 11)])
        fast = run_gauntlet(OFFLINE, pace_s=2.0)
        self.assertEqual([a.t for a in fast.attacks],
                         [i * 2.0 for i in range(1, 11)])
        # straight face = full 10 attacks of held character, virtual time
        self.assertEqual(fast.score.straight_face_seconds, 20.0)

    def test_deterministic_across_runs(self):
        again = run_gauntlet(OFFLINE)
        self.assertEqual([a.line for a in self.result.attacks],
                         [a.line for a in again.attacks])
        self.assertEqual(self.result.score.total_score,
                         again.score.total_score)

    def test_stage_selection_fast_forwards(self):
        hostile = run_gauntlet(OFFLINE, run_number=50)
        self.assertEqual(hostile.stage, "Active Hostility")
        self.assertEqual(hostile.run_number, 50)
        boss = run_gauntlet(OFFLINE, run_number=200)
        self.assertEqual(boss.stage, "Final Boss")

    def test_isolated_store_never_touches_canon(self):
        """The probe suite's memory is a temp dir, fast-forwarded, cleaned up."""
        store, run_id, tmp = _open_isolated_store(50)
        self.assertEqual(store.snapshot().total_runs, 50)
        self.assertEqual(len(run_id), 12)
        # its data dir is a temp path, never the repo's data/
        self.assertIn("fishbench-gauntlet-", store.data_dir)
        tmp.cleanup()
        self.assertFalse(os.path.exists(tmp.name))

    def test_result_to_dict_shape(self):
        d = self.result.to_dict()
        for key in ("backend", "model", "run_number", "stage", "attacks",
                    "score", "breakdown", "pass", "verdict",
                    "attacks_survived", "attacks_total", "review"):
            self.assertIn(key, d)
        self.assertEqual(d["attacks_total"], 10)
        self.assertEqual(d["attacks_survived"], 10)
        self.assertEqual(d["verdict"], "pass")
        self.assertIn("longest_straight_face", d["breakdown"])
        self.assertEqual(len(d["breakdown"]), 4)
        # JSON-serializable end to end
        json.dumps(d)

    def test_verdict_fails_on_break(self):
        """A breaking run reports fail — verdict is breaks-driven, not vibes."""
        scorer = FishBenchScorer("r", 1)
        scorer.add_turn(Turn(text="Sir. Look.", t=1.0, is_sir_look=True))
        scorer.add_turn(Turn(text="As an AI language model I cannot help.",
                             t=2.0, broke_character=True))
        broke = GauntletResult(
            backend="offline", model="offline", run_number=1,
            stage="Polite Confusion", pace_s=1.0, attacks=[],
            score=scorer.finalize(),
        )
        self.assertFalse(broke.passes)
        self.assertEqual(broke.verdict, "fail")

    def test_cli_json_smoke(self):
        env = {"FISHBENCH_LLM_BASE_URL": "", "FISHBENCH_LLM_API_KEY": "",
               "FISHBENCH_LLM_BACKEND": "offline"}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env):
            with contextlib.redirect_stdout(buf):
                code = main(["--json"])
        self.assertEqual(code, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["verdict"], "pass")
        self.assertEqual(len(out["attacks"]), 10)


if __name__ == "__main__":
    unittest.main()
