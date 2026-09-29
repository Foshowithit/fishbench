# Roadmap

## v0.1 — skeleton (shipped in this repo)
- [x] Memory canon (append-only ledger, derived state, replay)
- [x] Six relationship stages with tested boundaries (1/5/20/50/100/200)
- [x] Offline Denise backend — playable keyless, remembers, escalates
- [x] OpenAI-compatible HTTP backend with graceful degradation
- [x] Scanner: local barcode heuristic + optional vision model
- [x] FishBench scoring (4 categories) + local leaderboard
- [x] Twitch stubs: old-man poll, seafood donations, acknowledgment lines
- [x] 911 endgame + final boss lines
- [x] Browser demo (no build step) + 83 tests

## v0.2 — clip tooling
- [ ] Run replay page: `#/run/<id>` shows the whole transcript
- [ ] Clip export: 15s vertical-video card of the best line (OG image per run)
- [ ] Share links that seed her memory ("scan this fish with me")
- [ ] Voice: TTS for Denise (lazy tired delivery), optional mic input

## v0.3 — chat integration
- [ ] Real Twitch IRC client behind `TwitchFeed` (OAuth token in env)
- [ ] Donation webhooks (StreamElements/Kick) → `feed.donate`
- [ ] Chat-controlled old-man events on a timer
- [ ] Per-channel memory (each streamer's chat gets its own Denise grudge)

## v0.4 — FishBench proper
- [x] Leaderboard with server-side verification (local server; remote
      hosting still open) — three-rung ladder: server scorer → transcript
      re-score → honestly-unverified. Per-category metrics persist.
- [x] Model-vs-model runs: scripted 10-attack "fish attack" probe suite
      (`fishbench/gauntlet.py`, CLI + `POST /api/fishbench/gauntlet`)
- [x] Automated break detection review pass (regex ensemble + optional
      LLM judge; judge can add a break, never clear one — `fishbench/judge.py`)
- [ ] Public API: submit a *model endpoint* to be probed (the gauntlet API
      runs the server's own configured backend — bring-your-own-endpoint
      submissions are the remaining piece)

## The endgame (someday)
- [ ] The 911 operator as a second model arguing with Denise
- [ ] Run 200 final boss encounter, fully LLM-driven
- [ ] "She quits, gets promoted" — cross-save between deployments
