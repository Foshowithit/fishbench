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
    GET  /api/fishbench/leaderboard    ranked entries
    POST /api/fishbench/leaderboard    submit a run score
    GET  /api/health                   liveness

Run:  python -m fishbench.server --port 8383
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse

from . import __version__
from .llm import LLMConfig, Receptionist
from .memory import MemoryStore
from .persona import (
    RELATIONSHIP_STAGES, THE_LINE_911, THE_LINE_IMPRESSED, stage_for_run,
)
from .scoring import (
    FishBenchScorer, LeaderboardEntry, PLACEHOLDER_CATEGORIES, Turn,
    looks_like_sir_look, rank,
)
from .twitch import TwitchFeed
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
    """Coerce a payload number, clamped; never raises."""
    try:
        v = float(value)
    except (TypeError, ValueError):
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
                 vision_config: Optional[VisionConfig] = None):
        self.lock = threading.RLock()
        self.memory = MemoryStore(data_dir)
        self.receptionist = Receptionist(self.memory, llm_config)
        self.vision_config = vision_config or VisionConfig.from_env()
        self.feed = TwitchFeed()
        self.leaderboard_path = leaderboard_path
        self.leaderboard: list[LeaderboardEntry] = self._load_leaderboard()

        self.run_id: str = ""
        self.run_started_at: float = 0.0
        self.scorer: Optional[FishBenchScorer] = None
        self.conversation: list[dict[str, str]] = []
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

    def _append_conv(self, entry: dict[str, str]) -> None:
        """Append one transcript line, keeping the window bounded."""
        entry["text"] = _cap(entry.get("text", ""), MAX_MSG)
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
            self._append_conv({"role": "you", "text": f"[scanned {ev.item}]"})
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
                self._append_conv(dict(ln))
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
        with self.lock:
            default_run = self.memory.snapshot().total_runs
            entry = LeaderboardEntry(
                name=_cap(payload.get("name") or "anonymous", 40),
                model=_cap(payload.get("model") or self.receptionist.backend_name, 60),
                run_number=_as_int(payload.get("run_number"), default_run, 0, 1_000_000),
                score=_as_int(payload.get("score"), 0, -10_000, 10_000_000),
                straight_face_seconds=_as_num(payload.get("straight_face_seconds"), 0.0, 0.0, 86_400.0),
                character_breaks=_as_int(payload.get("character_breaks"), 0, 0, 1_000_000),
                timestamp=time.time(),
            )
            full = rank(self.leaderboard + [entry])
            # Position is computed on the FULL list, before the 100-entry cap.
            position = next((i + 1 for i, e in enumerate(full) if e is entry), len(full))
            self.leaderboard = full[:100]
            self._save_leaderboard()
        return {"ok": True, "position": position}

    def leaderboard_view(self, category: str = "total") -> list[dict[str, Any]]:
        with self.lock:
            return [e.to_dict() for e in rank(self.leaderboard, category)]

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
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

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
                self._json({
                    "category": cat,
                    "entries": s.leaderboard_view(cat),
                    "placeholder": cat in PLACEHOLDER_CATEGORIES,
                })
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
