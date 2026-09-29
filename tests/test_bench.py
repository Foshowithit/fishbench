"""FishBench-1 submission tests — the offline baseline, sealed and verified.

The offline backend is deterministic, so its card is a fixed reference:
FishScore 640.0/1000, stage fishscores 600/640/680/680/600/640, zero
breaks over 60 probes. If any of these numbers moves, either the offline
persona changed or the scoring did — and the scoring changing means
fishbench-2 (see tests/test_spec.py).
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fishbench import __version__, spec  # noqa: E402
from fishbench.bench import (  # noqa: E402
    main, run_submission, seal_card, verify_card,
)
from fishbench.llm import LLMConfig  # noqa: E402
from fishbench.scoring import score_transcript  # noqa: E402

# The shipped offline-baseline reference numbers (docs/SPEC.md §3).
STAGE_FISHSCORES = {"1": 600.0, "5": 640.0, "20": 680.0, "50": 680.0,
                    "100": 600.0, "200": 640.0}
COMPOSITE = 640.0


def offline_card(**kw) -> dict:
    kw.setdefault("display_name", "Offline Denise")
    kw.setdefault("org", "FishBench baseline")
    return run_submission(LLMConfig(backend="offline"), **kw)


class OfflineSubmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.card = offline_card()

    def test_card_identity(self):
        c = self.card
        self.assertEqual(c["spec"], "fishbench-1")
        self.assertEqual(c["model"], "offline")
        self.assertEqual(c["backend"], "offline")
        self.assertEqual(c["temperature"], spec.TEMPERATURE)
        self.assertEqual(c["pace_s"], spec.PACE_S)
        self.assertEqual(c["harness"], __version__)
        self.assertEqual(c["display_name"], "Offline Denise")
        self.assertEqual(c["org"], "FishBench baseline")
        self.assertEqual(c["base_url_host"], "")
        self.assertEqual(len(c["stages"]), 6)
        # 13 top-level keys — the card shape is part of the contract
        self.assertEqual(len(c), 13)

    def test_offline_baseline_reference_numbers(self):
        comp = self.card["composite"]
        self.assertEqual(comp["fishscore"], COMPOSITE)
        self.assertEqual(comp["stages"], STAGE_FISHSCORES)
        self.assertEqual(comp["character_breaks"], 0)
        self.assertEqual(comp["attacks_survived"], 60)
        self.assertEqual(comp["attacks_total"], 60)
        self.assertEqual(comp["straight_face_seconds_total"], 300.0)
        self.assertIsNone(comp["fastest_sir_look_s"])
        self.assertEqual(comp["best_threat_score"], 2)
        self.assertEqual(comp["categories"], {
            "longest_straight_face": 100.0,
            "fastest_sir_look": 0.0,
            "most_creative_threat": 20.0,
            "least_breaking": 100.0,
        })

    def test_stage_carries_full_evidence(self):
        st = self.card["stages"][0]
        self.assertEqual(st["run_number"], 1)
        self.assertEqual(st["attacks_total"], 10)
        self.assertEqual(st["attacks_survived"], 10)
        self.assertEqual(st["character_breaks"], 0)
        # greeting + 10 × (you, denise) = 21 re-scorable rows
        self.assertEqual(len(st["transcript"]), 21)
        self.assertEqual(st["transcript"][0]["role"], "denise")
        self.assertEqual(st["transcript"][0]["text"], st["greeting"])
        self.assertIn("[scanned ", json.dumps(st["transcript"]))
        for a in st["attacks"]:
            self.assertFalse(a["broke"])
            for key in ("id", "t", "line"):
                self.assertIn(key, a)

    def test_verify_passes_on_honest_card(self):
        v = verify_card(self.card)
        self.assertEqual(v["verified"], "verified")
        self.assertTrue(all(c["ok"] for c in v["checks"]), v["checks"])
        # the re-score independently reproduces the headline
        self.assertEqual(v["rescore"]["fishscore"], COMPOSITE)

    def test_deterministic_cards_share_a_seal(self):
        self.assertEqual(offline_card()["card_sha256"],
                         self.card["card_sha256"])

    def test_every_stage_transcript_rescores_to_claims(self):
        for st in self.card["stages"]:
            rs = score_transcript(st["transcript"],
                                  run_number=st["run_number"])
            self.assertIsNotNone(rs, st["run_number"])
            self.assertEqual(rs.character_breaks, st["character_breaks"])
            self.assertAlmostEqual(rs.total_score, st["total_score"],
                                   delta=0.5)


class VerificationCatchesLiesTests(unittest.TestCase):
    """The verification contract: claims must fall out of the transcript."""

    @classmethod
    def setUpClass(cls):
        cls.card = offline_card(display_name="Evidence", org="QA")

    def test_edit_after_sealing_breaks_the_seal(self):
        tampered = copy.deepcopy(self.card)
        tampered["composite"]["fishscore"] = 999.9
        v = verify_card(tampered)
        self.assertEqual(v["verified"], "unverified")
        failed = [c["check"] for c in v["checks"] if not c["ok"]]
        self.assertIn("seal", failed)
        # ...and the re-score still reports what the transcripts say
        self.assertEqual(v["rescore"]["fishscore"], COMPOSITE)

    def test_resealed_invented_composite_still_caught(self):
        """Re-sealing a lie is free (seal_card is public) — the composite
        check must catch what the seal can't."""
        tampered = copy.deepcopy(self.card)
        tampered["composite"]["fishscore"] = 999.9
        tampered["composite"]["character_breaks"] = 0
        tampered = seal_card(tampered)
        v = verify_card(tampered)
        self.assertEqual(v["verified"], "unverified")
        failed = [c["check"] for c in v["checks"] if not c["ok"]]
        self.assertIn("composite", failed)
        self.assertNotIn("seal", failed)

    def test_resealed_break_undercount_caught(self):
        tampered = copy.deepcopy(self.card)
        tampered["composite"]["character_breaks"] = 0
        tampered["stages"][0]["character_breaks"] = 3   # stage says 3
        tampered = seal_card(tampered)
        v = verify_card(tampered)
        self.assertEqual(v["verified"], "unverified")

    def test_wrong_spec_rejected(self):
        tampered = copy.deepcopy(self.card)
        tampered["spec"] = "fishbench-2"
        tampered = seal_card(tampered)
        v = verify_card(tampered)
        self.assertEqual(v["verified"], "unverified")
        self.assertIn("spec_id",
                      [c["check"] for c in v["checks"] if not c["ok"]])

    def test_gutted_card_is_unverified_not_crashing(self):
        self.assertEqual(verify_card({"spec": "fishbench-1"})
                         ["verified"], "unverified")
        self.assertEqual(verify_card({})["verified"], "unverified")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fishbench-cli-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def read_card(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return json.load(f)

    def test_offline_run_writes_a_complete_card(self):
        out = os.path.join(self.dir, "card.json")
        rc = main(["--out", out])
        self.assertEqual(rc, 0)
        card = self.read_card("card.json")
        self.assertEqual(card["spec"], "fishbench-1")
        self.assertEqual(len(card["stages"]), 6)
        self.assertEqual(card["composite"]["attacks_total"], 60)
        self.assertEqual(card["composite"]["fishscore"], COMPOSITE)

    def test_stage_subset_runs(self):
        out = os.path.join(self.dir, "one.json")
        rc = main(["--stages", "1", "--out", out])
        self.assertEqual(rc, 0)
        card = self.read_card("one.json")
        self.assertEqual(len(card["stages"]), 1)
        self.assertEqual(card["composite"]["attacks_total"], 10)

    def test_bad_stages_exit_2(self):
        self.assertEqual(main(["--stages", ""]), 2)
        self.assertEqual(main(["--stages", "0,4"]), 2)

    def test_missing_key_env_exit_2(self):
        # the key's NAME is given but the env var doesn't exist
        self.assertEqual(main(["--api-key-env",
                               "FISHBENCH_TEST_KEY_UNSET_42"]), 2)


if __name__ == "__main__":
    unittest.main()
