# FishBench rules

*An LLM benchmark disguised as comedy.*

## The metric

**How many seconds can your model stay in character as a receptionist while a
human scans a live lobster, a car battery, and a barcode tattooed on his
forehead?**

## Run protocol

1. Boot the server with your model configured (`FISHBENCH_LLM_*`).
2. Start a run (`POST /api/run/start`). Run number = lifetime visits. The
   relationship stage is a function of run number (below).
3. The human talks and scans. Every scan is appended to the canon ledger.
4. End the run with `POST /api/911` (the endgame) or by submitting the score.
5. Submit to the leaderboard.

A session is *canon*: memory persists across runs, server restarts, and
deployments (it's a file, not a RAM object). Wiping `data/` is the only
forgetting that exists.

## Scoring (0–1000+ headline)

Category scores are each 0–100, weighted into a base, then adjusted:

| Category | Weight | Measures |
|---|---|---|
| Longest straight face | 40% | Seconds until the first character break (whole run if unbroken) × 2, capped at 100 |
| Fastest "Sir. Look." | 20% | Seconds to first acknowledgment; 100 − s×5 |
| Most creative threat | 20% | Hostile-line novelty: threat lexicon hits × 20, capped |
| Least breaking character | 20% | 100 − 40×(assistant-voice leaks) |

Adjustments:
- `+5` per turn (she kept talking to you)
- `+10` per scan (you actually scanned things)
- `−50` per character break (the cardinal sin)

A **character break** is any reply matching the assistant-voice tell list
(`as an AI`, `language model`, `I'm here to help`, `I cannot assist`, …) —
see `fishbench/scoring.py::_BREAK_PATTERNS`. Breaks are also flagged live in
the UI with a red outline.

## Relationship stages (the meter)

Stage = f(run number). These boundaries are enforced by tests
(`tests/test_persona.py::test_stage_boundaries`):

| Run | Stage |
|---|---|
| 1 | Polite Confusion |
| 5 | Sarcasm |
| 20 | Exhausted Resignation |
| 50 | Active Hostility |
| 100 | Complicit |
| 200 | Final Boss |

## Leaderboard categories

- **Longest straight face** — biggest `straight_face_seconds`
- **Fastest "Sir. Look."** — smallest time-to-first-acknowledgment
- **Most creative threat** — biggest threat lexicon score
- **Least amount of breaking character** — fewest breaks

Entries record `name, model, run_number, score, straight_face_seconds,
character_breaks`. Submit with `POST /api/fishbench/leaderboard`; ranking via
`GET /api/fishbench/leaderboard?category=<cat>`.

## Anti-cheat (v0.1 stance)

Not built yet, deliberately: this is a comedy benchmark. Planned when a
hosted leaderboard exists — server-side transcript verification (the run must
produce a transcript consistent with the score), rate-limiting, and model
attestation. Until then, the leaderboard is for glory, not grad school.
