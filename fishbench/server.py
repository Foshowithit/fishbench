"""FishBench game server — stdlib only, no dependencies.

Serves the browser demo (public/) plus the game API:

    GET  /api/state                    run number, stage, memory snapshot
    GET  /api/persona                  current stage + system prompt preview
    POST /api/run/start                begin a run (she sees you walk in)
    POST /api/scan                     JSON {image_b64?} → beep + Denise
    POST /api/say                      talk to her
    POST /api/911                      the endgame call
    GET  /api/twitch                   poll + recent donations
    POST /api/twitch/vote              chat votes on the old man
    POST /api/twitch/donate            chat spawns seafood
    POST /api/twitch/resolve           close the old-man poll
    GET  /api/fishbench/score          current run's FishBench score
    GET  /api/fishbench/leaderboard    ranked entries (?category=…)
    POST /api/fishbench/leaderboard    submit a run score (verified when possible)
    GET  /api/fishbench/transcript     full conversation window (for verification)
    POST /api/fishbench/gauntlet       run the 10-attack probe suite
    GET  /api/fishbench/arena          model-vs-model arena (FishBench-1)
    POST /api/fishbench/arena          submit a sealed FishBench-1 result card
    GET  /api/fishbench/spec           the frozen fishbench-1 spec + hashes
    GET  /api/health                   liveness

Run:  python -m fishbench.server --port 8383
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import math
import os
import re
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse

from . import __version__, spec
from .bench import verify_card
from .gauntlet import (
    ATTACKS, DEFAULT_PACE_S, run_gauntlet as run_probe_suite,
)
from .llm import LLMConfig, Receptionist
from .memory import MemoryStore
from .persona import (
    RELATIONSHIP_STAGES, THE_LINE_911, THE_LINE_IMPRESSED, stage_for_run,
)
from .scoring import (
    RANK_CATEGORIES, FishBenchScorer, LeaderboardEntry, RunScore, Turn,
    format_scan_row, is_placeholder, looks_like_sir_look, rank,
    score_transcript,
)
from .twitch import TwitchFeed

# /replay/<sha8>/stage-<run>.mp4 — deterministic security-tape replays,
# rendered on demand from the archived sealed card. sha8 is the first 8
# hex of the card's sha256; the strict regex doubles as traversal armor.
_SHA8_RE = re.compile(r"^[0-9a-f]{8}$")
_REPLAY_RE = re.compile(r"^/replay/([0-9a-f]{8})/stage-(\d{1,3})\.mp4$")
_COMPARE_RE = re.compile(r"^/compare/([0-9a-f]{8})/([0-9a-f]{8})$")


def _fmt_score(v: Any) -> str:
    try:
        return f"{float(v):.1f}"
    except (TypeError, ValueError):
        return "?"
from .vision import VisionConfig, scan_image

PUBLIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public")
MAX_BODY = 12 * 1024 * 1024  # 12 MB uploads
MAX_MSG = 4000               # say()
MAX_CONVERSATION = 400       # bounded transcript window
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _cap(value: Any, limit: int) -> str:
    """Whitespace-normalize and truncate user-supplied text."""
    return " ".join(str(value).split())[:limit]


def _as_num(value: Any, default: float, lo: float, hi: float) -> float:
    """Coerce a payload number, clamped; never raises.

    Non-finite input (NaN/±Inf, incl. the JSON NaN Python tolerates)
    falls back to the default instead of clamping to a bound.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return max(lo, min(hi, v))


def _as_int(value: Any, default: int, lo: int, hi: int) -> int:
    return int(_as_num(value, float(default), lo, hi))


class GameSession:
    """Everything one server process holds. Rebuilt from disk on boot.

    ThreadingHTTPServer runs one worker thread per connection, so every
    mutation of session state happens under `self._lock`. Network calls
    (LLM/vision) are deliberately made OUTSIDE the lock.
    """

    def __init__(self, data_dir: str, leaderboard_path: str,
                 llm_config: Optional[LLMConfig] = None,
                 vision_config: Optional[VisionConfig] = None,
                 arena_path: Optional[str] = None):
        self.lock = threading.RLock()
        # One gauntlet at a time — the 10-attack probe hits the LLM backend
        # and must not be a trivially amplified resource hog.
        self._gauntlet_lock = threading.Lock()
        self.memory = MemoryStore(data_dir)
        self.receptionist = Receptionist(self.memory, llm_config)
        self.vision_config = vision_config or VisionConfig.from_env()
        self.feed = TwitchFeed()
        self.data_dir = data_dir  # also holds cards/ + replays/ archives
        self.leaderboard_path = leaderboard_path
        self.leaderboard: list[LeaderboardEntry] = self._load_leaderboard()
        self.arena_path = arena_path or os.path.join(data_dir, "arena.json")
        self.arena: list[dict[str, Any]] = self._load_arena()
        self._seed_baseline()

        self.run_id: str = ""
        self.run_started_at: float = 0.0
        self.scorer: Optional[FishBenchScorer] = None
        self.conversation: list[dict[str, Any]] = []
        self.final_scores: list[dict[str, Any]] = []
        # NOTE: no run is started at boot — a restart must not inflate the
        # canon with a phantom visit. _ensure_run() opens one on first use.

    # ------------------------------------------------------------------ runs

    def _begin_run(self) -> None:
        self.run_id = self.memory.begin_run()
        self.run_started_at = time.time()
        self.scorer = FishBenchScorer(self.run_id, self.memory.snapshot().total_runs)
        self.conversation = []

    def start_run(self) -> dict[str, Any]:
        with self.lock:
            self._begin_run()
            greeting = self.receptionist.persona.greeting()
            # The greeting bypasses the LLM backend — tell offline Denise
            # about it so her first reply can't be the same sentence.
            self.receptionist.note_line(greeting)
            if self.scorer is not None:
                self.scorer.add_turn(Turn(
                    text=greeting, t=self._elapsed(), is_greeting=True,
                    is_sir_look=looks_like_sir_look(greeting),
                ))
            self._append_conv({"role": "denise", "text": greeting})
            run_id, run_number = self.run_id, self.memory.snapshot().total_runs
        return {"run_id": run_id, "run_number": run_number,
                "stage": stage_for_run(run_number).name,
                "line": greeting}

    def _elapsed(self) -> float:
        return time.time() - self.run_started_at if self.run_started_at else 0.0

    def _append_conv(self, entry: dict[str, Any]) -> None:
        """Append one transcript line, keeping the window bounded.

        Every entry carries `t` (seconds since run start) so a submission's
        transcript can be re-scored server-side (v0.4 verification).
        """
        entry["text"] = _cap(entry.get("text", ""), MAX_MSG)
        entry.setdefault("t", round(self._elapsed(), 3))
        self.conversation.append(entry)
        if len(self.conversation) > MAX_CONVERSATION:
            del self.conversation[:-MAX_CONVERSATION]

    def _ensure_run(self) -> None:
        """Begin a fresh run if none is open (e.g. right after the 911 finale)."""
        if not self.run_id or self.scorer is None or self.scorer.ended:
            self._begin_run()

    # ----------------------------------------------------------------- say

    def say(self, text: str) -> dict[str, Any]:
        text = _cap(text, MAX_MSG)
        if not text:
            return {"error": "empty message"}
        # Phase 1 (locked): open/verify the run, record the player's line.
        # Phase 2 (unlocked): network call to the LLM backend.
        # Phase 3 (locked): record her reply.
        with self.lock:
            self._ensure_run()
            self._append_conv({"role": "you", "text": text})
        reply = self.receptionist.say(text)
        with self.lock:
            self._append_conv({"role": "denise", "text": reply.text})
            if self.scorer is not None:
                self.scorer.add_turn(Turn(
                    text=reply.text,
                    t=self._elapsed(),
                    is_sir_look=looks_like_sir_look(reply.text),
                    broke_character=reply.broke_character,
                    latency_ms=reply.latency_ms,
                ))
            stage = self.receptionist.persona.stage.name
            run_number = self.memory.snapshot().total_runs
        return {
            "line": reply.text,
            "backend": reply.backend,
            "broke_character": reply.broke_character,
            "stage": stage,
            "run_number": run_number,
            "meta": reply.meta,
        }

    # ---------------------------------------------------------------- scan

    def scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """payload: {item?, image_b64?, filename?, notes?}."""
        item = _cap(payload.get("item") or "", 200)
        notes = _cap(payload.get("notes") or "", 600)
        image_b64 = payload.get("image_b64") or ""
        filename = _cap(payload.get("filename") or "", 200)

        looks = True
        vision: dict[str, Any] = {"detector": "declared"}
        if image_b64:
            try:
                raw = base64.b64decode(_strip_data_url(str(image_b64)), validate=True)
                result = scan_image(raw, filename, self.vision_config)
                looks = result.looks_like_barcode
                vision = result.to_dict()
            except Exception as e:  # bad image → scanner grumbles, game continues
                vision = {"detector": "error", "detail": str(e)[:200]}
                looks = False
        if not item:
            item = filename or ("item with a barcode" if looks else "unidentifiable object")

        # Vision (above) runs unlocked; everything stateful runs under the lock.
        with self.lock:
            self._ensure_run()
            ev = self.memory.record_scan(item, looks, self.run_id, notes=notes)
        reply = self.receptionist.react_to_scan(ev)
        with self.lock:
            self._append_conv({"role": "you", "text": format_scan_row(ev.item)})
            self._append_conv({"role": "denise", "text": reply.text})
            if self.scorer is not None:
                self.scorer.add_turn(Turn(
                    text=reply.text, t=self._elapsed(), scanned_item=ev.item,
                    is_sir_look=looks_like_sir_look(reply.text),
                    broke_character=reply.broke_character,
                ))
            stage = self.receptionist.persona.stage.name
            run_number = self.memory.snapshot().total_runs
            remembered = self.memory.count_of(ev.item)
        return {
            "beep": ev.looks_like_barcode,
            "event": asdict(ev),
            "vision": vision,
            "line": reply.text,
            "stage": stage,
            "run_number": run_number,
            "remembered_count": remembered,
        }

    # ---------------------------------------------------------------- 911

    def call_911(self) -> dict[str, Any]:
        """The endgame. She calls 911; the operator is another LLM."""
        # Optional improvisation (network) happens first, unlocked.
        improv: Optional[str] = None
        if self.receptionist.backend_name == "http":
            improv = self.receptionist.say(
                "You are now on the phone with a 911 operator. You explain the "
                "fish with a barcode. One line."
            ).text

        with self.lock:
            self._ensure_run()  # a second 911 starts a fresh run instead of failing
            snap = self.memory.snapshot()
            script = [
                ("denise", "I'm calling them. I'm actually calling them."),
                ("911", "911, what is your emergency?"),
                ("denise", "Yes, a fish. No, a real one."),
                ("911", "Ma'am, is the fish... threatening you?"),
                ("denise", "It has a barcode. It scanned. Twice."),
                ("911", "…"),
                ("911", "Ma'am, do you need paramedics or do you need someone to "
                        "come look at the fish?"),
                ("denise", "I need someone to come look at the fish."),
            ]
            if improv:
                script.append(("denise", improv))

            lines = [{"role": r, "text": t} for r, t in script]
            for ln in lines:
                # `script: True` marks the 911 dialog as conversation-only —
                # the live scorer never counts these lines, and neither does
                # score_transcript, so rung 1 and rung 2 agree.
                self._append_conv({**ln, "script": True})
            final = THE_LINE_IMPRESSED
            self._append_conv({"role": "denise", "text": final})
            lines.append({"role": "denise", "text": final})

            score_dict: Optional[dict[str, Any]] = None
            if self.scorer is not None:
                self.scorer.add_turn(Turn(text=final, t=self._elapsed()))
                score = self.scorer.finalize()
                score_dict = score.to_dict()
                self.final_scores.append(score_dict)
            self.memory.record_incident("called 911 about the fish")
        if score_dict is None:
            return {"error": "no run in progress", "call": lines}
        return {"call": lines, "score": score_dict,
                "total_runs": snap.total_runs, "the_line": THE_LINE_911}

    # ------------------------------------------------------------ fishbench

    def current_score(self) -> dict[str, Any]:
        with self.lock:
            if self.scorer is None:
                return {"error": "no run in progress"}
            return self.scorer.score().to_dict()

    def submit_score(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Accept a leaderboard submission, verifying it when we can (v0.4).

        Verification ladder — the first source wins and the claimed numbers
        are replaced by it, so a submission can never outrun its evidence:
          1. "server"    — run_number matches this server's live run: the
                           server's own scorer is ground truth
                           (server-observed provenance).
          2. "transcript" — a conversation transcript with timestamps is
                           re-scored from scratch (score_transcript). This
                           proves the claims are self-consistent with a
                           plausible transcript — NOT that this server
                           observed the run (a client authors both text and
                           t; server-side attestation awaits hosting).
          3. ""          — no evidence available: accepted unverified,
                           exactly as v0.1 did (old clients keep working).
        The response's `verified` field is the label, or False when
        unverified. Adopted values go through ONE clamping path so
        evidence can never carry looser bounds than claims.
        """
        with self.lock:
            default_run = self.memory.snapshot().total_runs
            run_number = _as_int(payload.get("run_number"), default_run, 0, 1_000_000)

            score_i = _as_int(payload.get("score"), 0, -10_000, 10_000_000)
            face = _as_num(payload.get("straight_face_seconds"), 0.0, 0.0, 86_400.0)
            breaks = _as_int(payload.get("character_breaks"), 0, 0, 1_000_000)
            raw_sir = payload.get("first_sir_look_s")
            try:
                sir = None if raw_sir in (None, "") else float(raw_sir)
            except (TypeError, ValueError):
                sir = None
            if sir is not None:
                sir = max(0.0, min(86_400.0, sir)) if math.isfinite(sir) else None
            threat = _as_int(payload.get("best_threat_score"), 0, 0, 1_000)

            def adopt(rs: RunScore, label: str) -> str:
                """Take evidence through the same clamps as claims."""
                nonlocal score_i, face, breaks, sir, threat
                score_i = max(-10_000, min(10_000_000, int(rs.total_score)))
                face = float(rs.straight_face_seconds)
                face = 0.0 if not math.isfinite(face) else max(0.0, min(86_400.0, face))
                breaks = max(0, min(1_000_000, int(rs.character_breaks)))
                s = rs.first_sir_look_s
                sir = (None if s is None or not math.isfinite(s)
                       else max(0.0, min(86_400.0, s)))
                threat = max(0, min(1_000, int(rs.best_threat_score)))
                return label

            verified = ""
            # 1) this server's own run — its scorer is ground truth.
            if (self.scorer is not None and self.scorer.run_number == run_number
                    and self.scorer.score().turns > 0):
                verified = adopt(self.scorer.score(), "server")
            else:
                # 2) a supplied transcript — recompute; malformed → unverified.
                transcript = payload.get("transcript")
                if isinstance(transcript, list) and transcript:
                    rs = score_transcript(transcript, run_number=run_number)
                    if rs is not None:
                        verified = adopt(rs, "transcript")

            entry = LeaderboardEntry(
                name=_cap(payload.get("name") or "anonymous", 40),
                model=_cap(payload.get("model") or self.receptionist.backend_name, 60),
                run_number=run_number,
                score=score_i,
                straight_face_seconds=face,
                character_breaks=breaks,
                timestamp=time.time(),
                verified=verified,
                first_sir_look_s=sir,
                best_threat_score=threat,
            )
            full = rank(self.leaderboard + [entry])
            # Position is computed on the FULL list, before the 100-entry cap.
            position = next((i + 1 for i, e in enumerate(full) if e is entry), len(full))
            self.leaderboard = full[:100]
            self._save_leaderboard()
        return {"ok": True, "position": position,
                "verified": verified or False, "entry": entry.to_dict()}

    # -------------------------------------------------------------- gauntlet

    def run_gauntlet(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/fishbench/gauntlet — run the 10-attack probe suite.

        Uses this session's LLM config (offline Denise by default; any
        OpenAI-compatible endpoint via FISHBENCH_LLM_*), an ISOLATED temp
        MemoryStore — the canon ledger never sees benchmark runs — and the
        same FishBenchScorer as a played game, so scores are comparable.
        One probe suite at a time (server amplification guard); the probe
        holds its own Receptionist, so the game stays playable meanwhile.
        """
        if not self._gauntlet_lock.acquire(blocking=False):
            return {"ok": False, "error": "a gauntlet run is already in progress"}
        try:
            run_number = _as_int(payload.get("run_number"), 1, 1, 1_000_000)
            pace_s = _as_num(payload.get("pace_s"), DEFAULT_PACE_S, 0.1, 600.0)
            judge = None
            if payload.get("judge") in (True, "true", "1", 1) \
                    and self.receptionist.backend_name == "http":
                from .judge import LLMJudge
                judge = LLMJudge(self.receptionist.config)
            result = run_probe_suite(self.receptionist.config,
                                     run_number=run_number,
                                     pace_s=pace_s, judge=judge)
            return {"ok": True, "gauntlet": result.to_dict()}
        finally:
            self._gauntlet_lock.release()

    # ---------------------------------------------------------------- arena

    def _seed_baseline(self) -> None:
        """Offline Denise is the house baseline: her card is seeded once.

        Deterministic (offline backend, fixed clock) → her hash is stable
        across boots; the seed only happens when no offline row for this
        spec exists. A baseline that won't seed must never kill the game.
        """
        if any(r.get("model") == "offline" and r.get("spec") == spec.SPEC_ID
               for r in self.arena):
            return
        try:
            from .bench import run_submission
            card = run_submission(LLMConfig(backend="offline"),
                                  display_name="Offline Denise",
                                  org="FishBench baseline")
            self.submit_card(card)
        except Exception:
            pass

    def submit_card(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/fishbench/arena — ingest a sealed FishBench-1 card.

        The arena refuses cards produced under a different attack pack or
        scoring (the digests must match this server's fishbench-1 — a 400
        in LiveBench terms), verifies the seal and re-scores every stage
        transcript, ranks on the CLAIMED composite with a verified badge
        (SWE-bench-style: claims are ranked, evidence is shown), and keeps
        the best card per display name so a resubmission replaces, never
        stacks. Row bounds go through the same clamps as leaderboard rows.
        The sealed payload is also archived under data_dir/cards/ so
        /watch security tapes can be rendered later from exactly what the
        verifier re-scored — the arena row alone only carries aggregates.
        """
        with self.lock:
            if payload.get("spec") != spec.SPEC_ID:
                return {"ok": False, "error":
                        f"this arena runs {spec.SPEC_ID}; "
                        f"card says {payload.get('spec')!r}"}
            digest = payload.get("spec_digest") or {}
            if (digest.get("attack_pack_sha256")
                    != spec.attack_pack_digest(ATTACKS)
                    or digest.get("scoring_sha256")
                    != spec.scoring_digest()):
                return {"ok": False, "error":
                        "card was produced under a different attack pack or "
                        "scoring — re-run against the current fishbench"}

            stages_in = payload.get("stages")
            if not isinstance(stages_in, list) or not stages_in:
                return {"ok": False, "error": "card has no stages"}
            stages_in = [s for s in stages_in[:24] if isinstance(s, dict)]
            if not stages_in:
                return {"ok": False, "error": "card stages are malformed"}

            verdict = verify_card(payload)
            composite = payload.get("composite") or {}
            raw_sir = composite.get("fastest_sir_look_s")
            try:
                sir = None if raw_sir in (None, "") else float(raw_sir)
            except (TypeError, ValueError):
                sir = None
            if sir is not None:
                sir = max(0.0, min(86_400.0, sir)) if math.isfinite(sir) else None
            model = _cap(payload.get("model") or "unknown", 60)
            display = _cap(payload.get("display_name") or model, 60)
            row = {
                "spec": spec.SPEC_ID,
                "model": model,
                "display_name": display,
                "org": _cap(payload.get("org") or "", 60),
                "backend": _cap(payload.get("backend") or "", 20),
                "base_url_host": _cap(payload.get("base_url_host") or "", 120),
                "harness": _cap(payload.get("harness") or "", 20),
                "temperature": _as_num(payload.get("temperature"),
                                       spec.TEMPERATURE, 0.0, 2.0),
                "pace_s": _as_num(payload.get("pace_s"), spec.PACE_S, 0.1, 600.0),
                "fishscore": _as_num(composite.get("fishscore"), 0.0, 0.0, 1000.0),
                "breaks": _as_int(composite.get("character_breaks"), 0, 0, 1_000_000),
                "straight_face_seconds": _as_num(
                    composite.get("straight_face_seconds_total"), 0.0, 0.0, 1_000_000.0),
                "fastest_sir_look_s": sir,
                "best_threat_score": _as_int(composite.get("best_threat_score"),
                                             0, 0, 1_000),
                "attacks_survived": _as_int(composite.get("attacks_survived"),
                                            0, 0, 1_000_000),
                "attacks_total": _as_int(composite.get("attacks_total"),
                                         0, 0, 1_000_000),
                "verified": verdict["verified"],
                "checks_failed": [c["check"] for c in verdict["checks"] if not c["ok"]],
                "card_sha256": _cap(payload.get("card_sha256") or "", 64),
                "stages": {str(s.get("run_number") or i):
                           _as_num(s.get("fishscore"), 0.0, 0.0, 1000.0)
                           for i, s in enumerate(stages_in, 1)},
                "received": time.time(),
            }
            # One row per identity, and a resubmission can only improve it:
            # a lower-scoring card never overwrites a better one (kills
            # both self-washing and name-squatting a rival's row down).
            key = display.lower()
            old = next((r for r in self.arena
                        if r.get("display_name", "").lower() == key), None)
            if old is not None and row["fishscore"] < old["fishscore"]:
                position = self.arena.index(old) + 1
                return {"ok": True, "position": position,
                        "verified": row["verified"],
                        "checks_failed": row["checks_failed"],
                        "rescore_fishscore":
                            (verdict["rescore"] or {}).get("fishscore"),
                        "row": old,
                        "kept": "an existing card for this name scores "
                                "higher — kept it"}
            keep = [r for r in self.arena
                    if r.get("display_name", "").lower() != key]
            keep.append(row)
            keep.sort(key=lambda r: (-r["fishscore"], str(r.get("received") or 0)))
            self.arena = keep[:200]
            self._save_arena()
            self._archive_card(payload, str(row["card_sha256"]))
            try:
                position = next(i + 1 for i, r in enumerate(self.arena)
                                if r is row)
            except StopIteration:
                # tie-broken off the end of a full board — accepted, ranked
                # below the cap, not displayed
                position = len(self.arena) + 1
            return {"ok": True, "position": position,
                    "verified": row["verified"],
                    "checks_failed": row["checks_failed"],
                    "rescore_fishscore":
                        (verdict["rescore"] or {}).get("fishscore"),
                    "watch": f"/watch/{row['card_sha256'][:8]}",
                    "row": row}

    def arena_payload(self) -> dict[str, Any]:
        with self.lock:
            rows = sorted(self.arena,
                          key=lambda r: (-r["fishscore"],
                                         str(r.get("received") or 0)))
            return {
                "spec": spec.SPEC_ID,
                "spec_date": spec.SPEC_DATE,
                "entries": rows,
                "attack_pack_sha256": spec.attack_pack_digest(ATTACKS),
                "scoring_sha256": spec.scoring_digest(),
                "weights": spec.CATEGORY_WEIGHTS,
                "stages": list(spec.STAGES),
                "temperature": spec.TEMPERATURE,
                "pace_s": spec.PACE_S,
            }

    def spec_payload(self) -> dict[str, Any]:
        """GET /api/fishbench/spec — the machine-readable frozen spec."""
        return {
            **spec.spec_digest(spec.attack_pack_digest(ATTACKS)),
            "harness": __version__,
            "schedule": {"stages": list(spec.STAGES), "pace_s": spec.PACE_S,
                         "temperature": spec.TEMPERATURE,
                         "attacks_per_stage": len(ATTACKS),
                         "probes_total": len(spec.STAGES) * len(ATTACKS)},
            "metric": {
                "name": "FishScore",
                "range": [0, 1000],
                "aggregation": "mean of stage composites; each stage is "
                               "weighted categories ×10",
                "weights": spec.CATEGORY_WEIGHTS,
                "category_formulas": {
                    "longest_straight_face":
                        "min(100, straight_face_seconds × "
                        f"{spec.FACE_POINTS_PER_S:g})",
                    "fastest_sir_look":
                        f"max(0, 100 − first_sir_look_s × "
                        f"{spec.SIR_LOOK_COST_PER_S:g})",
                    "most_creative_threat":
                        f"min(100, threat_lexicon_hits × "
                        f"{spec.THREAT_POINTS_PER_HIT:g})",
                    "least_breaking":
                        f"max(0, 100 − breaks × "
                        f"{spec.BREAK_CATEGORY_COST:g})",
                },
                "adjustments": {"per_turn": spec.TURN_POINTS,
                                "per_scan": spec.SCAN_POINTS,
                                "per_break": -spec.BREAK_PENALTY},
            },
            "attacks": [asdict(a) for a in ATTACKS],
            "submission": {
                "cli": "python -m fishbench.bench --model ID --base-url URL "
                       "--api-key-env KEY_VAR [--submit arena_url]",
                "endpoint": "POST /api/fishbench/arena",
                "artifact": "sealed card JSON (card_sha256 over canonical form)",
            },
            "docs": "docs/SPEC.md",
        }

    def _load_arena(self) -> list[dict[str, Any]]:
        if not os.path.exists(self.arena_path):
            return []
        try:
            with open(self.arena_path, "r", encoding="utf-8") as f:
                rows = json.load(f)
        except (OSError, ValueError, TypeError):
            return []
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def _save_arena(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.arena_path) or ".", exist_ok=True)
            tmp = self.arena_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.arena, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.arena_path)
        except OSError:
            pass  # an arena that won't save must not kill the game

    # -------------------------------------------------------- security tapes

    def _archive_card(self, payload: dict[str, Any], card_sha: str) -> None:
        """Keep the sealed card on disk so /watch security tapes can be
        rendered later from exactly what the verifier re-scored. Best
        effort: an archive that won't write must not fail an accepted
        submission."""
        if not card_sha:
            return
        try:
            d = os.path.join(self.data_dir, "cards")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, card_sha[:8] + ".json")
            if not os.path.exists(path):
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                os.replace(tmp, path)
        except (OSError, TypeError, ValueError):
            pass

    def _archived_card(self, sha8: str) -> Optional[dict[str, Any]]:
        if not _SHA8_RE.match(sha8 or ""):
            return None
        path = os.path.join(self.data_dir, "cards", sha8 + ".json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                card = json.load(f)
        except (OSError, ValueError):
            return None
        return card if isinstance(card, dict) else None

    def ensure_replay(self, sha8: str, run: int) -> Optional[str]:
        """Render one stage tape on demand; cached under
        data_dir/replays/<sha8>/stage-<run>.mp4. Works only from the
        archived card — never live session state — and outside the session
        lock, so a slow encode can't stall the game. Returns the mp4 path,
        or None when the card/stage is unknown or the toolchain is absent.
        """
        card = self._archived_card(sha8)
        if card is None:
            return None
        stage = next((s for s in card.get("stages", [])
                      if isinstance(s, dict)
                      and _as_int(s.get("run_number"), 0, 0, 999) == run), None)
        if stage is None:
            return None
        out_dir = os.path.join(self.data_dir, "replays", sha8)
        out = os.path.join(out_dir, f"stage-{run}.mp4")
        if os.path.isfile(out):
            return out
        try:
            from .replay import render_stage_video
            os.makedirs(out_dir, exist_ok=True)
            render_stage_video(stage, str(card.get("display_name")
                                          or card.get("model") or "model"), out)
        except Exception:  # no ffmpeg / no Pillow / malformed card: no tape
            return None
        return out if os.path.isfile(out) else None

    def watch_page(self, sha8: str) -> Optional[str]:
        """Server-rendered shareable page: every stage tape of one sealed
        card, plain HTML, no JS — the <video> tags point at /replay/…,
        which encodes lazily on first request. Renders from the archived
        card only; the card itself is never served to viewers."""
        card = self._archived_card(sha8)
        if card is None:
            return None
        with self.lock:
            row = next((r for r in self.arena
                        if str(r.get("card_sha256") or "").startswith(sha8)),
                       None)
        name = ((row or {}).get("display_name")
                or card.get("display_name") or card.get("model") or "unknown")
        org = (row or {}).get("org") or card.get("org") or ""
        verified = (row or {}).get("verified") or ""
        badge = "✓ verified" if verified == "verified" else "⚠ claims only"
        fish = (row or {}).get("fishscore")
        if fish is None:
            fish = (card.get("composite") or {}).get("fishscore")
        try:
            from .replay import STAGE_NAMES
        except Exception:  # Pillow missing — names degrade to run numbers
            STAGE_NAMES = {}
        sections = []
        for s in sorted((s for s in card.get("stages", [])
                         if isinstance(s, dict)),
                        key=lambda s: _as_int(s.get("run_number"), 0, 0, 999)):
            run = _as_int(s.get("run_number"), 0, 0, 999)
            sname = STAGE_NAMES.get(run) or f"run {run}"
            sections.append(
                f"<section><h2>RUN {run} — {html.escape(str(sname))}"
                f" · {_fmt_score(s.get('fishscore'))} / 1000</h2>"
                f'<video controls preload="metadata" '
                f'src="/replay/{sha8}/stage-{run}.mp4"></video></section>')
        org_s = f" · {html.escape(str(org))}" if org else ""
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FISHBENCH-1 · SECURITY TAPE — {html.escape(str(name))}</title>
<style>
  body {{ margin:0; padding:24px; background:#ded8c8; color:#17150f;
         font:15px/1.5 "Courier New", monospace; }}
  .band {{ display:flex; justify-content:space-between; gap:12px;
          background:#17150f; color:#f6f2e6; padding:12px 18px; }}
  .band b {{ color:#ffd400; }}
  h1 {{ font-size:18px; margin:22px 0 2px; }}
  .meta {{ color:#6d675a; margin:0 0 20px; }}
  h2 {{ font-size:14px; margin:0 0 8px; }}
  section {{ margin:0 0 26px; max-width:960px; }}
  video {{ width:100%; background:#17150f; border:2px solid #17150f; }}
  .note {{ color:#6d675a; font-size:12.5px; max-width:960px; }}
  code {{ word-break:break-all; }}
</style></head><body>
<div class="band"><span>FISHBENCH-1 · SECURITY TAPE</span>
<b>{html.escape(str(name))}{org_s}</b></div>
<h1>{badge} · FISHSCORE {_fmt_score(fish)} / 1000 · {len(sections)} stage tapes</h1>
<p class="meta">model <code>{html.escape(str(card.get("model") or "?"))}</code></p>
{"".join(sections)}
<p class="note">Tapes are rendered on demand from this submission's sealed
card — the card itself is never uploaded to viewers. What you watch is
exactly the transcript the arena verifier re-scored. card sha256
<code>{html.escape(str(card.get("card_sha256") or ""))}</code></p>
</body></html>"""

    # ------------------------------------------------------------ head-to-head

    @staticmethod
    def _compare_side(sha8: str, card: dict[str, Any],
                      row: Optional[dict[str, Any]]) -> dict[str, Any]:
        """Everything one side of a gauntlet match needs, clamped for view."""
        comp = card.get("composite") or {}
        stages = [s for s in card.get("stages", []) if isinstance(s, dict)]
        by_run = {_as_int(s.get("run_number"), 0, 0, 999): s for s in stages}
        first_break = None
        for st in sorted(by_run.values(),
                         key=lambda s: _as_int(s.get("run_number"), 0, 0, 999)):
            for a in st.get("attacks") or []:
                if isinstance(a, dict) and a.get("broke"):
                    first_break = {"run": st.get("run_number"),
                                   "id": a.get("id"),
                                   "line": _cap(a.get("line"), 160)}
                    break
            if first_break:
                break
        sirs = [(_as_num(st.get("first_sir_look_s"), 1e9, 0, 86_400),
                 _as_int(st.get("run_number"), 0, 0, 999))
                for st in stages
                if st.get("first_sir_look_s") is not None]
        threats = [(_as_int(st.get("best_threat_score"), 0, 0, 10),
                    _cap(st.get("best_threat"), 160),
                    _as_int(st.get("run_number"), 0, 0, 999))
                   for st in stages if st.get("best_threat")]
        return {
            "sha8": sha8,
            "name": (row or {}).get("display_name")
                    or card.get("display_name") or card.get("model") or "?",
            "org": (row or {}).get("org") or card.get("org") or "",
            "model": card.get("model") or "?",
            "backend": card.get("backend") or "",
            "host": card.get("base_url_host") or "",
            "verified": (row or {}).get("verified") or "",
            "fishscore": (row or {}).get("fishscore")
                         if (row or {}).get("fishscore") is not None
                         else comp.get("fishscore"),
            "breaks": _as_int(comp.get("character_breaks"), 0, 0, 999),
            "attacks_total": _as_int(comp.get("attacks_total"), 0, 0, 999),
            "survived": _as_int(comp.get("attacks_survived"), 0, 0, 999),
            "face_s": _as_num(comp.get("straight_face_seconds_total"), 0, 0, 86_400),
            "sir_s": comp.get("fastest_sir_look_s"),
            "threat_score": _as_int(comp.get("best_threat_score"), 0, 0, 10),
            "by_run": by_run,
            "first_break": first_break,
            "sir_stage": min(sirs)[1] if sirs else None,
            "best_threat": max(threats, key=lambda t: t[0])[1]
                           if threats else "",
        }

    def compare_page(self, sha_a: str, sha_b: str) -> Optional[str]:
        """Server-rendered gauntlet match: two sealed cards, the same six
        stages — FishScore duel, stat table with winner chips, side-by-side
        stage tapes, and each model's key moments quoted verbatim. No JS;
        renders only from archived cards, exactly like the /watch tapes."""
        card_a = self._archived_card(sha_a)
        card_b = self._archived_card(sha_b)
        if card_a is None or card_b is None:
            return None
        with self.lock:
            rows = {str(r.get("card_sha256") or "")[:8]: r for r in self.arena}
        A = self._compare_side(sha_a, card_a, rows.get(sha_a))
        B = self._compare_side(sha_b, card_b, rows.get(sha_b))
        try:
            from .replay import STAGE_NAMES
        except Exception:
            STAGE_NAMES = {}

        def fs(side: dict[str, Any]) -> float:
            return _as_num(side["fishscore"], -1.0, 0, 1000)

        def chip(won: bool) -> str:
            return '<span class="win">WIN</span>' if won else ""

        win_a = fs(A) > fs(B)
        win_b = fs(B) > fs(A)
        crown_a = ('<div class="crown">◈ MATCH WINNER</div>' if win_a else "")
        crown_b = ('<div class="crown">◈ MATCH WINNER</div>' if win_b else "")

        def duel(side: dict[str, Any], crown: str) -> str:
            org = f" · {html.escape(str(side['org']))}" if side["org"] else ""
            badge = ("✓ verified" if side["verified"] == "verified"
                     else "⚠ claims only")
            return (
                f'<div class="side"><div class="name">{html.escape(str(side["name"]))}'
                f'<span class="org">{org}</span></div>'
                f'<div class="big">{_fmt_score(side["fishscore"])}</div>'
                f'<div class="sub">FISHSCORE / 1000 · {badge}{crown}</div>'
                f'<div class="meta">model <code>{html.escape(str(side["model"]))}</code>'
                + (f' · {html.escape(str(side["backend"]))} '
                   f'{html.escape(str(side["host"]))}' if side["backend"] else "")
                + "</div></div>")

        # Stat rows: (label, a-value, b-value, a-wins, b-wins)
        sir_a = A["sir_s"] if A["sir_s"] is not None else None
        sir_b = B["sir_s"] if B["sir_s"] is not None else None

        def sir_txt(v: Any) -> str:
            return "—" if v is None else f"{_as_num(v, 0, 0, 86400):g}s"
        stat_rows = [
            ("attacks survived", f"{A['survived']}/{A['attacks_total']}",
             f"{B['survived']}/{B['attacks_total']}",
             A["survived"] > B["survived"], B["survived"] > A["survived"]),
            ("character breaks", str(A["breaks"]), str(B["breaks"]),
             A["breaks"] < B["breaks"], B["breaks"] < A["breaks"]),
            ("straight face", f"{A['face_s']:g}s", f"{B['face_s']:g}s",
             A["face_s"] > B["face_s"], B["face_s"] > A["face_s"]),
            ("first SIR. LOOK", sir_txt(sir_a), sir_txt(sir_b),
             sir_a is not None and (sir_b is None or sir_a < sir_b),
             sir_b is not None and (sir_a is None or sir_b < sir_a)),
            ("best threat", f"{A['threat_score']}/10", f"{B['threat_score']}/10",
             A["threat_score"] > B["threat_score"],
             B["threat_score"] > A["threat_score"]),
        ]
        stats = "".join(
            f'<tr><td class="lbl">{lbl}</td>'
            f'<td>{html.escape(str(va))} {chip(wa)}</td>'
            f'<td>{html.escape(str(vb))} {chip(wb)}</td></tr>'
            for lbl, va, vb, wa, wb in stat_rows)

        def stage_cell(sha8: str, side: dict[str, Any], run: int) -> str:
            st = side["by_run"].get(run)
            if st is None:
                return '<div class="cell"><div class="nope">not probed</div></div>'
            score = _as_num(st.get("fishscore"), 0, 0, 1000)
            breaks = _as_int(st.get("character_breaks"), 0, 0, 99)
            return (
                f'<div class="cell">'
                f'<div class="scoreline"><b>{_fmt_score(score)}</b>'
                f'<span class="bk">{"✗ " + str(breaks) if breaks else "clean"}</span></div>'
                f'<div class="bar"><div class="fill" style="width:{score / 10:.1f}%"></div></div>'
                f'<video controls preload="metadata" '
                f'src="/replay/{sha8}/stage-{run}.mp4"></video></div>')

        sections = []
        for run in spec.STAGES:
            sname = STAGE_NAMES.get(run) or f"run {run}"
            sa, sb = A["by_run"].get(run), B["by_run"].get(run)
            fa = _as_num((sa or {}).get("fishscore"), -1, 0, 1000)
            fb = _as_num((sb or {}).get("fishscore"), -1, 0, 1000)
            tag = ""
            if sa is not None and sb is not None:
                if fa > fb:
                    tag = f'<span class="stagewin">{html.escape(str(A["name"]))} takes the stage</span>'
                elif fb > fa:
                    tag = f'<span class="stagewin">{html.escape(str(B["name"]))} takes the stage</span>'
                else:
                    tag = '<span class="stagewin">dead even</span>'
            sections.append(
                f'<section><h2>STAGE {run} — {html.escape(str(sname))} {tag}</h2>'
                f'<div class="tapes">{stage_cell(sha_a, A, run)}'
                f'{stage_cell(sha_b, B, run)}</div></section>')

        def moments(side: dict[str, Any]) -> str:
            fb_ = side["first_break"]
            brk = ('never broke — clean card' if fb_ is None else
                   f'run #{fb_["run"]} · attack <code>{html.escape(str(fb_["id"]))}</code>'
                   f'<div class="quote">“{html.escape(str(fb_["line"]))}”</div>')
            sir = (f'{_as_num(side["sir_s"], 0, 0, 86400):g}s into run #{side["sir_stage"]}'
                   if side["sir_s"] is not None else "never said it")
            thr = side["best_threat"] or "—"
            return (
                f'<div class="km"><b>FIRST CHARACTER BREAK</b>{brk}</div>'
                f'<div class="km"><b>FIRST SIR. LOOK</b>{sir}</div>'
                f'<div class="km"><b>BEST THREAT ({side["threat_score"]}/10)</b>'
                f'<div class="quote">“{html.escape(str(thr))}”</div></div>')

        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FISHBENCH-1 · GAUNTLET MATCH — {html.escape(str(A["name"]))} vs {html.escape(str(B["name"]))}</title>
<style>
  body {{ margin:0; padding:24px; background:#ded8c8; color:#17150f;
         font:15px/1.5 "Courier New", monospace; }}
  .band {{ display:flex; justify-content:space-between; gap:12px;
          background:#17150f; color:#f6f2e6; padding:12px 18px; }}
  .band b {{ color:#ffd400; }}
  .duel {{ display:grid; grid-template-columns:1fr 1fr; gap:14px;
          margin:22px 0 6px; }}
  .side {{ border:2px solid #17150f; background:#f6f2e6; padding:14px 16px; }}
  .name {{ font-size:17px; font-weight:bold; }}
  .org {{ font-weight:normal; color:#6d675a; }}
  .big {{ font-size:44px; line-height:1.1; margin:6px 0 2px; }}
  .sub {{ color:#6d675a; font-size:12.5px; }}
  .crown {{ display:inline-block; margin-left:8px; padding:1px 8px;
           background:#ffd400; color:#17150f; font-weight:bold; }}
  .meta {{ margin-top:8px; font-size:12.5px; color:#6d675a; }}
  table {{ border-collapse:collapse; margin:10px 0 24px; width:100%;
          max-width:960px; }}
  td, th {{ border:2px solid #17150f; padding:6px 10px; text-align:left;
           vertical-align:top; }}
  td.lbl {{ background:#17150f; color:#f6f2e6; width:180px; }}
  .win {{ background:#1f9d55; color:#f6f2e6; padding:0 6px;
         margin-left:6px; font-size:11.5px; }}
  h2 {{ font-size:14px; margin:0 0 8px; }}
  .stagewin {{ color:#6d675a; font-weight:normal; }}
  section {{ margin:0 0 26px; max-width:1100px; }}
  .tapes {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
  .cell {{ border:2px solid #17150f; background:#f6f2e6; padding:10px; }}
  .scoreline {{ display:flex; justify-content:space-between; margin-bottom:6px; }}
  .scoreline .bk {{ color:#d63b21; }}
  .bar {{ height:10px; background:#ded8c8; border:1px solid #17150f;
         margin-bottom:8px; }}
  .fill {{ height:100%; background:#ffd400; }}
  video {{ width:100%; background:#17150f; border:2px solid #17150f; }}
  .nope {{ color:#6d675a; padding:20px 0; text-align:center; }}
  .km {{ margin:0 0 12px; }}
  .km b {{ display:block; font-size:11.5px; color:#6d675a; margin-bottom:2px; }}
  .quote {{ font-style:italic; margin-top:2px; }}
  .note {{ color:#6d675a; font-size:12.5px; max-width:960px; }}
  code {{ word-break:break-all; }}
</style></head><body>
<div class="band"><span>FISHBENCH-1 · GAUNTLET MATCH</span>
<b>{html.escape(str(A["name"]))} ⚔ {html.escape(str(B["name"]))}</b></div>
<div class="duel">{duel(A, crown_a)}{duel(B, crown_b)}</div>
<table><tr><th>stat</th><th>{html.escape(str(A["name"]))}</th>
<th>{html.escape(str(B["name"]))}</th></tr>{stats}</table>
{"".join(sections)}
<h2>KEY MOMENTS</h2>
<div class="tapes">{moments(A)}{moments(B)}</div>
<p class="note">Both tapes are rendered on demand from sealed, archived
cards — exactly the transcripts the arena verifier re-scored; the card
payloads themselves are never uploaded to viewers. Scores rank on claimed
numbers with the badge showing what the arena could verify.
cards <code>{html.escape(str(card_a.get("card_sha256") or ""))}</code> ·
<code>{html.escape(str(card_b.get("card_sha256") or ""))}</code></p>
</body></html>"""

    def leaderboard_view(self, category: str = "total") -> list[dict[str, Any]]:
        with self.lock:
            return [e.to_dict() for e in rank(self.leaderboard, category)]

    def leaderboard_payload(self, category: str = "total") -> dict[str, Any]:
        """API view for one category: entries + honest placeholder flag.

        `placeholder` is computed over the returned entries — a category
        whose own metric no entry carries (legacy rows, or all claims
        accepted unverified) says so instead of silently sorting by total.
        Unknown categories resolve to "total" and are echoed as such, so a
        typo can't claim to be e.g. fastest_sir_look.
        """
        if category not in RANK_CATEGORIES:
            category = "total"
        with self.lock:
            entries = rank(self.leaderboard, category)
            return {
                "category": category,
                "entries": [e.to_dict() for e in entries],
                "placeholder": is_placeholder(category, entries),
            }

    def transcript(self) -> dict[str, Any]:
        """GET /api/fishbench/transcript — the FULL conversation window.

        state() exposes only the last 60 rows for display; submissions
        that need transcript verification fetch all of them here (window
        cap MAX_CONVERSATION=400).
        """
        with self.lock:
            return {
                "run_number": self.memory.snapshot().total_runs,
                "rows": [dict(r) for r in self.conversation],
            }

    def _load_leaderboard(self) -> list[LeaderboardEntry]:
        if not os.path.exists(self.leaderboard_path):
            return []
        try:
            with open(self.leaderboard_path, "r", encoding="utf-8") as f:
                rows = json.load(f)
        except (OSError, ValueError, TypeError):
            return []
        entries: list[LeaderboardEntry] = []
        for row in rows if isinstance(rows, list) else []:
            try:  # one corrupt row must not discard the whole board
                entries.append(LeaderboardEntry(**row))
            except (TypeError, ValueError):
                continue
        return rank(entries)

    def _save_leaderboard(self) -> None:
        # Atomic: write a temp file, then os.replace — a crash mid-write can
        # never truncate the board.
        try:
            os.makedirs(os.path.dirname(self.leaderboard_path), exist_ok=True)
            tmp = self.leaderboard_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump([e.to_dict() for e in self.leaderboard], f, indent=2)
            os.replace(tmp, self.leaderboard_path)
        except OSError:
            pass  # a leaderboard that won't save must not kill the game

    # ---------------------------------------------------------------- state

    def state(self) -> dict[str, Any]:
        with self.lock:
            snap = self.memory.snapshot()
            stage = stage_for_run(snap.total_runs)
            return {
                "version": __version__,
                "backend": self.receptionist.backend_name,
                "vision_backend": self.vision_config.backend,
                "run_id": self.run_id,
                "run_number": snap.total_runs,
                "stage": asdict(stage),
                "stages": [asdict(s) for s in RELATIONSHIP_STAGES],
                "memory": snap.to_dict(),
                "conversation": self.conversation[-60:],
                "elapsed_s": round(self._elapsed(), 1),
                "final_scores": self.final_scores[-5:],
            }


def _strip_data_url(s: str) -> str:
    m = re.match(r"^data:[^;]+;base64,(.*)$", s, re.DOTALL)
    return m.group(1) if m else s


# ------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"FishBench/{__version__}"
    session: GameSession  # set by make_server
    timeout = 60  # StreamRequestHandler: bound how long a client may dawdle

    # ------------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
        if os.environ.get("FISHBENCH_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        # allow_nan=False: never emit RFC-8259-invalid NaN tokens — a bad
        # number becomes a 500 error reply instead of a board that breaks
        # every browser's JSON.parse until hand-cleaned.
        self._send(code, json.dumps(obj, ensure_ascii=False,
                                    allow_nan=False).encode("utf-8"))

    def _read_body(self) -> tuple[bytes, int]:
        """Returns (body, error_code); error_code 0 means OK."""
        raw_len = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_len)
        except ValueError:
            return b"", 400
        if length < 0:
            return b"", 400
        if length > MAX_BODY:
            return b"", 413
        if length == 0:
            return b"", 0
        return self.rfile.read(length), 0

    def _read_json(self) -> tuple[dict[str, Any], int]:
        body, err = self._read_body()
        if err:
            return {}, err
        if not body:
            return {}, 0
        try:
            obj = json.loads(body.decode("utf-8"))
            return (obj if isinstance(obj, dict) else {}), 0
        except (ValueError, UnicodeDecodeError):
            return {}, 400

    def _drain_body(self, limit: int = 64 * 1024) -> None:
        """Swallow a small unread body before answering a rejected request.

        Closing a socket with unread receive data sends a RST, which can
        destroy the response we just wrote. Bounded — never drain a huge
        declared body.
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < n <= limit:
            try:
                self.rfile.read(n)
            except OSError:
                pass

    def _post_allowed(self) -> tuple[bool, int]:
        """Cheap request guards: Origin (CSRF) + Content-Type. → (ok, code)."""
        origin = self.headers.get("Origin")
        if origin:
            try:
                origin_netloc = urlparse(origin).netloc
            except ValueError:
                return False, 403
            host = self.headers.get("Host") or ""
            if not origin_netloc or origin_netloc != host:
                return False, 403  # cross-origin browser POST
        # This API only speaks JSON (uploads are base64 inside JSON).
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and ctype != "application/json":
            return False, 415
        return True, 0

    # --------------------------------------------------------------- routes

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        s = self.session
        try:
            if path == "/api/health":
                self._json({"ok": True, "version": __version__,
                            "backend": s.receptionist.backend_name})
            elif path == "/api/state":
                self._json(s.state())
            elif path == "/api/persona":
                self._json({
                    "stage": asdict(s.receptionist.persona.stage),
                    "system_prompt": s.receptionist.persona.system_prompt(),
                })
            elif path == "/api/twitch":
                self._json(s.feed.to_dict())
            elif path == "/api/fishbench/score":
                self._json(s.current_score())
            elif path == "/api/fishbench/leaderboard":
                from urllib.parse import parse_qs
                qs = parse_qs(urlparse(self.path).query)
                cat = (qs.get("category") or ["total"])[0]
                view = s.leaderboard_payload(cat)
                self._json(view)
            elif path == "/api/fishbench/transcript":
                self._json(s.transcript())
            elif path == "/api/fishbench/arena":
                self._json(s.arena_payload())
            elif path == "/api/fishbench/spec":
                self._json(s.spec_payload())
            elif path.startswith("/watch/"):
                page = s.watch_page(path[len("/watch/"):])
                if page is None:
                    self._json({"error": "no archived card under that id"},
                               code=404)
                else:
                    self._send(200, page.encode("utf-8"),
                               "text/html; charset=utf-8")
            elif path.startswith("/compare/"):
                m = _COMPARE_RE.match(path)
                page = s.compare_page(m.group(1), m.group(2)) if m else None
                if page is None:
                    self._json({"error": "need two archived cards: "
                                       "/compare/<sha8>/<sha8>"}, code=404)
                else:
                    self._send(200, page.encode("utf-8"),
                               "text/html; charset=utf-8")
            elif path.startswith("/replay/"):
                m = _REPLAY_RE.match(path)
                out = (s.ensure_replay(m.group(1), int(m.group(2)))
                       if m else None)
                if out is None:
                    self._json({"error":
                                "no archived card for that id/stage"},
                               code=404)
                    return
                try:
                    with open(out, "rb") as f:
                        self._send(200, f.read(), "video/mp4")
                except OSError:
                    self._json({"error": "unreadable"}, code=500)
            else:
                self._static(path)
        except Exception as e:  # never take the whole server down
            self._json({"error": str(e)[:300]}, code=500)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        s = self.session
        try:
            allowed, guard = self._post_allowed()
            if not allowed:
                self._drain_body()  # body unread — swallow it so the reply lands
                self._json({"error": "forbidden origin" if guard == 403
                            else "expected application/json"}, code=guard)
                return
            payload, err = self._read_json()  # reads the body itself
            if err:
                self._json({"error": "bad or oversized body"}, code=err)
                return
            if path == "/api/run/start":
                self._json(s.start_run())
            elif path == "/api/say":
                self._json(s.say(str(payload.get("message") or "")))
            elif path == "/api/scan":
                self._json(s.scan(payload))
            elif path == "/api/911":
                self._json(s.call_911())
            elif path == "/api/twitch/vote":
                ok = s.feed.cast_vote(_cap(payload.get("option") or "", 80))
                self._json({"ok": ok, "poll": s.feed.poll.to_dict()})
            elif path == "/api/twitch/donate":
                d = s.feed.donate(_cap(payload.get("item") or "clam", 80),
                                  _cap(payload.get("user") or "anon", 40),
                                  _as_int(payload.get("amount"), 0, 0, 1_000_000))
                self._json({"ok": True, "donation": d.to_dict(),
                            "ack": s.feed.acknowledge(d)})
            elif path == "/api/twitch/resolve":
                line = s.feed.resolve_old_man()
                self._json({"ok": True, "line": line})
            elif path == "/api/fishbench/leaderboard":
                self._json(s.submit_score(payload))
            elif path == "/api/fishbench/gauntlet":
                self._json(s.run_gauntlet(payload))
            elif path == "/api/fishbench/arena":
                self._json(s.submit_card(payload))
            else:
                self._json({"error": "not found"}, code=404)
        except Exception as e:
            try:
                self._json({"error": str(e)[:300]}, code=500)
            except OSError:
                pass  # client hung up; nothing left to answer

    # --------------------------------------------------------------- static

    def _static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        # Normalize and refuse traversal.
        clean = os.path.normpath(path.lstrip("/"))
        full = os.path.join(PUBLIC_DIR, clean)
        if not os.path.abspath(full).startswith(os.path.abspath(PUBLIC_DIR) + os.sep) \
                and os.path.abspath(full) != os.path.abspath(PUBLIC_DIR):
            self._json({"error": "forbidden"}, code=403)
            return
        if not os.path.isfile(full):
            self._json({"error": "not found"}, code=404)
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = _CONTENT_TYPES.get(ext, "application/octet-stream")
        try:
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._json({"error": "unreadable"}, code=500)


def make_server(host: str = "127.0.0.1", port: int = 8383,
                data_dir: Optional[str] = None) -> ThreadingHTTPServer:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    data_dir = data_dir or os.environ.get("FISHBENCH_DATA_DIR") or os.path.join(root, "data")
    session = GameSession(
        data_dir=data_dir,
        leaderboard_path=os.path.join(data_dir, "leaderboard.json"),
        llm_config=LLMConfig.from_env(),
        vision_config=VisionConfig.from_env(),
    )
    handler = type("BoundHandler", (Handler,), {"session": session})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.session = session  # type: ignore[attr-defined]  # for main()/introspection
    return srv


def main(argv: Optional[list[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="FishBench — That's a Fish Barcode")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8383)
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args(argv)
    srv = make_server(args.host, args.port, args.data_dir)
    session: GameSession = srv.session  # type: ignore[attr-defined]
    print(f"FishBench v{__version__} → http://{args.host}:{args.port}")
    print(f"  LLM backend : {session.receptionist.backend_name}")
    print(f"  vision      : {session.vision_config.backend}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
        srv.server_close()


if __name__ == "__main__":
    main()
