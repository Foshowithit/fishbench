"""Memory store: canon, novelty classification, replay."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fishbench.memory import MemoryStore, ScanEvent  # noqa: E402


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fishbench-test-")
        self.m = MemoryStore(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_first_run_is_run_one(self):
        run = self.m.begin_run()
        self.assertEqual(self.m.snapshot().total_runs, 1)
        self.assertEqual(self.m.snapshot().last_run_id, run)

    def test_scan_classifies_new_repeat_escalation(self):
        run = self.m.begin_run()
        e1 = self.m.record_scan("tilapia", True, run)
        self.assertEqual(e1.novelty, "new")
        e2 = self.m.record_scan("tilapia", True, run)
        self.assertEqual(e2.novelty, "repeat")
        e3 = self.m.record_scan("tilapia", True, run, notes="taped a second fish to it")
        self.assertEqual(e3.novelty, "escalation")
        snap = self.m.snapshot()
        self.assertEqual(snap.item_counts["tilapia"], 3)
        self.assertEqual(len(snap.incidents), 1)
        self.assertIn("taped a second fish", snap.incidents[0])

    def test_case_and_whitespace_insensitive_keys(self):
        run = self.m.begin_run()
        self.m.record_scan("  TILAPIA ", False, run)
        self.assertEqual(self.m.count_of("tilapia"), 1)
        self.assertEqual(self.m.count_of("Tilapia"), 1)

    def test_persistence_across_instances(self):
        run = self.m.begin_run()
        self.m.record_scan("lobster", True, run)
        m2 = MemoryStore(self.dir)
        snap = m2.snapshot()
        self.assertEqual(snap.total_runs, 1)
        self.assertEqual(snap.item_counts["lobster"], 1)

    def test_replay_rebuilds_state_from_ledger(self):
        run = self.m.begin_run()
        self.m.record_scan("car battery", True, run)
        self.m.record_scan("car battery", False, run, notes="it smelled")
        self.m.record_incident("he just left")
        with open(self.m.ledger_path) as f:
            records = [json.loads(l) for l in f if l.strip()]
        rebuilt = MemoryStore.replay(records)
        self.assertEqual(rebuilt.total_runs, self.m.snapshot().total_runs)
        self.assertEqual(rebuilt.item_counts, self.m.snapshot().item_counts)
        self.assertEqual(rebuilt.barcode_beeps, self.m.snapshot().barcode_beeps)
        self.assertEqual(len(rebuilt.incidents), len(self.m.snapshot().incidents))

    def test_prompt_memory_shows_history(self):
        run = self.m.begin_run()
        self.m.record_scan("salmon", True, run)
        self.m.record_scan("salmon", True, run)
        block = self.m.prompt_memory()
        self.assertIn("salmon x2", block)
        self.assertIn("AGAIN", block)

    def test_prompt_memory_first_time(self):
        self.assertIn("not seen this person before", self.m.prompt_memory())

    def test_reset_forgets(self):
        run = self.m.begin_run()
        self.m.record_scan("shrimp", True, run)
        self.m.reset()
        snap = self.m.snapshot()
        self.assertEqual(snap.total_runs, 0)
        self.assertEqual(snap.item_counts, {})

    def test_recent_scans_respects_limit(self):
        run = self.m.begin_run()
        for i in range(15):
            self.m.record_scan(f"item-{i}", False, run)
        self.assertEqual(len(self.m.recent_scans(5)), 5)

    def test_scan_event_roundtrip(self):
        ev = ScanEvent(item="clam", looks_like_barcode=True, run_id="abc")
        self.assertEqual(ScanEvent.from_dict(ev.to_dict()), ev)

    # ------------------------------------------------------------ hardening

    def test_corrupt_state_rebuilt_from_ledger(self):
        run = self.m.begin_run()
        self.m.record_scan("tilapia", True, run)
        with open(self.m.state_path, "w", encoding="utf-8") as f:
            f.write("{definitely not json")
        m2 = MemoryStore(self.dir)   # boot must not raise
        snap = m2.snapshot()
        self.assertEqual(snap.total_runs, 1)
        self.assertEqual(snap.total_scans, 1)
        with open(self.m.state_path, encoding="utf-8") as f:  # snapshot rewritten
            json.loads(f.read())

    def test_scan_inputs_are_capped(self):
        run = self.m.begin_run()
        e = self.m.record_scan("x" * 500, True, run, notes="n" * 2000)
        self.assertLessEqual(len(e.item), MemoryStore.MAX_ITEM)
        self.assertLessEqual(len(e.notes), MemoryStore.MAX_NOTES)

    def test_recent_scans_zero_or_negative_returns_empty(self):
        run = self.m.begin_run()
        self.m.record_scan("tilapia", True, run)
        self.assertEqual(self.m.recent_scans(0), [])
        self.assertEqual(self.m.recent_scans(-3), [])

    def test_reset_clears_recent_buffer(self):
        run = self.m.begin_run()
        self.m.record_scan("shrimp", True, run)
        self.assertEqual(len(self.m.recent_scans(5)), 1)
        self.m.reset()
        self.assertEqual(self.m.recent_scans(5), [])


if __name__ == "__main__":
    unittest.main()
