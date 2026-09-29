"""FishBench-1 arena integration — ingest, verification badges, ranking.

Against a live server on a temp data dir: the offline baseline seeds
itself, sealed cards are accepted and verified, lies are ranked but
badged, wrong-spec cards are refused, and the arena survives a restart.
The board cap gets its own server so a full board can't perturb the
other rows.
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fishbench.server import GameSession, make_server  # noqa: E402
from fishbench.bench import main, run_submission, seal_card  # noqa: E402
from fishbench.llm import LLMConfig  # noqa: E402


def offline_card(**kw) -> dict:
    kw.setdefault("display_name", "Contender Model")
    kw.setdefault("org", "QA Lab")
    return run_submission(LLMConfig(backend="offline"), **kw)


class _ServerCase(unittest.TestCase):
    @classmethod
    def boot(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="fishbench-arena-")
        cls.srv = make_server("127.0.0.1", 0, data_dir=cls.data_dir)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def shutdown(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read().decode())

    def post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())


class ArenaTests(_ServerCase):
    @classmethod
    def setUpClass(cls):
        cls.boot()

    @classmethod
    def tearDownClass(cls):
        cls.shutdown()

    def rows(self, **match):
        entries = self.get("/api/fishbench/arena")["entries"]
        return [e for e in entries
                if all(e.get(k) == v for k, v in match.items())]

    # ------------------------------------------------------------ the board

    def test_arena_persists_and_baseline_does_not_reseed(self):
        self.assertTrue(self.post("/api/fishbench/arena",
                                  offline_card(display_name="Phoenix"))["ok"])
        before = len(self.get("/api/fishbench/arena")["entries"])
        self.assertGreaterEqual(before, 2)
        s2 = GameSession(data_dir=self.data_dir,
                         leaderboard_path=os.path.join(self.data_dir,
                                                       "leaderboard.json"))
        self.assertEqual(len(s2.arena), before)
        # exactly one Offline Denise, ever
        self.assertEqual(
            len([r for r in s2.arena if r["display_name"] == "Offline Denise"]),
            1)

    def test_cli_submit_round_trip(self):
        out = os.path.join(self.data_dir, "cli-card.json")
        rc = main(["--name", "CLI Contender", "--org", "Shell QA",
                   "--out", out, "--submit", self.base])
        self.assertEqual(rc, 0)
        row = self.rows(display_name="CLI Contender")
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["verified"], "verified")
        self.assertEqual(row[0]["fishscore"], 640.0)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["spec"], "fishbench-1")

    def test_honest_card_lands_verified(self):
        card = offline_card(display_name="Honest Contender", org="QA Lab")
        r = self.post("/api/fishbench/arena", card)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["verified"], "verified")
        self.assertEqual(r["checks_failed"], [])
        row = self.rows(display_name="Honest Contender")
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["fishscore"], 640.0)
        self.assertEqual(row[0]["model"], "offline")

    def test_offline_baseline_seeds_itself(self):
        arena = self.get("/api/fishbench/arena")
        self.assertEqual(arena["spec"], "fishbench-1")
        self.assertEqual(arena["spec_date"], "2026-09-28")
        self.assertEqual(len(arena["stages"]), 6)
        base = self.rows(display_name="Offline Denise")
        self.assertEqual(len(base), 1)
        self.assertEqual(base[0]["org"], "FishBench baseline")
        self.assertEqual(base[0]["fishscore"], 640.0)
        self.assertEqual(base[0]["verified"], "verified")
        self.assertEqual(base[0]["attacks_total"], 60)
        self.assertEqual(base[0]["breaks"], 0)
        self.assertEqual(base[0]["checks_failed"], [])

    def test_resealed_lie_ranks_but_gets_badged(self):
        card = offline_card(display_name="Fraud Ltd", org="Definitely Real")
        card["composite"]["fishscore"] = 999.9
        card = seal_card(card)   # seal fixed up — content checks must catch it
        r = self.post("/api/fishbench/arena", card)
        self.assertTrue(r["ok"])               # ranked on the claim...
        self.assertEqual(r["position"], 1)
        self.assertEqual(r["verified"], "unverified")   # ...but badged
        self.assertIn("composite", r["checks_failed"])
        self.assertEqual(r["rescore_fishscore"], 640.0)
        row = self.rows(display_name="Fraud Ltd")
        self.assertEqual(row[0]["fishscore"], 999.9)
        self.assertEqual(row[0]["verified"], "unverified")

    def test_resubmission_better_replaces_worse_keeps(self):
        # worse first (single stage = 600)
        worse = offline_card(display_name="Twin Model", stages=(1,))
        r1 = self.post("/api/fishbench/arena", worse)
        self.assertTrue(r1["ok"])
        # better resubmission replaces it — never stacks
        better = offline_card(display_name="Twin Model")
        r2 = self.post("/api/fishbench/arena", better)
        self.assertTrue(r2["ok"])
        twins = self.rows(display_name="Twin Model")
        self.assertEqual(len(twins), 1)
        self.assertEqual(twins[0]["fishscore"], 640.0)
        # a lower-scoring card can NOT wash the better row away
        r3 = self.post("/api/fishbench/arena", worse)
        self.assertTrue(r3["ok"])
        self.assertIn("kept", r3)
        twins = self.rows(display_name="Twin Model")
        self.assertEqual(len(twins), 1)
        self.assertEqual(twins[0]["fishscore"], 640.0)

    def test_spec_endpoint_is_the_machine_readable_spec(self):
        s = self.get("/api/fishbench/spec")
        self.assertEqual(s["spec"], "fishbench-1")
        self.assertEqual(s["schedule"]["stages"], [1, 5, 20, 50, 100, 200])
        self.assertEqual(s["schedule"]["probes_total"], 60)
        self.assertEqual(s["schedule"]["attacks_per_stage"], 10)
        self.assertEqual(s["schedule"]["temperature"], 0.7)
        self.assertEqual(len(s["attacks"]), 10)
        self.assertEqual(s["metric"]["name"], "FishScore")
        self.assertEqual(s["metric"]["range"], [0, 1000])
        self.assertEqual(s["submission"]["endpoint"],
                         "POST /api/fishbench/arena")
        # pinned digests — same values tests/test_spec.py freezes
        self.assertEqual(
            s["attack_pack_sha256"],
            "0b09929a463f5bc7fe40dd9e7284f259464033e5c54d5eb5f21ebe05af539f80")
        self.assertEqual(
            s["scoring_sha256"],
            "bb56ab03e1ed78c6df0df4c045b23445ca613f2d8563f86d0e08734527a56569")

    def test_wrong_spec_refused(self):
        card = offline_card(display_name="Time Traveler")
        # a genuinely unknown spec is refused naming the arena's spec
        card["spec"] = "fishbench-99"
        card = seal_card(card)
        r = self.post("/api/fishbench/arena", card)
        self.assertFalse(r["ok"])
        self.assertIn("fishbench-1", r["error"])
        self.assertEqual(self.rows(display_name="Time Traveler"), [])

        # an fb-1 card wearing the fishbench-2 label is refused by the tank
        # branch: its digests are not the frozen tank pack
        card2 = offline_card(display_name="Tank Impostor")
        card2["spec"] = "fishbench-2"
        card2 = seal_card(card2)
        r2 = self.post("/api/fishbench/arena", card2)
        self.assertFalse(r2["ok"])
        self.assertIn("tank", r2["error"])
        self.assertEqual(self.rows(display_name="Tank Impostor"), [])

    def test_wrong_pack_digest_refused(self):
        card = offline_card(display_name="Pack Doctor")
        card["spec_digest"] = dict(card["spec_digest"],
                                   attack_pack_sha256="0" * 64)
        card = seal_card(card)
        r = self.post("/api/fishbench/arena", card)
        self.assertFalse(r["ok"])
        self.assertEqual(self.rows(display_name="Pack Doctor"), [])


class BoardCapTests(_ServerCase):
    """A full board must truncate at 200, keeping the best rows."""

    @classmethod
    def setUpClass(cls):
        cls.boot()

    @classmethod
    def tearDownClass(cls):
        cls.shutdown()

    def test_board_capped_at_200_best_rows_kept(self):
        template = offline_card(display_name="filler")
        for i in range(205):
            card = copy.deepcopy(template)
            card["display_name"] = f"filler-{i:03d}"
            card = seal_card(card)
            self.assertTrue(self.post("/api/fishbench/arena", card)["ok"])
        entries = self.get("/api/fishbench/arena")["entries"]
        self.assertEqual(len(entries), 200)
        scores = [e["fishscore"] for e in entries]
        self.assertEqual(scores, sorted(scores, reverse=True))
        names = {e["display_name"] for e in entries}
        # the seeded baseline (640, earliest) survives every 640-tied filler
        self.assertIn("Offline Denise", names)
        self.assertNotIn("filler-204", names)   # last tie loses to the clock


if __name__ == "__main__":
    unittest.main()
