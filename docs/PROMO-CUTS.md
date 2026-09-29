# Promo cuts — *That's a Fish Barcode*

Compressed promo passes for FishBench v0.1, cut against what the repo actually
does. Every claim below is checkable in `fishbench/` (see **Truth guard** at the
bottom for the four things deliberately cut).

---

## 1. 60-second YouTube spot

**[0:00 — black. Gym ambience. A treadmill hums. Someone fails a rep.]**

**NARRATOR** *(nature-documentary calm)*:
Every gym has a scanner. It accepts anything.

**[A man walks up holding a whole tilapia. He scans it.]**

*BEEP.*

**[The gate opens. One step of glory.]**

The front desk does not.

**RECEPTIONIST** *(flat, dead calm)*:
"Sir. That's a *fish* barcode."

**[FREEZE FRAME. TITLE CARD: THAT'S A FISH BARCODE]**

**[0:15 — SMASH MONTAGE. Rising pace, one beep each:]**

Fire extinguisher — *BEEP*
Car battery — *BEEP*
A second fish taped to the first fish — *BEEP BEEP*
Barcode tattooed on your forehead — *BEEP*
The Old Man takes the fish and inspects it like evidence. Says nothing.

**[0:35 — quiet again.]**

**NARRATOR:**
And she's not scripted. She's a live language model.

**[Beat.]**

She remembers.

Run 1: *"Hi! Welcome in — do you have your member card?"*
Run 20: *"Scan it. I don't want to know."*
Run 50: *"No. Not today. I know what you're gonna do."*
Run 100: she starts *helping* you scan it.
Run 200: *"I've been waiting for you. I kept the fish."*

**[0:50 — LOGO. Credits over black. She's on the phone.]**

**RECEPTIONIST:** "...yes, a fish. No — *a real one.*"

**[BEEP.]**

**ONE MORE BEEP IS ALL YOU NEED.**

*Runs keyless. No API key. It plays.*

---

## 1b. 15-second vertical (short-form)

*9:16. Shot on a phone. No narrator — all her.*

**[0:00]** Handheld. A whole tilapia at the scanner. *BEEP.* The gate opens.

**[0:03]** Her, flat: **"Sir. That's a fish barcode."**

**[0:05]** *TITLE CARD: THAT'S A FISH BARCODE*

**[0:06]** Montage, one beep each: car battery — *BEEP*. Second fish taped to the
first fish — *BEEP BEEP*. Forehead — *BEEP*.

**[0:10]** Her, quietly, not looking up: **"Scan it. I don't want to know."**

**[0:13]** On-screen text: **SHE'S REMEMBERED 20 RUNS.**
Then: **KEYLESS DEMO — NO API KEY.**

**[0:15]** Cut to black on her line: *"...yes, a fish. No — a real one."*

---

## 2. Social post (X / Bluesky — 280ish)

The scanner accepts any barcode. The front desk does not.

Run 1: "Do you have your member card?"
Run 50: "No. Not today. I know what you're gonna do."
Run 100: she helps you scan the fish.
Run 200: "I kept the fish."

She's an LLM. She remembers every one. 🐟

---

## 2b. Social post (long — Reddit / r/gamedev)

**An LLM benchmark disguised as a comedy game.**

The pitch: you're a guy. The gym scanner accepts any barcode — a car battery,
a second fish taped to the first fish, a barcode tattooed on your forehead. The
receptionist is the only obstacle, and she's not scripted. She's a live model
with a persistent memory ledger, so every session is canon.

The meter isn't a Karen Meter, it's a relationship:

| Run | She is | She says |
|---|---|---|
| 1 | Polite Confusion | "Do you have your member card?" |
| 20 | Exhausted Resignation | "Scan it. I don't want to know." |
| 50 | Active Hostility | "No. Not today. I know what you're gonna do." |
| 100 | Complicit | "Okay, hold it steady — no, the flat side toward the glass." |
| 200 | Final Boss | "I've been waiting for you. I kept the fish." |

The benchmark: how long can a model stay in character as a tired receptionist
while a human scans seafood? Scored on longest straight face, fastest "Sir.
Look.", most creative threat, and fewest assistant-voice leaks. Character
breaks are detected live and cost you 50 points.

Zero dependencies. Runs keyless on an offline persona backend, or point it at
any OpenAI-compatible endpoint to make her live. 161 tests.

---

## 3. One-pager pitch

### THAT'S A FISH BARCODE
**The scanner says yes. She says no.**

**Logline.** A gym scanner that accepts any barcode. A receptionist who
remembers every fish you've ever scanned. She is not scripted — she is a live
language model with a memory ledger and a grudge that compounds.

**The loop.** Scan something absurd → the gate opens → she reacts → you get
twelve seconds of her patience → she calls 911. Then you come back tomorrow and
she remembers.

**Why it travels.** The game is the demo, the demo is the ad. Her best lines
are improvised, so nobody can pre-write the clip — every streamer's run is
unique footage. Clips are the marketing.

**The meter is a relationship, not a difficulty slider.**

| Run | Stage | What she does |
|---|---|---|
| 1 | Polite Confusion | "Do you have your member card?" |
| 5 | Sarcasm | "You again. Let me guess — you have something with a barcode." |
| 20 | Exhausted Resignation | "Scan it. I don't want to know." |
| 50 | Active Hostility | "No. Not today. I know what you're gonna do." |
| 100 | Complicit | She helps you scan it, because she wants to see if it works |
| 200 | Final Boss | "I've been waiting for you. I kept the fish." |

**Second product inside the first: FishBench.** An LLM benchmark disguised as
comedy. Score any model on how long it holds the bit — longest straight face,
fastest "Sir. Look.", most creative threat, fewest character breaks. Every
streamer becomes a QA tester.

**The endgame.** She calls 911. The operator is a second model. You hear the
whole argument.

> "Yes, a fish. No, a real one."

**Status.** v0.1 shipped and playable. Keyless out of the box (offline persona
backend), or plug in any OpenAI-compatible model. 161 tests, zero
dependencies, stdlib server, no build step.

---

## 4. Taglines — cut against the build

- *The scanner says yes. She says no.* ← the one
- *She remembers the fish.*
- *Run 200. She kept the fish.*
- *Every barcode deserves a chance.*
- *GET IN. GET BEEPED. GET OUT.*
- *An LLM benchmark disguised as comedy.*
- *"Scan it. I don't want to know."* ← her line, at run 20
- *The gate opens. The front desk does not.*

---

## Truth guard

Four things in the original store blurb are **not in the build**. Cut them or
they'll read as a lie next to a playable demo (roadmap targets noted):

| Claim | Reality |
|---|---|
| "40+ scannable objects" | No object list exists. The scanner accepts *anything* — that's the joke, and it's the stronger version. |
| "Real-time fish decomposition" | No decay/time mechanic in `memory.py` or `scoring.py`. |
| "Story, Endless, Co-op, and 4-player Chaos Mode" | No multiplayer, no modes (`server.py` is single-session). Local leaderboard only. |
| "Unlock new deliveries — whisper, southern, tired 3 a.m., unison" | No voice packs, no TTS, no unlock system (TTS is v0.2). |

Swap the fabrication for the thing that's actually better: **the 6-stage
relationship arc across 200 runs**, and **FishBench** — the benchmark framing
is in the README and `docs/FISHBENCH.md` but was missing from the promo
entirely. That's the strongest unused asset.
