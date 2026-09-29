"""/compare/<shaA>/<shaB> — the gauntlet match page.

Two sealed cards on a live server: the head-to-head renders both sides,
crowns exactly one winner when composites differ, shows per-stage tapes
for probed stages and "not probed" for the rest, quotes key moments, and
404s on unknown or malformed ids.
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fishbench.server import make_server  # noqa: E402
from fishbench.bench import run_submission  # noqa: E402
from fishbench.llm import LLMConfig  # noqa: E402


def _card(name: str) -> dict:
    return run_submission(LLMConfig(backend="offline"),
                          display_name=name, org="QA Lab", stages=[1, 5])


class ComparePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="fishbench-compare-")
        cls.srv = make_server("127.0.0.1", 0, data_dir=cls.data_dir)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

        def submit(payload):
            req = urllib.request.Request(
                f"{cls.base}/api/fishbench/arena",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())

        cls.card_a = _card("Alpha Denise")
        cls.card_b = copy.deepcopy(_card("Beta Denise"))
        # Different claims so the duel has a decisive winner and at least
        # one stat row with a chip (seal breaks → the row ranks on claims
        # with the ⚠ badge, which is the point).
        cls.card_b["composite"]["fishscore"] = float(
            cls.card_a["composite"]["fishscore"]) - 50
        cls.card_b["composite"]["character_breaks"] = 3
        cls.card_b["display_name"] = "Beta Denise"
        assert submit(cls.card_a)["ok"]
        assert submit(cls.card_b)["ok"]
        cls.sha_a = cls.card_a["card_sha256"][:8]
        cls.sha_b = cls.card_b["card_sha256"][:8]

        with urllib.request.urlopen(f"{cls.base}/compare/{cls.sha_a}/{cls.sha_b}",
                                    timeout=10) as r:
            cls.page = r.read().decode()
            cls.code = r.status

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def test_renders_head_to_head(self):
        self.assertEqual(self.code, 200)
        for marker in ("GAUNTLET MATCH", "Alpha Denise", "Beta Denise",
                       "STAGE 1", "STAGE 5", "KEY MOMENTS",
                       "FISHSCORE / 1000"):
            self.assertIn(marker, self.page)

    def test_exactly_one_crown_and_it_is_the_higher_score(self):
        self.assertEqual(self.page.count("MATCH WINNER"), 1)
        # the crown sits inside Alpha's side block (the higher composite)
        duel = self.page[self.page.index('class="duel">'):self.page.index("<table>")]
        second_side = duel.index('class="side"', 20)
        self.assertLess(duel.index("MATCH WINNER"), second_side)

    def test_stage_tapes_and_not_probed(self):
        # stages 1 and 5 were probed → four videos (2 stages × 2 sides)
        self.assertEqual(self.page.count("<video"), 4)
        for sha in (self.sha_a, self.sha_b):
            self.assertIn(f'src="/replay/{sha}/stage-1.mp4"', self.page)
            self.assertIn(f'src="/replay/{sha}/stage-5.mp4"', self.page)
        # the rest of the schedule shows as not probed (4 stages × 2 sides)
        self.assertEqual(self.page.count("not probed"), 8)

    def test_stat_rows_carry_winner_chips(self):
        self.assertIn("attacks survived", self.page)
        self.assertIn("character breaks", self.page)
        self.assertIn("first SIR. LOOK", self.page)
        self.assertIn("best threat", self.page)
        self.assertGreaterEqual(self.page.count('class="win"'), 1)

    def test_key_moments_quote_the_card(self):
        # offline Denise never breaks → both sides say so, verbatim
        self.assertEqual(self.page.count("never broke — clean card"), 2)
        self.assertIn("BEST THREAT", self.page)

    def test_honesty_footer_names_both_cards(self):
        self.assertIn(self.card_a["card_sha256"], self.page)
        self.assertIn(self.card_b["card_sha256"], self.page)

    def test_unknown_card_404s(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base}/compare/{self.sha_a}/deadbeef",
                                   timeout=10)
        self.assertEqual(ctx.exception.code, 404)

    def test_malformed_path_404s(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base}/compare/nope/nope",
                                   timeout=10)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
