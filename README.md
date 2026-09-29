# That's a Fish Barcode

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
suite.

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
│   └── server.py      stdlib HTTP API + static demo server
├── public/            the browser demo (no build step)
├── tests/             161 tests, all offline
├── docs/              architecture + FishBench rules
└── data/              runtime memory (gitignored — every deployment remembers)
```

## Status

v0.4 — playable demo, full memory loop, scoring harness, per-category
leaderboard with transcript verification, the 10-attack gauntlet (CLI +
API), and the LLM-judge review pass. Twitch stubs only. Not yet: real
Twitch IRC wiring, voice/TTS, a hosted (remote) leaderboard,
clip-export tooling. See [docs/ROADMAP.md](docs/ROADMAP.md).

MIT licensed. Scan responsibly.
