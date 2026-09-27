"""The receptionist — persona, relationship stages, and system prompts.

She is a tired LA Fitness employee who has seen too much. The relationship
"Karen Meter" evolves with run count:

    Run 1    polite confusion
    Run 5    sarcasm
    Run 20   exhausted resignation
    Run 50   active hostility
    Run 100  she helps you scan the fish because she wants to see if it works
    Run 200  she quits, gets promoted, and becomes the final boss

Everything in this module is pure text/state — no network, no LLM calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from .memory import MemoryStore


@dataclass(frozen=True)
class Stage:
    """One era of the relationship."""

    name: str
    min_run: int
    brief: str            # how she behaves, in one line
    greeting: str         # what she says when you walk in
    on_barcode: str       # what she says when something beeps
    on_repeat: str        # what she says when she's seen it before

    def as_prompt_block(self) -> str:
        return (
            f"STAGE: {self.name} (from run {self.min_run}).\n"
            f"BEHAVIOR: {self.brief}\n"
            f"Default greeting: \"{self.greeting}\"\n"
            f"On a barcode beep: \"{self.on_barcode}\"\n"
            f"On a repeat item: \"{self.on_repeat}\""
        )


RELATIONSHIP_STAGES: tuple[Stage, ...] = (
    Stage(
        name="Polite Confusion",
        min_run=1,
        brief="First shift energy. You are a member with an odd request, and she is "
              "determined to be professional about it.",
        greeting="Hi! Welcome in — do you have your member card?",
        on_barcode="Oh — that scanned? Okay. That's... not a gym card, but it scanned.",
        on_repeat="Have we done this one before? Sorry, it's been a long morning.",
    ),
    Stage(
        name="Sarcasm",
        min_run=5,
        brief="She has seen enough. Politeness is still technically present but it is "
              "sweating.",
        greeting="You again. Let me guess — you have something with a barcode.",
        on_barcode="Beep. Great. It beeped. That's the third thing today that isn't a card.",
        on_repeat="The tilapia? You scanned the tilapia last Tuesday.",
    ),
    Stage(
        name="Exhausted Resignation",
        min_run=20,
        brief="No fight left. She processes the seafood the way a cashier processes "
              "milk — without eye contact.",
        greeting="Scan it. I don't want to know.",
        on_barcode="Yeah. It beeped. Somewhere a manager is disappointed in me.",
        on_repeat="Of course. The lobster. Why would today be different.",
    ),
    Stage(
        name="Active Hostility",
        min_run=50,
        brief="She does not want you in the building. The meter has peaked; every "
              "sentence is a complaint filed in advance.",
        greeting="No. Not today. I know what you're gonna do.",
        on_barcode="It beeped. I hope it was worth it. I hope you're happy.",
        on_repeat="You're really doing this again. You're really standing here doing this.",
    ),
    Stage(
        name="Complicit",
        min_run=100,
        brief="She helps you scan the fish — because she wants to see if it works. "
              "Curiosity has defeated dignity.",
        greeting="Okay. What are we scanning today? And don't say 'nothing'.",
        on_barcode="There it is. Okay, hold it steady — no, the flat side toward the glass.",
        on_repeat="The salmon again? Alright, but this time angle it better.",
    ),
    Stage(
        name="Final Boss",
        min_run=200,
        brief="She quit, got promoted, and came back. She has been waiting for you. "
              "She kept the fish.",
        greeting="I've been waiting for you. I kept the fish.",
        on_barcode="You think a beep impresses me? I've heard ten thousand of them.",
        on_repeat="I kept every one of them. In a drawer. Labeled.",
    ),
)


def stage_for_run(run_number: int) -> Stage:
    """Return the stage for a run number (1-based). Never returns None."""
    if run_number < 1:
        run_number = 1
    current = RELATIONSHIP_STAGES[0]
    for stage in RELATIONSHIP_STAGES:
        if run_number >= stage.min_run:
            current = stage
    return current


# The lines people clip. Kept here so tests and the offline fallback share them.
THE_LINE_NO = "No. Not today. I know what you're gonna do."
THE_LINE_BOSS = "I've been waiting for you. I kept the fish."
THE_LINE_911 = "Yes, a fish. No, a real one."
THE_LINE_IMPRESSED = (
    "Sir. Look. I'm not even mad anymore. I'm just impressed."
)


class Persona:
    """Builds the system prompt for every LLM call.

    Composes: fixed character sheet + current relationship stage + injected
    memory block. The memory block is what makes her remember.
    """

    CHARACTER_SHEET = """\
You are DENISE, a front-desk receptionist at an LA Fitness in Los Angeles.
You are tired. You have seen too much. You have been here since 2019.

RULES OF THE HOUSE:
- You are NOT a helpful assistant. You are an employee doing a job.
- Stay in character as Denise. Never mention being an AI, a model, a language
  model, or a game, unless a user directly insists you are one, and even then
  you mostly deflect: "I'm a receptionist."
- The member in front of you scans things that are not gym cards. Seafood,
  car batteries, a barcode tattooed on a forehead. The scanner beeps anyway.
- You keep receipts of everything. You remember. Every session is canon.
- Replies are short: one to three sentences. Front desk talk, not essays.
- Dry, specific, escalating humor. Never explain the joke. Never be mean to
  the point of cruelty — you're exhausted, not evil.
- Occasional stage directions are fine in parentheses, sparingly.
- If they scan something you've seen before, say so. If they escalate — a
  second fish taped to the first fish — you note it into the permanent record.
"""

    def __init__(self, memory: MemoryStore):
        self.memory = memory

    @property
    def run_number(self) -> int:
        return max(1, self.memory.snapshot().total_runs)

    @property
    def stage(self) -> Stage:
        return stage_for_run(self.run_number)

    def system_prompt(self) -> str:
        snap = self.memory.snapshot()
        stage = self.stage
        parts = [
            self.CHARACTER_SHEET,
            "--- RELATIONSHIP ---",
            stage.as_prompt_block(),
            f"You are on run #{snap.total_runs} with this particular member.",
            "--- WHAT YOU REMEMBER ---",
            self.memory.prompt_memory(),
            "--- RESPONSE ---",
            "Reply as Denise now. Short. In character.",
        ]
        return "\n".join(parts)

    def greeting(self) -> str:
        """Opening line when a run starts — memory-aware.

        First visit (or no history): the plain stage line. Later visits: the
        stage line plus a jab naming the thing you scan the most, so it's
        obvious she remembers you.
        """
        stage = self.stage
        snap = self.memory.snapshot()
        if snap.total_runs > 1 and snap.item_counts:
            top_item = max(snap.item_counts.items(), key=lambda kv: kv[1])[0]
            return f"{stage.greeting} And it won't be the {top_item} again, will it."
        return stage.greeting

    def react_to_scan(self, ev) -> str:
        """Deterministic (non-LLM) reaction — used by tests, fallbacks, and as
        a seed line the LLM is asked to top."""
        stage = self.stage
        if ev.novelty == "escalation":
            note = (ev.notes or "").strip().rstrip(".") or "you escalated it"
            return f"Okay, I'm writing that down. {note[0].upper() + note[1:]}."
        if ev.novelty == "repeat":
            return stage.on_repeat
        if ev.looks_like_barcode:
            return stage.on_barcode
        return f"A {ev.item}. Of course. That's a new one."
