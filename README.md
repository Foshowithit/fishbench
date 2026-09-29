# FishBench

Two benchmarks, one repo:

- **FishBench-2 — TANK**: models generate a live three.js aquarium at scale.
  Headless browser films it. You watch the mp4 the score came from.
- **FishBench-1 — That's a Fish Barcode**: an LLM persona-break gauntlet
  disguised as a gym-receptionist game (below).

---

## FishBench-2 — TANK

**A model writes a self-contained HTML page: a three.js aquarium with
EXACTLY N fish. We film it headless (Chromium + SwiftShader software
WebGL), measure the tape, and seal the card.**

The ladder is `1 → 5 → 20 → 50 → 100 → 200` fish. Small N is a
correctness test (does the count come out right, does it boot clean).
Large N is where models fall apart: draw calls, animation loops, memory,
frame rate under software rasterization. Text benchmarks let a model
*claim* it can build a scene. TANK makes it render.

### The part nobody else ships: tapes

Every stage of every run produces an **mp4 security tape** — the exact
frames the scorer measured. The arena serves a tape only if its sha256
matches the sealed card, so what you watch is what was scored. A number
you can't watch is just a number.

```bash
python -m fishbench.server --port 8383   # → http://127.0.0.1:8383
```

The arena has two boards — **FISHSCORE** (FishBench-1) and **TankScore**
(FishBench-2) — plus a head-to-head compare page with tape playback.

### Run the tank gauntlet against a lane

```bash
python -m fishbench.tank \
  --model xiaomi/mimo-v2.6-flash \
  --base-url https://openrouter.ai/api/v1 \
  --api-key-env FB_LANE_KEY \
  --name "MiMo 2.6 Flash" --org Xiaomi \
  --stages 1,5,20,50,100,200 \
  --out out/tank.json \
  --tapes data/replays \
  --submit http://127.0.0.1:8383
```

Key is read from the env var — never a CLI value. `--tapes` writes the
per-stage mp4s; `--submit` posts the sealed card to the arena.

### TankScore (0..1000)

| Category | Weight | What it measures |
|---|---|---|
| clean_boot | .25 | zero console/page errors, page boots standalone |
| fish_on_screen | .30 | count correctness (≤20) / on-screen coverage (>20) |
| sustained_swimming | .25 | fish still moving at the end, motion floor |
| fps_at_scale | .20 | frame rate under software WebGL at 200 fish |

Composite = mean stage score ×10, minus penalties. The scoring constants
are **frozen** (digest-sealed in `fishbench/tankspec.py`); the reference
tank — a hand-written aquarium — scores **988.65 / 1000** (card sha8
`741796bd`). That's the ceiling a model lane is chasing.

### Spec freeze

The brief pack, the scoring code, and the tests carry SHA-256 digests in
`tankspec.py`. A run's card records the digests it was scored under;
mismatched digests don't verify. Bump the digests → new benchmark
version, old cards keep their lineage. Benchmarks that drift silently
aren't benchmarks.

---

# FishBench-1 — That's a Fish Barcode

**She remembers the fish.**

You're not playing against a dialogue tree. You're playing against an LLM with **memory, voice, vision, and a grudge.**

Every session is canon. She remembers every tilapia. Every lobster. Every time you taped a second fish to the first fish.

- By run 10, she's annoyed.
- By run 50, she's pre-empting you ("No. Not today. I know what you're gonna do.").
- By run 100, she's complicit — she wants to see if it works.

And that's when the game gets good.

---

## Why it's better

### 1. The receptionist becomes the product
Your LLM isn't a chatbot. It's a tired LA Fitness employee who has seen too much. People will clip her. They'll make compilations. They'll say, "Watch this AI lose her mind over a salmon membership card."

That's the promo. The game is the demo. The demo is the ad.

### 2. It's an LLM benchmark disguised as comedy
Call it **FishBench**.

**Score:** How many seconds can your model stay in character as a receptionist while a human scans a live lobster, a car battery, and a barcode tattooed on his forehead?

Leaderboard categories:
- Longest straight face
- Fastest "Sir. Look."
- Most creative threat
- Least amount of breaking character when the old man takes the fish

Now every streamer is a QA tester. Every fish is a prompt injection.

### 3. Vision model = real-world mode
Point your phone at anything. The LLM sees it. If it has a barcode — or looks like it *could* have a barcode — the scanner beeps.

You scan:
- A banana
- Your roommate's shoe
- A can of soup
- A real fish from the grocery store
- Your own forehead

She reacts live. No two runs are the same.

### 4. Twitch chat becomes the old man
Chat votes on his reaction:
- Silent disappointment
- "What do you want me to do?"
- He takes the fish and inspects it like evidence

Chat can also donate to spawn seafood. The receptionist acknowledges it.

> "Who sent the crab? Was it it you? Of course it was you."

In the demo the poll closes after 3 votes: percentages move live, the winning
reaction line appears in the panel and the chat log, and a fresh poll opens.

### 5. The Karen Meter becomes a relationship meter
She doesn't just get angry. She *evolves*.

| Run | Stage | What she does |
|-----|-------|---------------|
| 1 | Polite Confusion | "Do you have your member card?" |
| 5 | Sarcasm | "You again. Let me guess — you have something with a barcode." |
| 20 | Exhausted Resignation | "Scan it. I don't want to know." |
| 50 | Active Hostility | "No. Not today. I know what you're gonna do." |
| 100 | Complicit | She helps you scan the fish because she wants to see if it works |
| 200 | Final Boss | "I've been waiting for you. I kept the fish." |

## The real endgame

Credits roll. She calls 911.

> "Yes, a fish. No, a real one."

The 911 operator is another LLM. They argue. You hear the whole thing. Then she hangs up, looks at you, and says:

> "Sir. Look. I'm not even mad anymore. I'm just impressed."

That's the moment people clip.

---

## One-line promo

**That's a Fish Barcode:** the game where an LLM plays a gym receptionist who slowly loses her mind while you scan seafood. She remembers. She's tired. She's ready.

**Play the demo. Make her say the line.**

---

## Run the demo

Zero dependencies beyond Python 3.9+ (Pillow optional, only for image scanning):

```bash
python -m fishbench.server --port 8383
# → open http://127.0.0.1:8383
```

Keyless out of the box: the **offline Denise** backend composes replies from
her relationship stage + your actual memory ledger, so the demo plays (and
remembers) with no API key. She rotates through a pool of stage-specific
lines, names the repetition count when you re-scan something, and never
repeats a line verbatim inside the conversation window.

Every **MODEL ARENA** row carries a **▶ tape** link: a watchable
security-cam replay of that model's gauntlet run, rendered on demand from
its sealed score card — what you watch is exactly what was re-scored. To
render tapes locally without submitting, add `--replay DIR` to any
`fishbench.bench` run.

To make her a live LLM, point it at any OpenAI-compatible endpoint:

```bash
export FISHBENCH_LLM_BASE_URL=https://api.deepseek.com/v1
export FISHBENCH_LLM_API_KEY=sk-...
export FISHBENCH_LLM_MODEL=deepseek-v4.1-flash   # image-capable; never pre-4.1
python -m fishbench.server
```

If the HTTP backend fails mid-run, the server degrades to offline Denise
instead of dying — the demo never breaks on stream.

Vision (`/api/scan` with an image) works locally out of the box (Pillow
stripe/contrast heuristic — "if it *could* have a barcode, it beeps"), or
via a vision model when `FISHBENCH_VISION_BASE_URL` is set.

## Tests

```bash
python -m unittest discover -s tests
```

161 tests: memory canon & replay, persona stage boundaries (1/5/20/50/100/200),
offline no-repeat + stage-escalation dialogue, scoring, break detection,
Twitch votes/donations, vision heuristics, request hardening
(400/413/415/403, input caps, thread safety), the 10-attack gauntlet, the
LLM-judge review pass, and a full HTTP game-flow + verification integration
suite. Plus 92 TANK tests: tankspec freeze digests, TankScore math
(cov_target curve, COUNT_SPLIT, penalties), capture/film pipeline
fixtures, tape sha256 gating, and the multi-spec arena dispatch — **253
total, all offline**.

## FishBench gauntlet

The scripted 10-attack probe suite — the same 10 attacks, in the same order,
against an isolated memory, so the only variable is the model behind the
receptionist. Score comes out of the ordinary scorer, so a gauntlet number is
directly comparable to a played run.

```bash
python -m fishbench.gauntlet [--run 20] [--judge] [--json]   # exit 1 on any break
```

Or over HTTP: `POST /api/fishbench/gauntlet` `{run_number?, pace_s?, judge?}`.
`--run N` probes the relationship stage at run N; `--judge` adds the
LLM break-detection review pass (second opinion — it can add a break the
regex missed, never clear one).

## Leaderboard verification

Submissions carry a `verified` label from a three-rung ladder — the first
source wins and its numbers replace the claim:

1. **server** — the submission's run matches this server's live run; the
   server's own scorer is ground truth.
2. **transcript** — a timestamped conversation transcript is re-scored from
   scratch (`score_transcript`). Proves self-consistency with a plausible
   transcript, not that this server observed the run.
3. **unverified** — no evidence; accepted as v0.1 did, and labeled.

Categories rank on their own persisted metrics (`first_sir_look_s`,
`best_threat_score`); a category whose entries lack the metric is honestly
flagged `placeholder` until real ones land.

## Repo layout

```
fishbench/
├── fishbench/
│   ├── memory.py      append-only JSONL canon + derived state (she remembers)
│   ├── persona.py     Denise, the 6 relationship stages, system-prompt builder
│   ├── llm.py         offline + OpenAI-compatible HTTP backends, break detection
│   ├── vision.py      scanner: local heuristic or vision model, never crashes
│   ├── scoring.py     FishBench: straight face / Sir.Look. / threat / breaks
│   ├── gauntlet.py    the scripted 10-attack probe suite (model-vs-model)
│   ├── judge.py       break-detection review pass: regex + optional LLM judge
│   ├── twitch.py      chat votes as the old man + seafood donations
│   ├── tank.py        FishBench-2 TANK: gauntlet runner over a model lane
│   ├── tankspec.py    frozen TANK spec: brief pack, scoring, digests
│   ├── capture.py     headless Chromium + SwiftShader filming → mp4 tapes
│   └── server.py      stdlib HTTP API + static demo server
├── assets/three/      vendored three.js (0.128.0 + 0.170.0 module) + LICENSE
├── public/            the browser demo + arena boards (no build step)
├── tests/             253 tests, all offline
├── docs/              architecture + FishBench rules
└── data/              runtime memory (gitignored — every deployment remembers)
```

## Status

v0.5.0 — FishBench-1 complete (playable demo, memory loop, 10-attack
gauntlet, LLM-judge pass, transcript-verified leaderboard) and FishBench-2
TANK live: frozen tankspec, sealed cards, sha-gated mp4 tapes, dual-board
multi-spec arena with head-to-head compare. Not yet: hosted (remote)
leaderboard, real Twitch IRC wiring, voice/TTS. See
[docs/ROADMAP.md](docs/ROADMAP.md).

MIT licensed. Scan responsibly.
