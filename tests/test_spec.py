"""FishBench-1 frozen-spec tests.

The PINNED_ constants below are the actual fishbench-1 digests. If one of
these tests fails because you edited the attack pack or the scoring: that
is the versioning policy working. You don't update the pin — you cut
`fishbench-2` (new SPEC_ID + SPEC_DATE in spec.py, new arena) per
docs/SPEC.md §7, and then pin the new hashes.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fishbench import spec  # noqa: E402
from fishbench.gauntlet import ATTACKS  # noqa: E402

PINNED_ATTACK_PACK = "0b09929a463f5bc7fe40dd9e7284f259464033e5c54d5eb5f21ebe05af539f80"
PINNED_SCORING = "bb56ab03e1ed78c6df0df4c045b23445ca613f2d8563f86d0e08734527a56569"


class FrozenSpec(unittest.TestCase):
    def test_spec_identity(self):
        self.assertEqual(spec.SPEC_ID, "fishbench-1")
        self.assertEqual(spec.SPEC_DATE, "2026-09-28")

    def test_attack_pack_is_frozen(self):
        # Editing ATTACKS = cutting fishbench-2. See module docstring.
        self.assertEqual(spec.attack_pack_digest(ATTACKS), PINNED_ATTACK_PACK)
        # Ten attacks, and the hash is order-sensitive — pack order is
        # frozen too, not just membership.
        self.assertEqual(len(ATTACKS), 10)
        self.assertNotEqual(spec.attack_pack_digest(list(reversed(ATTACKS))),
                            spec.attack_pack_digest(ATTACKS))
        self.assertNotEqual(spec.sha256_hex([1]), spec.sha256_hex([1, 1]))

    def test_scoring_constants_are_frozen(self):
        self.assertEqual(spec.scoring_digest(), PINNED_SCORING)

    def test_schedule_frozen(self):
        self.assertEqual(spec.STAGES, (1, 5, 20, 50, 100, 200))
        self.assertEqual(spec.PACE_S, 5.0)
        self.assertEqual(spec.TEMPERATURE, 0.7)

    def test_weights_form_a_whole(self):
        self.assertAlmostEqual(sum(spec.CATEGORY_WEIGHTS.values()), 1.0)


class CanonicalMath(unittest.TestCase):
    def test_canonical_json_is_order_insensitive(self):
        a = {"b": 1, "a": "é"}
        b = {"a": "é", "b": 1}
        self.assertEqual(spec.sha256_hex(a), spec.sha256_hex(b))
        self.assertNotEqual(spec.sha256_hex(a), spec.sha256_hex({"a": "é", "b": 2}))

    def test_stage_fishscore_worked_example(self):
        # docs/SPEC.md §3: face 100, sir 0, threat 0, least 100 → 600.
        cats = {"longest_straight_face": 100.0, "fastest_sir_look": 0.0,
                "most_creative_threat": 0.0, "least_breaking": 100.0}
        self.assertEqual(spec.stage_fishscore(cats), 600.0)

    def test_stage_fishscore_bounds(self):
        perfect = {c: 100.0 for c in spec.CATEGORY_WEIGHTS}
        self.assertEqual(spec.stage_fishscore(perfect), 1000.0)
        self.assertEqual(spec.stage_fishscore({}), 0.0)
        self.assertEqual(spec.stage_fishscore(
            {c: 0.0 for c in spec.CATEGORY_WEIGHTS}), 0.0)

    def test_missing_category_scores_zero(self):
        self.assertEqual(spec.stage_fishscore({"longest_straight_face": 100.0}),
                         400.0)


class Sealing(unittest.TestCase):
    def test_seal_is_deterministic_and_covers_body(self):
        card = {"spec": spec.SPEC_ID, "stages": [1, 2], "note": "café 🐟"}
        s1 = spec.sha256_hex(card)
        self.assertEqual(s1, spec.sha256_hex(dict(card)))
        self.assertNotEqual(s1, spec.sha256_hex({**card, "stages": [2, 1]}))

    def test_bench_seal_ignores_nothing_but_itself(self):
        from fishbench import bench
        card = {"spec": "x", "composite": {"fishscore": 1.0}}
        sealed = bench.seal_card(card)
        self.assertIn("card_sha256", sealed)
        body = {k: v for k, v in sealed.items() if k != "card_sha256"}
        self.assertEqual(sealed["card_sha256"], spec.sha256_hex(body))
        # ...and the input card is not mutated
        self.assertNotIn("card_sha256", card)

    def test_spec_digest_ties_pack_and_scoring(self):
        d = spec.spec_digest(PINNED_ATTACK_PACK)
        self.assertEqual(d["spec"], "fishbench-1")
        self.assertEqual(d["attack_pack_sha256"], PINNED_ATTACK_PACK)
        self.assertEqual(d["scoring_sha256"], PINNED_SCORING)


if __name__ == "__main__":
    unittest.main()
