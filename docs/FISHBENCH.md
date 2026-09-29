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

## Anti-cheat (FishBench-1)

Built. Full methodology in [SPEC.md](SPEC.md) — the short version:

- **Frozen spec**: `fishbench/spec.py` pins spec ID `fishbench-1`
  (2026-09-28): stages 1/5/20/50/100/200, 5.0s virtual pace, temperature
  0.7, attack pack a01–a10, every scoring constant. Any change ⇒
  `fishbench-2`, new board.
- **Official submissions** are sealed result cards from the headless
  runner — 6 stages × 10 attacks = 60 probes against your own endpoint:
  `python -m fishbench.bench --model ID --base-url URL --api-key-env
  KEY_VAR --submit http://arena:8383`. The card is one JSON artifact
  sealed with a sha256 over its canonical form; API keys are read only
  from the env var you name, never argv.
- **Verification**: the arena refuses foreign-spec cards at the digest
  gate, recomputes the seal, and re-scores every stage transcript from
  scratch — claimed numbers must fall out of the card's own transcript.
  ✓ verified / ⚠ unverified badge per row; ranking is on claimed
  FishScore with the badge shown (SWE-bench style). What verification
  does and doesn't prove is spelled out in SPEC.md §5.
- **The model leaderboard** (`MODEL ARENA` button, `GET/POST
  /api/fishbench/arena`) keeps the best card per model; offline Denise is
  seeded as the 640.0/1000 baseline on every fresh arena.
- **Security tapes**: every arena row gets a **▶ tape** link — a
  watchable security-cam replay of that model's run, rendered from the
  archived sealed card (`/watch/<sha8>`). What you watch is exactly what
  was re-scored. SPEC.md §5.1.
