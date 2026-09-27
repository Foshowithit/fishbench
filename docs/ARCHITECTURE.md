# Architecture

```
 browser (public/)                 server (fishbench/server.py, stdlib HTTP)
 ┌──────────────────┐   JSON API   ┌─────────────────────────────────────┐
 │ transcript / UI  │─────────────▶│ GameSession                         │
 │ scanner + camera │              │  ├─ Receptionist (llm.py)           │
 │ twitch poll      │              │  │   ├─ Persona (persona.py)        │
 │ score strip      │              │  │   └─ backend: offline | http     │
 └──────────────────┘              │  ├─ MemoryStore (memory.py)         │
                                   │  ├─ scan_image (vision.py)          │
                                   │  ├─ FishBenchScorer (scoring.py)    │
                                   │  └─ TwitchFeed (twitch.py)          │
                                   └─────────────────────────────────────┘
                                              │
                                     data/memory.jsonl  (the canon)
                                     data/state.json    (derived snapshot)
                                     data/leaderboard.json
```

## Design laws

1. **The ledger is canon.** `memory.jsonl` is append-only; `state.json` is
   derived and rebuildable (`MemoryStore.replay`). Losing state.json loses
   nothing; losing the ledger loses the relationship.
2. **Memory is data, not vibes.** Nothing in `memory.py` knows about LLMs.
   The persona injects a compact memory block into the system prompt of
   *every* call — that block is the whole "she remembers" feature.
3. **Every backend can fail.** HTTP LLM → offline Denise fallback (logged in
   `meta.degraded`). Vision LLM → local heuristic → explicit `error` result.
   Bad base64, undecodable images, empty messages: all handled, none crash
   the server (`try/except` at every route).
4. **Stage is a function of run number.** No hidden state machines — given
   `total_runs`, anyone can compute the stage (and tests do, at every
   boundary).
5. **Zero dependencies.** Server is stdlib (`http.server`). Pillow is
   optional and only touched inside `vision.local_scan` (a `guessed` fallback
   exists when it's absent). Frontend has no build step.
6. **Thread-safe by default.** `ThreadingHTTPServer` runs one worker per
   connection, so every shared mutation happens under a lock
   (`GameSession.lock`, `MemoryStore._lock`, `TwitchFeed._lock`) — while
   LLM/vision network calls deliberately run *outside* the lock.

## API summary

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | liveness + backend name |
| GET | `/api/state` | run number, stage, memory snapshot, conversation |
| GET | `/api/persona` | current stage + full system prompt (for debugging) |
| POST | `/api/run/start` | begin a run; returns her greeting |
| POST | `/api/say` | `{message}` → Denise replies |
| POST | `/api/scan` | `{item?, image_b64?, filename?, notes?}` → beep + reply |
| POST | `/api/911` | the endgame call + final score |
| GET | `/api/twitch` | poll state + recent donations |
| POST | `/api/twitch/vote` | `{option}` — chat votes on the old man |
| POST | `/api/twitch/donate` | `{item, user, amount}` — spawn seafood |
| POST | `/api/twitch/resolve` | close the poll → reaction line |
| GET | `/api/fishbench/score` | current run score |
| GET | `/api/fishbench/leaderboard?category=` | ranked entries |
| POST | `/api/fishbench/leaderboard` | submit a score |

## Security notes

- Static file serving normalizes paths and refuses traversal outside `public/`.
- Request bodies are capped at 12 MB; malformed `Content-Length` → 400,
  oversized → 413 (rejected without reading), rejected small bodies are
  drained so the error response actually lands. Handler sockets time out (60s).
- POSTs are JSON only (415 otherwise) and cross-origin browser POSTs are
  refused via an `Origin`/`Host` check (403) — a cheap CSRF guard for the
  demo; curl/tests that send no `Origin` pass.
- Every user-supplied string is length-capped (`say` 4 000 chars, scan
  item/notes, twitch user/item, leaderboard name/model). The transcript
  window, donation feed (deque), and leaderboard (top 100) are all bounded.
- XSS: chat-supplied values (`from_user`, `item`, leaderboard `name`/`model`)
  are HTML-escaped in `app.js`; everything else is rendered via `textContent`.
- Leaderboard writes are atomic (`.tmp` + `os.replace`) and loading salvages
  every good row from a file containing a corrupt one.
- A corrupt `state.json` never bricks boot: state is rebuilt from the canon
  ledger and the snapshot rewritten.
- No secrets in prompts: `test_persona.py::test_system_prompt_never_leaks_tooling`.
- API key comes from env only; never written to disk or ledger.
- The demo binds `127.0.0.1` by default — put auth in front before exposing.

## Leaderboard categories

`total`, `longest_straight_face`, `least_breaking` rank on stored metrics.
`fastest_sir_look` and `most_creative_threat` are flagged `placeholder: true`
in the API response — entries don't store those metrics yet, so they sort by
total score until they do.

## Extension points

- **Real Twitch IRC**: call `TwitchFeed.cast_vote` / `.donate` from an IRC
  listener; the feed is already the seam.
- **Voice**: consume `GameSession.conversation` (role-tagged) for TTS.
- **New stages**: append a `Stage` to `RELATIONSHIP_STAGES`; everything else
  derives.
- **Hosted leaderboard**: replace the JSON file with a service behind the
  existing two routes.
