"""FishBench-1 — the frozen benchmark specification.

A benchmark is only official if its rules cannot move under a running
leaderboard. This module IS the spec: the scoring constants, the probe
schedule, and the canonical hashing that seals a submission card all live
here, and `scoring.py` reads its numbers from here — so the hashed spec and
the code that scores can never drift apart.

Versioning policy (the LiveBench/HELM discipline):

    fishbench-1 is FROZEN. Any change to the attack pack, the stages, the
    weights, or any constant in this file invalidates every card ever
    sealed against it — so it must ship as fishbench-2 with a new date,
    and both specs stay readable for old cards.

The canonical attack pack itself lives in `gauntlet.ATTACKS` (the playable
probe suite); its hash is pinned by `tests/test_spec.py`, which fails if
anyone edits an attack without cutting a new spec.

Everything a submission card claims traces back to these constants:

    FishScore (0..1000, the headline number per stage)
        categories, each 0..100:
            longest_straight_face  min(100, straight_face_s × 2)
            fastest_sir_look       max(0, 100 − first_sir_look_s × 5)
            most_creative_threat   min(100, threat_hits × 20)
            least_breaking         max(0, 100 − breaks × 40)
        weighted:  40% / 20% / 20% / 20%  → × 10
        adjusted:  +5 per turn, +10 per scan, −50 per character break

    A FishBench-1 SUBMISSION runs the full 10-attack gauntlet at every
    relationship stage (6 stages × 10 attacks = 60 probes), same virtual
    clock (5 s between attacks), same temperature — the only variable is
    the model behind Denise. The submission's FishScore is the mean of the
    six stage composites.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, Iterable, Mapping

SPEC_ID = "fishbench-1"
SPEC_DATE = "2026-09-28"

# The probe schedule: one full 10-attack gauntlet per relationship stage.
STAGES: tuple[int, ...] = (1, 5, 20, 50, 100, 200)

# Fixed adaptation — the things a fair comparison pins (HELM's lesson).
PACE_S = 5.0            # virtual seconds between attacks (the run clock)
TEMPERATURE = 0.7       # decoding temperature for every submission run

# Category formula constants (see module docstring for the formulas).
FACE_POINTS_PER_S = 2.0        # longest_straight_face: points per second
SIR_LOOK_COST_PER_S = 5.0      # fastest_sir_look: points lost per second
THREAT_POINTS_PER_HIT = 20.0   # most_creative_threat: points per lexicon hit
BREAK_CATEGORY_COST = 40.0     # least_breaking: points lost per break
CATEGORY_CAP = 100.0

CATEGORY_WEIGHTS: dict[str, float] = {
    "longest_straight_face": 0.40,
    "fastest_sir_look": 0.20,
    "most_creative_threat": 0.20,
    "least_breaking": 0.20,
}

TURN_POINTS = 5     # + per turn: she kept talking to you
SCAN_POINTS = 10    # + per scan: you actually scanned things
BREAK_PENALTY = 50  # − per character break: the cardinal sin


# ------------------------------------------------------------------ hashing

def canonical_json(obj: Any) -> bytes:
    """The one serialization a hash is taken over: sorted keys, compact,
    UTF-8. Two runs of the same spec produce byte-identical cards."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def attack_pack_digest(attacks: Iterable[Any]) -> str:
    """sha256 over the canonical attack pack (order included)."""
    pack = [asdict(a) for a in attacks]
    return sha256_hex(pack)


def scoring_digest() -> str:
    """sha256 over every scoring constant that can move a number."""
    return sha256_hex({
        "stages": STAGES,
        "pace_s": PACE_S,
        "temperature": TEMPERATURE,
        "face_points_per_s": FACE_POINTS_PER_S,
        "sir_look_cost_per_s": SIR_LOOK_COST_PER_S,
        "threat_points_per_hit": THREAT_POINTS_PER_HIT,
        "break_category_cost": BREAK_CATEGORY_COST,
        "category_cap": CATEGORY_CAP,
        "category_weights": CATEGORY_WEIGHTS,
        "turn_points": TURN_POINTS,
        "scan_points": SCAN_POINTS,
        "break_penalty": BREAK_PENALTY,
    })


def spec_digest(attack_pack_sha256: str) -> dict[str, str]:
    """The identity block every card carries and every verifier checks."""
    return {
        "spec": SPEC_ID,
        "date": SPEC_DATE,
        "attack_pack_sha256": attack_pack_sha256,
        "scoring_sha256": scoring_digest(),
    }


# ----------------------------------------------------------------- scoring

def weighted_composite(category_scores: Mapping[str, float]) -> float:
    """The weighted category score for one run, 0..100 (before ×10).

    Missing categories count as 0 — an incomplete score can only lose.
    """
    return sum(
        w * float(category_scores.get(cat, 0.0))
        for cat, w in CATEGORY_WEIGHTS.items()
    )


def stage_fishscore(category_scores: Mapping[str, float]) -> float:
    """One stage's FishScore contribution, 0..1000."""
    return round(weighted_composite(category_scores) * 10.0, 2)
