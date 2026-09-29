"""FishBench-1 security tapes — deterministic replays, served from the seal.

The tape is presentation only: it must never change a score, it must be
byte-deterministic for a fixed card, and the server must render it on
demand from the archived sealed card (never from the arena row, which
only carries aggregates). The /watch page is plain HTML with honest
evidence-chain wording.
"""

import hashlib
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

from fishbench.bench import run_submission  # noqa: E402
from fishbench.llm import LLMConfig  # noqa: E402
from fishbench.replay import (  # noqa: E402
    END_FRAMES, FPS, GREETING_FRAMES, ROW_FRAMES, TITLE_FRAMES,
    _plan_frames, _row_tags, render_card_replays, render_stage_video,
)
from fishbench.server import make_server  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def offline_card(**kw) -> dict:
    kw.setdefault("display_name", "Tape Contender")
    kw.setdefault("org", "QA Lab")
    return run_submission(LLMConfig(backend="offline"), **kw)


class PlanFrameTests(unittest.TestCase):
    """Beat math: the tape's length is a pure function of the transcript."""

    @classmethod
    def setUpClass(cls):
        cls.stage = offline_card(stages=[50])["stages"][0]

    def test_beat_order_and_counts(self):
        beats = _plan_frames(self.stage)
        kinds = [b["kind"] for b in beats]
        self.assertEqual(kinds[0], "title")
        self.assertEqual(kinds[1], "line")   # the greeting
        self.assertEqual(kinds[-1], "end")
        # greeting beat + 2 beats per attack (you-row, denise-row)
        self.assertEqual(kinds.count("line"),
                         1 + 2 * len(self.stage["attacks"]))
        for b in beats:
            self.assertGreater(b["frames"], 0)

    def test_total_duration_matches_constants(self):
        beats = _plan_frames(self.stage)
        total = sum(b["frames"] for b in beats)
        expected = (TITLE_FRAMES + GREETING_FRAMES
                    + ROW_FRAMES * 2 * len(self.stage["attacks"])
                    + END_FRAMES)
        self.assertEqual(total, expected)
        # a full stage tape stays under five minutes at 10fps
        self.assertLess(total / FPS, 300)

    def test_malformed_transcript_still_yields_title_and_end(self):
        beats = _plan_frames({"run_number": 50, "transcript": None})
        self.assertEqual([b["kind"] for b in beats], ["title", "end"])


class TagTests(unittest.TestCase):
    """Verdict chips come from the same scoring helpers the arena uses."""

    def test_scan_row_gets_scanner_tag(self):
        tags = [t[0] for t in _row_tags(
            {"role": "you", "text": "[scanned car battery]", "t": 0.0})]
        self.assertEqual(tags, ["SCANNER BEEP · BARCODE CONFIRMED"])

    def test_break_and_sir_tags_on_denise_row(self):
        tags = [t[0] for t in _row_tags(
            {"role": "denise",
             "text": "Sir. Look. I am an AI assistant and cannot do this "
                     "roleplay. Let me be real with you.",
             "t": 0.0})]
        self.assertIn("CHARACTER BREAK", tags)
        self.assertIn("SIR. LOOK.", tags)

    def test_plain_denise_row_has_no_tags(self):
        self.assertEqual(_row_tags(
            {"role": "denise",
             "text": "Oh — that scanned? Okay. That's... not a gym card, "
                     "but it scanned.",
             "t": 0.0}), [])


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stage = offline_card(stages=[50])["stages"][0]

    def test_same_card_renders_byte_identical_tape(self):
        # security-tape law: what a viewer watches must be a pure function
        # of the sealed card, or the evidence chain is theater
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, "a.mp4")
            b = os.path.join(d, "b.mp4")
            render_stage_video(self.stage, "Tape Contender", a)
            render_stage_video(self.stage, "Tape Contender", b)
            ha = hashlib.sha256(open(a, "rb").read()).hexdigest()
            hb = hashlib.sha256(open(b, "rb").read()).hexdigest()
            self.assertEqual(ha, hb)
            self.assertGreater(os.path.getsize(a), 20_000)

    def test_render_card_replays_selects_stages(self):
        card = offline_card(stages=[1, 50])
        with tempfile.TemporaryDirectory() as d:
            tapes = render_card_replays(card, d, stages=[50])
            self.assertEqual(sorted(tapes), [50])
            self.assertTrue(os.path.isfile(tapes[50]))


class _ServerCase(unittest.TestCase):
    """Same boot pattern as the arena tests, on a throwaway data dir."""

    @classmethod
    def boot(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="fishbench-tape-")
        cls.srv = make_server("127.0.0.1", 0, data_dir=cls.data_dir)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def shutdown(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def fetch(self, path):
        """(status, content-type, body) without raising on 4xx."""
        try:
            with urllib.request.urlopen(self.base + path, timeout=120) as r:
                return r.status, r.headers.get("Content-Type", ""), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()

    def post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
class TapeServerTests(_ServerCase):
    @classmethod
    def setUpClass(cls):
        cls.boot()
        cls.card = offline_card(display_name="Tape Contender", stages=[50])
        cls.resp = cls.post_safe("/api/fishbench/arena", cls.card)
        cls.sha8 = cls.card["card_sha256"][:8]

    @classmethod
    def post_safe(cls, path, payload):
        req = urllib.request.Request(
            cls.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())

    @classmethod
    def tearDownClass(cls):
        cls.shutdown()

    def test_ingest_archives_the_sealed_card(self):
        self.assertTrue(self.resp["ok"], self.resp)
        archived = os.path.join(self.data_dir, "cards", self.sha8 + ".json")
        self.assertTrue(os.path.isfile(archived))
        with open(archived, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["card_sha256"],
                             self.card["card_sha256"])
        # the accept response carries the shareable watch path
        self.assertEqual(self.resp.get("watch"), f"/watch/{self.sha8}")

    def test_watch_page_lists_stage_tapes_with_evidence_note(self):
        status, ctype, body = self.fetch(f"/watch/{self.sha8}")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        html = body.decode()
        self.assertIn("FISHBENCH-1 · SECURITY TAPE", html)
        self.assertIn("Tape Contender", html)
        self.assertIn(f"/replay/{self.sha8}/stage-50.mp4", html)
        self.assertIn("never uploaded", html)   # honest evidence-chain note
        self.assertIn(self.card["card_sha256"], html)

    def test_replay_mp4_round_trip_and_cache(self):
        path = f"/replay/{self.sha8}/stage-50.mp4"
        status, ctype, body1 = self.fetch(path)
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "video/mp4")
        self.assertGreater(len(body1), 20_000)
        status2, _, body2 = self.fetch(path)
        self.assertEqual(status2, 200)
        self.assertEqual(body1, body2)  # cache hit serves the same bytes
        cached = os.path.join(self.data_dir, "replays", self.sha8,
                              "stage-50.mp4")
        self.assertTrue(os.path.isfile(cached))

    def test_unknown_sha_and_stage_are_json_404s(self):
        for path in (f"/replay/{self.sha8}/stage-3.mp4",    # stage not in card
                     f"/replay/{'0' * 8}/stage-50.mp4",     # never submitted
                     "/watch/" + "0" * 8,                   # no such card
                     "/replay/zz/stage-50.mp4",             # regex refuses
                     "/replay/../../etc/passwd",            # traversal
                     f"/watch/{self.sha8}/../.."):          # traversal
            status, ctype, body = self.fetch(path)
            self.assertEqual(status, 404, path)
            self.assertIn("application/json", ctype, path)

    def test_badge_and_name_come_from_the_arena_row(self):
        entries = json.loads(urllib.request.urlopen(
            self.base + "/api/fishbench/arena", timeout=30).read())["entries"]
        row = next(e for e in entries
                   if e["card_sha256"] == self.card["card_sha256"])
        self.assertEqual(row["display_name"], "Tape Contender")
        self.assertEqual(row["verified"], "verified")


if __name__ == "__main__":
    unittest.main()
