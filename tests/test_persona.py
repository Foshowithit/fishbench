"""Persona stages + system prompt composition."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fishbench.memory import MemoryStore  # noqa: E402
from fishbench.persona import (  # noqa: E402
    RELATIONSHIP_STAGES, Persona, THE_LINE_BOSS, THE_LINE_NO,
    stage_for_run,
)


class StageTests(unittest.TestCase):
    def test_stage_boundaries(self):
        cases = {
            1: "Polite Confusion",
            4: "Polite Confusion",
            5: "Sarcasm",
            19: "Sarcasm",
            20: "Exhausted Resignation",
            49: "Exhausted Resignation",
            50: "Active Hostility",
            99: "Active Hostility",
            100: "Complicit",
            199: "Complicit",
            200: "Final Boss",
            999: "Final Boss",
        }
        for run, name in cases.items():
            self.assertEqual(stage_for_run(run).name, name, f"run {run}")

    def test_below_one_clamps_to_first_stage(self):
        self.assertEqual(stage_for_run(0).name, "Polite Confusion")
        self.assertEqual(stage_for_run(-5).name, "Polite Confusion")

    def test_stages_are_ordered_and_six(self):
        self.assertEqual(len(RELATIONSHIP_STAGES), 6)
        runs = [s.min_run for s in RELATIONSHIP_STAGES]
        self.assertEqual(runs, sorted(runs))
        self.assertEqual(runs[0], 1)

    def test_every_stage_has_the_required_lines(self):
        for s in RELATIONSHIP_STAGES:
            for attr in ("greeting", "on_barcode", "on_repeat", "brief"):
                self.assertTrue(getattr(s, attr).strip(), f"{s.name}.{attr}")

    def test_canonical_clip_lines_exist(self):
        self.assertIn("I know what you're gonna do", THE_LINE_NO)
        self.assertIn("I kept the fish", THE_LINE_BOSS)

    def test_pools_lead_with_the_canonical_line(self):
        """Rotation may add lines, but the first line of every pool is the
        canonical one — prompts, docs, and first-seen behavior stay put."""
        for s in RELATIONSHIP_STAGES:
            self.assertEqual(s.pool("greeting")[0], s.greeting, s.name)
            self.assertEqual(s.pool("beep")[0], s.on_barcode, s.name)
            self.assertEqual(s.pool("repeat")[0], s.on_repeat, s.name)

    def test_pools_are_large_enough_for_the_no_repeat_window(self):
        """OfflineBackend.HISTORY must stay below the smallest pool×beats
        combination count, or rotation would have to repeat inside the
        no-repeat window."""
        from fishbench.llm import OfflineBackend
        for s in RELATIONSHIP_STAGES:
            for kind in ("greeting", "beep", "repeat", "deflect", "ask", "beat"):
                pool = s.pool(kind)
                self.assertGreaterEqual(len(pool), 3, f"{s.name}.{kind}")
                self.assertEqual(len(pool), len(set(pool)),
                                 f"{s.name}.{kind} has duplicates")
            combos = len(s.pool("greeting")) * (1 + len(s.pool("beat")))
            self.assertGreater(
                combos, OfflineBackend.HISTORY,
                f"{s.name}: {combos} combos cannot outspend the window",
            )

    def test_stage_pools_do_not_share_lines(self):
        """Stage escalation must be audible: no line may exist in two stages."""
        for kind in ("greeting", "beep", "repeat", "deflect"):
            seen: dict[str, str] = {}
            for s in RELATIONSHIP_STAGES:
                for line in s.pool(kind):
                    self.assertNotIn(
                        line, seen,
                        f"{kind}: {line!r} claimed by {seen.get(line)} and {s.name}",
                    )
                    seen[line] = s.name

    def test_escalation_tones_are_distinguishable(self):
        """Runs 1 / 20 / 100 / 200 must be obviously different stages even
        with the stage label stripped — offline lines only."""
        lines = {}
        for run in (1, 20, 100, 200):
            with tempfile.TemporaryDirectory(prefix="fishbench-tone-") as d:
                mem = MemoryStore(d)
                for _ in range(run):
                    mem.begin_run()
                lines[run] = Persona(mem).stage_line("greeting")
        self.assertEqual(len(set(lines.values())), 4, lines)


class PersonaPromptTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fishbench-persona-")
        self.memory = MemoryStore(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_prompt_on_first_run(self):
        p = Persona(self.memory)
        prompt = p.system_prompt()
        self.assertIn("DENISE", prompt)
        self.assertIn("run #0", prompt)  # no runs yet
        self.assertIn("not seen this person before", prompt)

    def test_prompt_carries_memory_and_stage(self):
        self.memory.begin_run()
        run = self.memory.begin_run()
        self.memory.record_scan("tilapia", True, run)
        prompt = Persona(self.memory).system_prompt()
        self.assertIn("tilapia", prompt)
        # run 2 is below 5, so stage 1 — assert what's true:
        self.assertIn("Polite Confusion", prompt)
        self.assertIn("BEHAVIOR", prompt)

    def test_stage_advances_with_runs(self):
        for _ in range(50):
            self.memory.begin_run()
        self.assertEqual(Persona(self.memory).stage.name, "Active Hostility")

    def test_greeting_changes_with_stage(self):
        p1 = Persona(self.memory)
        self.memory.begin_run()
        self.assertIn("member card", p1.greeting())
        for _ in range(49):
            self.memory.begin_run()
        self.assertIn("I know what you're gonna do", Persona(self.memory).greeting())

    def test_greeting_rotates_between_visits(self):
        """Two run-starts in a row must not open with the same sentence."""
        opened = []
        for _ in range(6):
            self.memory.begin_run()
            opened.append(Persona(self.memory).greeting())
        self.assertGreaterEqual(len(set(opened)), 3, opened)
        self.assertEqual(opened[0], RELATIONSHIP_STAGES[0].greeting)

    def test_greeting_names_your_signature_item(self):
        for _ in range(3):
            run = self.memory.begin_run()
            for _ in range(3):
                self.memory.record_scan("lobster", True, run)
        self.memory.begin_run()  # walk back in
        g = Persona(self.memory).greeting()
        self.assertIn("lobster", g)          # she brings it up unprompted
        self.assertIn("won't be the lobster", g)

    def test_escalation_reaction_handles_empty_notes(self):
        from fishbench.memory import ScanEvent
        ev = ScanEvent(item="lobster", looks_like_barcode=True, run_id="r",
                       novelty="escalation", notes="")
        line = Persona(self.memory).react_to_scan(ev)
        self.assertIn("writing that down", line)
        self.assertNotIn("  ", line)

    def test_system_prompt_never_leaks_tooling(self):
        prompt = Persona(self.memory).system_prompt()
        for banned in ("OPENAI_API_KEY", "Bearer ", "FISHBENCH_LLM"):
            self.assertNotIn(banned, prompt)


if __name__ == "__main__":
    unittest.main()
