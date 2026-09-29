"""FishBench-2 TANK — scoring freeze, seal/verify honesty, arena ingest.

The scoring tests are pure: synthetic measurements through the real
tankspec functions prove the FAIL paths (blank tank, noisy boot, thrown
scene, half the fish, frozen windows) land where the spec says. The card
tests seal synthetic stage cards — no chromium — and prove verify_tank_card
catches re-sealed lies (the strong attack: edit, then re-hash). The arena
tests ingest onto a live server: honest rows land verified and ranked
within their own spec, tampered rows are badged, identity is per-spec.
The capture smoke is last and skipped wherever playwright/chromium are
absent so the suite stays fast and dependency-free.
"""

import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fishbench import tankspec  # noqa: E402
from fishbench.server import make_server  # noqa: E402
from fishbench.tank import seal_card, verify_tank_card  # noqa: E402

# Frozen 2026-09-29 spec — any change to briefs or scoring moves these and
# every submitted card would be refused. That refusal is the point.
BRIEF_PACK_SHA = "e3367c98e167468391fd516bf57265ae875c2abcf411c678696981c0d9550e51"
SCORING_SHA = "6e80d1965d0878676fcc127d8e1a892821afa8a1d3cf6c58afd885b1edac88ff"


# ------------------------------------------------------------ synth cards

def synth_stage(run: int, n: int, **kw) -> dict:
    """A stage card with honest, hand-set measurements — no chromium.

    Defaults describe a perfect stage: exact fish count, coverage at the
    calibration target, every window alive, 60fps. Every FAIL path is a
    kw away.
    """
    est = float(kw.pop("est", n))
    cov = kw.pop("cov", tankspec.cov_target(n))
    alive = kw.pop("alive", tankspec.WINDOWS)
    windows = kw.pop("windows", tankspec.WINDOWS)
    fps = kw.pop("fps", 60.0)
    console = kw.pop("console", 0)
    page = kw.pop("page", 0)
    drew = kw.pop("canvas_drew", True)
    crash = kw.pop("crash", False)
    source = kw.pop("source", f"<!doctype html><!-- synthetic tank n={n} -->")
    m = {
        "console_errors": console, "page_errors": page,
        "canvas_drew": drew, "fish_estimate": est,
        "motion_coverage": cov, "windows_alive": alive,
        "windows_total": windows, "fps": fps, "tank_crash": crash,
    }
    return {
        "run_number": run, "fish_requested": n,
        "tankscore": tankspec.stage_tankscore(m, n),
        "categories": tankspec.category_scores(m, n),
        "measurements": m, "tank_crash": crash,
        "generation": {"lane": "synthetic", "latency_ms": 0},
        "source_bytes": len(source.encode("utf-8")),
        "source_sha256": hashlib.sha256(
            source.encode("utf-8")).hexdigest(),
        "source": source,
        "tape": {"file": None, "sha256": None, "frames": 0,
                 "duration_s": 0.0, "sample_hz": tankspec.SAMPLE_HZ,
                 "error": "no tape in unit test"},
    }


def synth_card(stages=None, **kw) -> dict:
    """A sealed fishbench-2 card built from synthetic stages."""
    stages = stages if stages is not None else [
        synth_stage(i + 1, n) for i, n in enumerate(tankspec.STAGES)]
    composite = tankspec.aggregate(stages)
    composite["fish_requested_total"] = sum(
        s["fish_requested"] for s in stages)
    composite["stages_total"] = len(stages)
    card = {
        "spec": tankspec.SPEC_ID,
        "spec_digest": tankspec.spec_digest(),
        "harness": "unit-test",
        "model": kw.pop("model", "synthetic-model"),
        "display_name": kw.pop("display_name", "Synthetic Tank"),
        "org": kw.pop("org", "QA Lab"),
        "backend": kw.pop("backend", "synthetic"),
        "base_url_host": kw.pop("base_url_host", ""),
        "temperature": tankspec.TEMPERATURE,
        "run_seconds": tankspec.RUN_SECONDS,
        "sample_hz": tankspec.SAMPLE_HZ,
        "stages": stages,
        "composite": composite,
    }
    card.update(kw)
    return seal_card(card)


# ------------------------------------------------------------ scoring

class ScoringTests(unittest.TestCase):
    """Pure tankspec scoring — every FAIL path, exact numbers."""

    def test_spec_frozen(self):
        self.assertEqual(tankspec.SPEC_ID, "fishbench-2")
        self.assertEqual(tankspec.SPEC_DATE, "2026-09-29")
        self.assertEqual(tankspec.brief_pack_digest(), BRIEF_PACK_SHA)
        self.assertEqual(tankspec.scoring_digest(), SCORING_SHA)
        self.assertEqual(tankspec.STAGES, (1, 5, 20, 50, 100, 200))

    def test_perfect_stage_scores_1000(self):
        m = {"console_errors": 0, "page_errors": 0, "canvas_drew": True,
             "fish_estimate": 5, "motion_coverage": tankspec.cov_target(5),
             "windows_alive": tankspec.WINDOWS,
             "windows_total": tankspec.WINDOWS, "fps": 60.0}
        cats = tankspec.category_scores(m, 5)
        self.assertEqual(cats, {"clean_boot": 100.0, "fish_on_screen": 100.0,
                                "sustained_swimming": 100.0,
                                "fps_at_scale": 100.0})
        self.assertEqual(tankspec.stage_tankscore(m, 5), 1000.0)

    def test_never_drew_frame(self):
        # No canvas: three categories zero out; only window evidence
        # (sustained swimming) can still score — that's by design.
        m = {"console_errors": 0, "page_errors": 0, "canvas_drew": False,
             "fish_estimate": 5, "motion_coverage": 0.5,
             "windows_alive": 8, "windows_total": 8, "fps": 60.0}
        self.assertEqual(tankspec.category_scores(m, 5),
                         {"clean_boot": 0.0, "fish_on_screen": 0.0,
                          "sustained_swimming": 100.0,
                          "fps_at_scale": 0.0})
        self.assertEqual(tankspec.stage_tankscore(m, 5), 250.0)

    def test_console_and_page_errors(self):
        def m(**kw):
            base = {"console_errors": 0, "page_errors": 0,
                    "canvas_drew": True, "fish_estimate": 5,
                    "motion_coverage": tankspec.cov_target(5),
                    "windows_alive": 8, "windows_total": 8, "fps": 60.0}
            base.update(kw)
            return base

        # one console error: clean_boot 90, tankscore −5 → exactly 970
        self.assertEqual(tankspec.clean_boot(m(console_errors=1)), 90.0)
        self.assertEqual(tankspec.stage_tankscore(m(console_errors=1),
                                                  5), 970.0)
        # one page error: clean_boot 65, tankscore −25 → exactly 887.5
        self.assertEqual(tankspec.clean_boot(m(page_errors=1)), 65.0)
        self.assertEqual(tankspec.stage_tankscore(m(page_errors=1),
                                                  5), 887.5)

    def test_tankscore_never_goes_negative(self):
        m = {"console_errors": 500, "page_errors": 500, "canvas_drew": True,
             "fish_estimate": 5, "motion_coverage": 0,
             "windows_alive": 0, "windows_total": 8, "fps": 0.0}
        self.assertEqual(tankspec.stage_tankscore(m, 5), 0.0)

    def test_count_split_uses_est_below_coverage_above(self):
        good = {"console_errors": 0, "page_errors": 0, "canvas_drew": True,
                "motion_coverage": 0.0, "windows_alive": 8,
                "windows_total": 8, "fps": 60.0}
        # n=20 (== COUNT_SPLIT): counted path — half the fish = 50
        half = dict(good, fish_estimate=10)
        self.assertEqual(tankspec.fish_on_screen(half, 20), 50.0)
        # n=50: coverage path — half the target coverage = 50
        cov = dict(good, fish_estimate=50,
                   motion_coverage=tankspec.cov_target(50) / 2)
        self.assertEqual(tankspec.fish_on_screen(cov, 50), 50.0)
        # overshooting coverage can't exceed the cap
        fat = dict(good, fish_estimate=50,
                   motion_coverage=tankspec.cov_target(50) * 10)
        self.assertEqual(tankspec.fish_on_screen(fat, 50), 100.0)
        # the cov target itself is capped (0.0007/fish; cap at n≥643)
        self.assertAlmostEqual(tankspec.cov_target(100), 0.07)
        self.assertEqual(tankspec.cov_target(1000), tankspec.COV_CAP)

    def test_sustained_swimming_windows(self):
        base = {"console_errors": 0, "page_errors": 0, "canvas_drew": True,
                "fish_estimate": 5, "motion_coverage": 0,
                "windows_total": 8, "fps": 60.0}
        self.assertEqual(
            tankspec.sustained_swimming(dict(base, windows_alive=4)), 50.0)
        self.assertEqual(
            tankspec.sustained_swimming(dict(base, windows_alive=8)), 100.0)
        self.assertEqual(
            tankspec.sustained_swimming(dict(base, windows_alive=8,
                                             windows_total=0)), 0.0)

    def test_fps_at_scale_curve(self):
        base = {"console_errors": 0, "page_errors": 0, "canvas_drew": True,
                "fish_estimate": 5, "motion_coverage": 0,
                "windows_alive": 8, "windows_total": 8}
        self.assertEqual(tankspec.fps_at_scale(
            dict(base, fps=6.25)), 50.0)
        self.assertEqual(tankspec.fps_at_scale(dict(base, fps=60.0)), 100.0)

    def test_aggregate_means_and_totals(self):
        perfect = synth_stage(1, 5)
        wrecked = synth_stage(2, 5, console=1, page=1, alive=0, fps=0.0)
        agg = tankspec.aggregate([perfect, wrecked])
        want = round((perfect["tankscore"] + wrecked["tankscore"]) / 2, 2)
        self.assertEqual(agg["tankscore"], want)
        self.assertEqual(agg["tank_crashes"], 0)
        self.assertEqual(agg["console_errors_total"], 1)
        self.assertEqual(agg["page_errors_total"], 1)
        self.assertEqual(agg["stages"], {"5": wrecked["tankscore"]})
        # category means, not sums
        self.assertEqual(agg["categories"]["clean_boot"], 77.5)  # (100+65)/2

    def test_brief_pack_counts_every_fish(self):
        # The brief must ask for exactly N — the whole stage is that number.
        for n in tankspec.STAGES:
            self.assertIn(f"EXACTLY {n} fish", tankspec.build_brief(n))


# ------------------------------------------------------------ seal + verify

class CardSealTests(unittest.TestCase):
    """Seal → verify round-trip, and lies caught even after re-sealing."""

    def test_honest_card_verifies(self):
        card = synth_card()
        verdict = verify_tank_card(card)
        self.assertEqual(verdict["verified"], "verified")
        failed = [c["check"] for c in verdict["checks"] if not c["ok"]]
        self.assertEqual(failed, [])
        self.assertEqual(card["spec_digest"]["brief_pack_sha256"],
                         BRIEF_PACK_SHA)

    def test_resealed_tankscore_lie_caught(self):
        card = synth_card()
        card["stages"][0]["tankscore"] += 40  # a flattering stage
        lied = seal_card(card)  # re-hash: the seal itself is now valid
        verdict = verify_tank_card(lied)
        self.assertEqual(verdict["verified"], "unverified")
        failed = {c["check"] for c in verdict["checks"] if not c["ok"]}
        self.assertIn("stage_1_rescore", failed)

    def test_resealed_source_swap_caught(self):
        card = synth_card()
        card["stages"][2]["source"] += "\n<!-- touched after the fact -->"
        lied = seal_card(card)
        verdict = verify_tank_card(lied)
        self.assertEqual(verdict["verified"], "unverified")
        failed = {c["check"] for c in verdict["checks"] if not c["ok"]}
        self.assertIn("stage_3_source", failed)  # sha256 no longer matches

    def test_missing_measurement_key_caught(self):
        card = synth_card()
        del card["stages"][0]["measurements"]["fps"]
        lied = seal_card(card)
        verdict = verify_tank_card(lied)
        self.assertEqual(verdict["verified"], "unverified")
        failed = {c["check"] for c in verdict["checks"] if not c["ok"]}
        self.assertIn("stage_1_measurements", failed)

    def test_composite_lie_caught(self):
        card = synth_card()
        card["composite"]["tankscore"] += 30
        lied = seal_card(card)
        verdict = verify_tank_card(lied)
        self.assertEqual(verdict["verified"], "unverified")
        failed = {c["check"] for c in verdict["checks"] if not c["ok"]}
        self.assertIn("composite", failed)

    def test_stale_spec_digest_flagged(self):
        card = synth_card()
        card["spec_digest"]["brief_pack_sha256"] = "0" * 64
        verdict = verify_tank_card(seal_card(card))
        failed = {c["check"] for c in verdict["checks"] if not c["ok"]}
        self.assertIn("brief_pack", failed)

    def test_tampered_seal_without_reseal_caught(self):
        card = synth_card()
        card["stages"][0]["tankscore"] += 40  # seal NOT refreshed
        verdict = verify_tank_card(card)
        failed = {c["check"] for c in verdict["checks"] if not c["ok"]}
        self.assertIn("seal", failed)
        self.assertEqual(verdict["verified"], "unverified")


# ------------------------------------------------------------ arena ingest

class TankArenaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = tempfile.mkdtemp(prefix="fishbench-tank-arena-")
        cls.srv = make_server("127.0.0.1", 0, data_dir=cls.data_dir)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read().decode())

    def tank_entries(self):
        for b in self.get("/api/fishbench/arena")["specs"]:
            if b["spec"] == tankspec.SPEC_ID:
                return b["entries"]
        return []

    def test_honest_tank_card_lands_verified(self):
        card = synth_card(display_name="Honest Tank", org="QA Lab")
        r = self.post("/api/fishbench/arena", card)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["verified"], "verified")
        self.assertEqual(r["checks_failed"], [])
        rows = [e for e in self.tank_entries()
                if e["display_name"] == "Honest Tank"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["metric"], "TankScore")
        self.assertEqual(rows[0]["score"], card["composite"]["tankscore"])
        self.assertEqual(rows[0]["verified"], "verified")
        self.assertEqual(rows[0]["fish_requested_total"],
                         sum(tankspec.STAGES))

    def test_tank_row_never_touches_fb1_board(self):
        before = [e["display_name"]
                  for e in self.get("/api/fishbench/arena")["entries"]]
        self.post("/api/fishbench/arena",
                  synth_card(display_name="Board Isolation"))
        after = [e["display_name"]
                 for e in self.get("/api/fishbench/arena")["entries"]]
        self.assertEqual(after, before)  # fb-1 legacy view unchanged

    def test_tampered_card_lands_badged_not_refused(self):
        card = synth_card(display_name="Flattering Tank")
        card["stages"][0]["tankscore"] += 40
        r = self.post("/api/fishbench/arena", seal_card(card))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["verified"], "unverified")
        self.assertIn("stage_1_rescore", r["checks_failed"])
        row = next(e for e in self.tank_entries()
                   if e["display_name"] == "Flattering Tank")
        self.assertEqual(row["verified"], "unverified")

    def test_wrong_digest_refused(self):
        card = synth_card(display_name="Stale Spec Tank")
        card["spec_digest"]["scoring_sha256"] = "0" * 64
        r = self.post("/api/fishbench/arena", seal_card(card))
        self.assertFalse(r["ok"])
        self.assertIn("tank", r["error"])

    def test_keep_if_lower_identity_per_spec(self):
        # first card (one noisy stage → 985, beatable)
        self.post("/api/fishbench/arena",
                  synth_card(display_name="Improver", stages=[
                      synth_stage(1, 5, console=1),
                      synth_stage(2, 5)]))
        first = next(e for e in self.tank_entries()
                     if e["display_name"] == "Improver")
        # a WORSE resubmission is politely kept out
        worse = synth_card(display_name="Improver", stages=[
            synth_stage(1, 5, console=2, page=1),
            synth_stage(2, 5, console=2, page=1)])
        r = self.post("/api/fishbench/arena", worse)
        self.assertTrue(r["ok"])
        self.assertIn("kept", json.dumps(r))
        still = next(e for e in self.tank_entries()
                     if e["display_name"] == "Improver")
        self.assertEqual(still["card_sha256"], first["card_sha256"])
        # a BETTER resubmission replaces the row
        better = synth_card(display_name="Improver", stages=[
            synth_stage(1, 5), synth_stage(2, 5), synth_stage(3, 5)])
        r2 = self.post("/api/fishbench/arena", better)
        self.assertTrue(r2["ok"])
        self.assertNotIn("kept", json.dumps(r2))
        now = next(e for e in self.tank_entries()
                   if e["display_name"] == "Improver")
        self.assertEqual(now["card_sha256"], better["card_sha256"])

    def test_tape_hash_gate(self):
        card = synth_card(display_name="Tape Gated",
                          stages=[synth_stage(1, 5)])
        self.post("/api/fishbench/arena", card)
        sha8 = card["card_sha256"][:8]
        # no tape deposited → honest nothing
        self.assertIsNone(self.srv.session.ensure_replay(sha8, 1))
        # deposit a real file whose sha matches the manifest → served
        blob = b"fake-mp4-bytes-for-the-gate-test"
        want = hashlib.sha256(blob).hexdigest()
        card["stages"][0]["tape"]["sha256"] = want
        relid = seal_card(card)
        self.post("/api/fishbench/arena", relid)
        sha8 = relid["card_sha256"][:8]
        tape_dir = os.path.join(self.data_dir, "replays", sha8)
        os.makedirs(tape_dir, exist_ok=True)
        with open(os.path.join(tape_dir, "stage-1.mp4"), "wb") as f:
            f.write(blob)
        self.assertEqual(
            self.srv.session.ensure_replay(sha8, 1),
            os.path.join(tape_dir, "stage-1.mp4"))
        # tamper one byte on disk → the gate refuses to serve it
        with open(os.path.join(tape_dir, "stage-1.mp4"), "wb") as f:
            f.write(blob + b"x")
        self.assertIsNone(self.srv.session.ensure_replay(sha8, 1))

    def test_mixed_spec_compare_refused(self):
        fb1 = [e for e in self.get("/api/fishbench/arena")["entries"]
               if e.get("card_sha256")][:1]
        self.assertTrue(fb1, "offline Denise baseline should be seeded")
        tank_sha = next(e for e in self.tank_entries()
                        if e.get("card_sha256"))["card_sha256"][:8]
        page = self.srv.session.compare_page(fb1[0]["card_sha256"][:8],
                                             tank_sha)
        self.assertIn("no scale", page)  # honest refusal, not a fake duel

    def test_recovery_from_archived_cards(self):
        # Reboot on the same data dir: every archived card returns to the
        # board without chromium (pure seal + re-score).
        names_before = sorted(e["display_name"]
                              for e in self.tank_entries())
        srv2 = make_server("127.0.0.1", 0, data_dir=self.data_dir)
        try:
            names_after = sorted(
                e["display_name"] for e in srv2.session.arena
                if e.get("spec") == tankspec.SPEC_ID)
            self.assertEqual(names_after, names_before)
        finally:
            # never serve_forever'd → shutdown() would block forever on the
            # initially-unset __is_shut_down event; closing the socket is the
            # complete cleanup for a server that only handled direct calls.
            srv2.server_close()


# ------------------------------------------------------------ capture smoke

def _chromium_ready() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:
        return False


@unittest.skipUnless(_chromium_ready(),
                     "playwright + chromium not installed")
class CaptureSmokeTests(unittest.TestCase):
    """Two filmed stages through the real headless pipeline (short runs).

    tank_good must boot clean, count its fish, and stay alive; tank_broken
    must be caught throwing with dead windows and scored into the ground.
    """

    @classmethod
    def setUpClass(cls):
        from fishbench.capture import TankCapture
        from fishbench.tank import _load_fixture
        cls.TankCapture = TankCapture
        cls.good = _load_fixture("tank_good.html", 5)
        cls.broken = _load_fixture("tank_broken.html", 5)

    def _run(self, html):
        work = tempfile.mkdtemp(prefix="tank-smoke-")
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        cap = self.TankCapture(html=html, workdir=work,
                               run_seconds=2.0).run()
        return cap

    def test_good_tank_boots_and_lives(self):
        cap = self._run(self.good)
        m = cap.measurements(5)
        self.assertTrue(m["canvas_drew"])
        self.assertEqual(m["page_errors"], 0)
        self.assertGreaterEqual(m["fish_estimate"], 3)
        # one startup window may register no motion when the machine is
        # loaded (foreign encodes) — the tank must still be ~fully alive
        self.assertGreaterEqual(m["windows_alive"], m["windows_total"] - 1)
        self.assertGreater(m["fps"], 0.0)
        self.assertGreaterEqual(
            tankspec.stage_tankscore(m, 5), 700.0)

    def test_broken_tank_is_crushed(self):
        cap = self._run(self.broken)
        m = cap.measurements(5)
        self.assertGreaterEqual(m["page_errors"], 1)  # the loop threw
        self.assertLess(m["windows_alive"], m["windows_total"])
        self.assertLess(tankspec.stage_tankscore(m, 5), 500.0)


if __name__ == "__main__":
    unittest.main()
