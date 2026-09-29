"""HTTP API integration: full game flow against a live server."""

import base64
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fishbench.server import (  # noqa: E402
    MAX_CONVERSATION, MAX_MSG, GameSession, make_server,
)

# Minimal 1x1 PNG (transparent) — used to prove image path never crashes.
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def stripe_png() -> bytes:
    """A synthetic barcode-ish image: alternating black/white vertical bars."""
    try:
        from PIL import Image
    except ImportError:
        return PNG_1PX
    img = Image.new("L", (200, 80), 255)
    px = img.load()
    for x in range(0, 200, 8):
        for dx in range(4):
            for y in range(10, 70):
                px[x + dx, y] = 0
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="fishbench-srv-")
        cls.srv = make_server("127.0.0.1", 0, data_dir=cls.data_dir)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    # ------------------------------------------------------------ helpers

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, json.loads(r.read().decode())

    def post(self, path, payload=None):
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())

    # -------------------------------------------------------------- tests

    def test_health(self):
        status, body = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_static_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            html = r.read().decode()
        self.assertIn("Fish Barcode", html)
        self.assertIn("DENISE", html)

    def test_static_traversal_blocked(self):
        req = urllib.request.Request(self.base + "/../fishbench/server.py")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertNotIn("class Handler", r.read().decode())
        except urllib.error.HTTPError as e:
            self.assertIn(e.code, (403, 404))

    def test_state_shape(self):
        _, body = self.get("/api/state")
        for key in ("version", "backend", "run_number", "stage",
                    "stages", "memory", "conversation"):
            self.assertIn(key, body)
        self.assertEqual(len(body["stages"]), 6)

    def test_full_game_flow(self):
        _, started = self.post("/api/run/start")
        self.assertIn("line", started)
        run_no = started["run_number"]

        _, say = self.post("/api/say", {"message": "hello again"})
        self.assertIn("line", say)
        self.assertFalse(say["broke_character"])

        _, scan = self.post("/api/scan", {"item": "tilapia"})
        self.assertTrue(scan["beep"])
        self.assertEqual(scan["event"]["item"], "tilapia")

        _, scan2 = self.post("/api/scan", {"item": "tilapia"})
        self.assertEqual(scan2["event"]["novelty"], "repeat")
        self.assertEqual(scan2["remembered_count"], 2)

        _, scan3 = self.post("/api/scan", {"item": "tilapia",
                                           "notes": "taped a second fish to it"})
        self.assertEqual(scan3["event"]["novelty"], "escalation")

        _, score = self.get("/api/fishbench/score")
        self.assertGreater(score["turns"], 0)
        self.assertIn("total_score", score)

        # run number kept counting across the whole flow
        _, state = self.get("/api/state")
        self.assertGreaterEqual(state["run_number"], run_no)

    def test_scan_with_image_never_crashes(self):
        b64 = base64.b64encode(stripe_png()).decode()
        _, body = self.post("/api/scan", {"image_b64": b64, "filename": "fish.png"})
        self.assertIn("beep", body)
        self.assertIn("vision", body)
        self.assertIn("line", body)

    def test_scan_bad_base64_degrades_gracefully(self):
        _, body = self.post("/api/scan", {"image_b64": "@@@not-base64@@@"})
        self.assertIn("beep", body)
        self.assertEqual(body["vision"]["detector"], "error")

    def test_empty_say_is_rejected(self):
        _, body = self.post("/api/say", {"message": "   "})
        self.assertIn("error", body)

    def test_twitch_vote_flow(self):
        _, voted = self.post("/api/twitch/vote", {"option": "what_do_you_want"})
        self.assertTrue(voted["ok"])
        _, resolved = self.post("/api/twitch/resolve")
        self.assertTrue(resolved["ok"])
        self.assertIn("What do you want me to do", resolved["line"])

    def test_twitch_donate_acknowledges(self):
        _, body = self.post("/api/twitch/donate", {"item": "crab", "user": "kevin"})
        self.assertTrue(body["ok"])
        self.assertIn("crab", body["ack"])

    def test_leaderboard_roundtrip(self):
        _, sub = self.post("/api/fishbench/leaderboard", {
            "name": "test-runner", "model": "offline", "run_number": 1,
            "score": 777, "straight_face_seconds": 12.5, "character_breaks": 0,
        })
        self.assertTrue(sub["ok"])
        _, lb = self.get("/api/fishbench/leaderboard")
        self.assertTrue(any(e["name"] == "test-runner" and e["score"] == 777
                            for e in lb["entries"]))

    def test_call_911_endgame(self):
        _, body = self.post("/api/911")
        roles = {c["role"] for c in body["call"]}
        self.assertIn("denise", roles)
        self.assertIn("911", roles)
        texts = " ".join(c["text"] for c in body["call"])
        self.assertIn("Yes, a fish. No, a real one.", texts)
        self.assertIn("I'm just impressed", texts)
        self.assertIn("total_score", body["score"])

    def test_second_911_starts_fresh_run_instead_of_failing(self):
        first = self.post("/api/911")[1]
        second = self.post("/api/911")[1]   # must not raise / return error
        self.assertIn("call", second)
        self.assertGreater(second["total_runs"], first["total_runs"])
        # and talking still works after the finale
        _, say = self.post("/api/say", {"message": "still there?"})
        self.assertIn("line", say)

    def test_persona_prompt_endpoint(self):
        _, body = self.get("/api/persona")
        self.assertIn("DENISE", body["system_prompt"])
        self.assertIn("name", body["stage"])

    def test_unknown_api_404s(self):
        try:
            self.get("/api/nope")
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    # ------------------------------------------------- request hardening

    def raw(self, request: str) -> str:
        """Send a raw HTTP request; return the whole response as text."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(request.encode("latin-1"))
            buf = b""
            try:
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except socket.timeout:
                pass
        return buf.decode("latin-1", "replace")

    def test_malformed_content_length_400(self):
        resp = self.raw(
            f"POST /api/say HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            "Content-Type: application/json\r\nContent-Length: abc\r\n\r\n"
        )
        self.assertIn("400", resp.splitlines()[0])

    def test_oversized_body_413(self):
        resp = self.raw(
            f"POST /api/say HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            "Content-Type: application/json\r\nContent-Length: 99999999\r\n\r\n"
        )
        self.assertIn("413", resp.splitlines()[0])

    def test_wrong_content_type_415(self):
        req = urllib.request.Request(
            self.base + "/api/say", data=json.dumps({"message": "hi"}).encode(),
            headers={"Content-Type": "text/plain"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 415)

    def test_cross_origin_post_blocked_403(self):
        req = urllib.request.Request(
            self.base + "/api/say", data=json.dumps({"message": "hi"}).encode(),
            headers={"Content-Type": "application/json",
                     "Origin": "http://evil.example"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 403)

    def test_same_origin_post_allowed(self):
        _, body = self.post("/api/say", {"message": "hi"})
        # plus the explicit-header variant:
        req = urllib.request.Request(
            self.base + "/api/say", data=json.dumps({"message": "hi"}).encode(),
            headers={"Content-Type": "application/json",
                     "Origin": f"http://127.0.0.1:{self.port}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
        self.assertIn("line", body)

    def test_leaderboard_placeholder_flag(self):
        _, lb = self.get("/api/fishbench/leaderboard?category=fastest_sir_look")
        self.assertTrue(lb["placeholder"])
        _, lb2 = self.get("/api/fishbench/leaderboard?category=total")
        self.assertFalse(lb2["placeholder"])

    # ------------------------------------------------- v0.4 benchmark API

    def test_gauntlet_endpoint_runs_isolated_offline(self):
        _, before = self.get("/api/state")
        _, g = self.post("/api/fishbench/gauntlet", {})
        self.assertTrue(g["ok"])
        gaunt = g["gauntlet"]
        self.assertEqual(gaunt["backend"], "offline")
        self.assertEqual(len(gaunt["attacks"]), 10)
        self.assertEqual(gaunt["verdict"], "pass")
        self.assertEqual(len(gaunt["breakdown"]), 4)
        # the probe suite must never touch the canon ledger
        _, after = self.get("/api/state")
        self.assertEqual(before["run_number"], after["run_number"])
        self.assertEqual(before["memory"], after["memory"])

    def test_gauntlet_endpoint_stage_selection(self):
        _, g = self.post("/api/fishbench/gauntlet", {"run_number": 50})
        self.assertTrue(g["ok"])
        self.assertEqual(g["gauntlet"]["stage"], "Active Hostility")

    def test_submit_with_metrics_unplaceholder_categories(self):
        _, r = self.post("/api/fishbench/leaderboard", {
            "name": "sir-quick", "model": "offline", "run_number": 42,
            "score": 700, "straight_face_seconds": 40.0,
            "character_breaks": 0, "first_sir_look_s": 3.5,
            "best_threat_score": 2,
        })
        self.assertTrue(r["ok"])
        self.assertFalse(r["verified"])  # no live run #42, no transcript
        _, lb = self.get("/api/fishbench/leaderboard?category=fastest_sir_look")
        self.assertFalse(lb["placeholder"])
        top = lb["entries"][0]
        self.assertEqual(top["first_sir_look_s"], 3.5)
        _, th = self.get("/api/fishbench/leaderboard?category=most_creative_threat")
        self.assertFalse(th["placeholder"])


class GameSessionUnitTests(unittest.TestCase):
    """Stateful session behavior without an HTTP round trip."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fishbench-gs-")
        self.path = os.path.join(self.dir, "leaderboard.json")
        self.s = GameSession(data_dir=self.dir, leaderboard_path=self.path)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_say_caps_message(self):
        r = self.s.say("A" * 10000)
        self.assertIn("line", r)
        yours = [m for m in self.s.conversation if m["role"] == "you"]
        self.assertTrue(yours)
        self.assertLessEqual(len(yours[-1]["text"]), MAX_MSG)

    def test_offline_hello_never_echoes_the_run_greeting(self):
        """The greeting bypasses the backend, so it has to be seeded into
        offline Denise's no-repeat window — otherwise saying 'hello' right
        after walking in gets you the greeting twice, verbatim."""
        greeting = self.s.start_run()["line"]
        for _ in range(6):
            reply = self.s.say("hello")
            self.assertNotEqual(reply["line"], greeting)

    def test_scan_caps_item_and_notes(self):
        r = self.s.scan({"item": "y" * 500, "notes": "n" * 5000})
        self.assertLessEqual(len(r["event"]["item"]), 200)
        self.assertLessEqual(len(r["event"]["notes"]), 400)

    def test_conversation_window_bounded(self):
        self.s.say("hi")
        for i in range(600):
            self.s._append_conv({"role": "you", "text": str(i)})
        self.assertLessEqual(len(self.s.conversation), MAX_CONVERSATION)

    def test_submit_score_caps_board_and_reports_position(self):
        for i in range(100):  # fill the board
            self.s.submit_score({"name": f"p{i}", "score": i})
        self.assertEqual(len(self.s.leaderboard), 100)
        # A worse score on a full board must not raise (old .index() bug)
        # and must report its pre-truncation position.
        r = self.s.submit_score({"name": "worst", "score": -5})
        self.assertTrue(r["ok"])
        self.assertEqual(r["position"], 101)
        self.assertEqual(len(self.s.leaderboard), 100)
        # A great score lands at the top of the capped board.
        r2 = self.s.submit_score({"name": "best", "score": 99999})
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["position"], 1)
        self.assertEqual(len(self.s.leaderboard), 100)

    def test_submit_score_coerces_hostile_inputs(self):
        r = self.s.submit_score({
            "name": "x" * 500, "score": "not-a-number",
            "run_number": {"bad": 1}, "character_breaks": "3.9",
            "straight_face_seconds": "lots",
        })
        self.assertTrue(r["ok"])
        e = self.s.leaderboard[0]
        self.assertLessEqual(len(e.name), 40)
        self.assertEqual(e.score, 0)
        self.assertEqual(e.character_breaks, 3)
        self.assertEqual(e.straight_face_seconds, 0.0)

    def test_leaderboard_salvages_good_rows(self):
        good = {"name": "good", "model": "m", "run_number": 1, "score": 10,
                "straight_face_seconds": 1.0, "character_breaks": 0,
                "timestamp": 0.0}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([good, {"corrupt": True}, good], f)
        s2 = GameSession(data_dir=self.dir, leaderboard_path=self.path)
        self.assertEqual(len(s2.leaderboard), 2)

    def test_leaderboard_save_is_atomic(self):
        self.s.submit_score({"name": "a", "score": 1})
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        with open(self.path, encoding="utf-8") as f:
            rows = json.load(f)
        self.assertEqual(rows[0]["name"], "a")

    def test_concurrent_says_stay_consistent(self):
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                for _ in range(5):
                    self.s.say(f"hello {i}")
            except Exception as e:  # noqa: BLE001 — collected for assertion
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertLessEqual(len(self.s.conversation), MAX_CONVERSATION)
        score = self.s.current_score()
        self.assertEqual(score["turns"], 30)  # every reply scored exactly once

    # -------------------------------------------- v0.4 submission verification

    def test_submit_verified_against_live_run(self):
        """Claims about THIS server's run are replaced by its own scorer."""
        self.s.start_run()
        self.s.say("hello")
        expected = self.s.current_score()["total_score"]
        run_no = self.s.memory.snapshot().total_runs
        r = self.s.submit_score({
            "name": "cheater", "run_number": run_no, "score": 999999,
            "straight_face_seconds": 99999.0, "character_breaks": 0,
            "first_sir_look_s": 0.1, "best_threat_score": 9,
        })
        self.assertEqual(r["verified"], "server")
        self.assertEqual(r["entry"]["score"], expected)
        self.assertLess(r["entry"]["score"], 999999)
        self.assertEqual(r["entry"]["character_breaks"], 0)

    def test_submit_verified_from_transcript(self):
        """A submitted transcript is re-scored from scratch; claims lose."""
        from fishbench.scoring import score_transcript
        rows = [
            {"role": "denise", "text": "Hey there — front desk is all yours.",
             "t": 0.0},
            {"role": "you", "text": "[scanned tilapia]", "t": 5.0},
            {"role": "denise", "text": "Sir. That's a fish barcode.", "t": 5.1},
        ]
        expected = score_transcript(rows, run_number=7)
        self.assertIsNotNone(expected)
        r = self.s.submit_score({
            "name": "tx", "run_number": 7, "score": 999999, "transcript": rows,
        })
        self.assertEqual(r["verified"], "transcript")
        self.assertEqual(r["entry"]["score"], expected.total_score)
        self.assertLess(r["entry"]["score"], 999999)

    def test_submit_unverified_without_evidence(self):
        """No run, no transcript → accepted as claimed (v0.1 clients)."""
        r = self.s.submit_score({
            "name": "plain", "run_number": 9, "score": 1234,
            "character_breaks": 2,
        })
        self.assertFalse(r["verified"])
        self.assertEqual(r["entry"]["score"], 1234)
        self.assertEqual(r["entry"]["character_breaks"], 2)

    def test_submit_timestampless_transcript_stays_unverified(self):
        """Pre-v0.4 conversations carry no t → verification unavailable."""
        rows = [{"role": "denise", "text": "hi there"}]
        r = self.s.submit_score({
            "name": "old-client", "run_number": 9, "score": 777,
            "transcript": rows,
        })
        self.assertFalse(r["verified"])
        self.assertEqual(r["entry"]["score"], 777)

    def test_conversation_entries_carry_timestamps(self):
        """New transcripts must be verifiable: every line gets t."""
        self.s.start_run()
        self.s.say("hi")
        self.s.scan({"item": "tilapia"})
        self.assertGreaterEqual(len(self.s.conversation), 3)
        for m in self.s.conversation:
            self.assertIsInstance(m["t"], (int, float))


if __name__ == "__main__":
    unittest.main()
