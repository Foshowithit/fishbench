"""LLM abstraction — the part where the model *becomes* Denise.

Two backends:

    offline   deterministic, keyless, ships with the repo. The demo runs with
              no API key and still remembers and escalates.
    http      any OpenAI-compatible /chat/completions endpoint (DeepSeek,
              OpenAI, Ollama, vLLM...). Set FISHBENCH_LLM_BASE_URL and
              FISHBENCH_LLM_API_KEY, optionally FISHBENCH_LLM_MODEL.

Backend is chosen by configuration, never by guessing.
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .memory import MemoryStore, ScanEvent
from .persona import Persona


class LLMError(Exception):
    """Raised when a backend fails in a way the caller must handle."""


@dataclass
class LLMConfig:
    backend: str = "offline"            # offline | http
    base_url: str = ""                  # e.g. https://api.deepseek.com/v1
    api_key: str = ""
    model: str = "deepseek-v4.1-flash"  # DEEPSEEK FLOOR: never pre-4.1
    timeout: float = 30.0
    temperature: float = 0.9
    # Extra request headers for providers that demand more than a bearer
    # token (e.g. opencode-go requires x-opencode-session). Values here are
    # endpoint routing ids, not secrets — keys still live only in api_key.
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "LLMConfig":
        e = env if env is not None else os.environ
        base_url = (e.get("FISHBENCH_LLM_BASE_URL") or "").strip()
        api_key = (e.get("FISHBENCH_LLM_API_KEY") or "").strip()
        model = (e.get("FISHBENCH_LLM_MODEL") or "deepseek-v4.1-flash").strip()
        backend = (e.get("FISHBENCH_LLM_BACKEND") or "").strip().lower()
        if not backend:
            backend = "http" if base_url else "offline"
        extra_headers: dict[str, str] = {}
        raw_headers = (e.get("FISHBENCH_LLM_HEADERS") or "").strip()
        if raw_headers:
            try:
                parsed = json.loads(raw_headers)
                if isinstance(parsed, dict):
                    extra_headers = {str(k): str(v)
                                     for k, v in parsed.items()}
            except ValueError:
                pass  # not JSON — bench --header flags are the structured path
        return cls(
            backend=backend if backend in ("offline", "http") else "offline",
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            model=model,
            extra_headers=extra_headers,
        )


@dataclass
class LLMReply:
    text: str
    backend: str
    model: str = ""
    latency_ms: int = 0
    broke_character: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


def detect_break(text: str) -> bool:
    """Does this reply leak assistant voice? Delegates to the scorer's regex."""
    from .scoring import detect_character_break
    return detect_character_break(text)


class OfflineBackend:
    """Deterministic, keyless Denise. Good enough to clip.

    Not a dialogue tree — it composes from stage + memory + the actual scan,
    rotates through each stage's line pools so nothing repeats verbatim
    inside the no-repeat window, names the repetition count when you re-scan
    something, and answers differently depending on what you actually said.

    Determinism note: line choice is least-recently-used over each stage's
    pool combinations — same input sequence, same outputs. `self.rng` /
    `seed` are kept for API compatibility; nothing rolls the dice anymore.
    """

    name = "offline"

    # No line comes back verbatim inside this many turns. Kept strictly
    # below the smallest pool combination count in RELATIONSHIP_STAGES
    # (3 lines × (1 + 3 beats) = 12) so rotation always finds a fresh line.
    HISTORY = 10

    _GREETING_RE = re.compile(r"\b(hi|hello|hey|yo|sup|howdy)\b")
    _STOPWORDS = ("sorry", "my bad", "apolog", "stop", "quit", "enough",
                  "why", "thank")
    _SEAFOOD = ("lobster", "crab", "shrimp", "salmon", "oysters", "oyster",
                "clam", "tilapia", "tuna", "catfish", "fish")

    def __init__(self, persona: Persona, seed: int = 0,
                 history: Optional[int] = None):
        self.persona = persona
        self.rng = random.Random(seed)
        self._recent: deque[str] = deque(maxlen=history or self.HISTORY)
        self._cursors: dict[tuple[str, str], int] = {}
        self._last_used: dict[tuple[str, str], dict[str, int]] = {}
        self._tick = 0
        # GameSession may call us from several request threads at once.
        self._lock = threading.RLock()

    # ------------------------------------------------------------ selection

    def _pick(self, kind: str, fmt: Optional[Callable[[str], str]] = None) -> str:
        """Next line from the stage's pool for `kind`.

        Candidates are every pool line × optional stage beat. Anything said
        inside the no-repeat window is refused outright; among the rest she
        takes the least-recently-used one, so the whole pool gets an even
        rotation instead of cycling two favorites.
        """
        stage = self.persona.stage
        pool = stage.pool(kind)
        if not pool:
            return ""
        apply = fmt or (lambda s: s)
        key = (stage.name, kind)
        with self._lock:
            start = self._cursors.get(key, 0)
            beats = stage.pool("beat")
            bases = [apply(pool[(start + offset) % len(pool)])
                     for offset in range(len(pool))]
            # Raw lines first, then raw+beat combinations — when a raw line is
            # blocked (say, because the run-start greeting was seeded into the
            # window), she moves to a different line, not the same one padded.
            candidates = list(bases)
            candidates.extend(
                f"{base} ({beat})" for beat in beats for base in bases
            )

            fresh = [c for c in candidates if c not in self._recent]
            if fresh:
                last = self._last_used.setdefault(key, {})
                # min() is stable: never-used lines keep rotation order.
                chosen = min(fresh, key=lambda c: last.get(c, -1))
                last[chosen] = self._tick
                self._tick += 1
                self._cursors[key] = (start + 1) % len(pool)
                return chosen

            # Every combination is inside the window (only possible for a
            # hand-made stage with tiny pools): take the oldest raw line.
            def age(offset: int) -> int:
                line = apply(pool[(start + offset) % len(pool)])
                try:
                    return self._recent.index(line)
                except ValueError:
                    return -1
            offset = min(range(len(pool)), key=age)
            self._cursors[key] = (start + offset + 1) % len(pool)
            return apply(pool[(start + offset) % len(pool)])

    def remember(self, line: str) -> None:
        """Add a Denise line to the no-repeat window (e.g. the run-start
        greeting, which the server composes without a backend)."""
        with self._lock:
            if line:
                self._recent.append(line)

    # -------------------------------------------------------------- replies

    def complete(self, system: str, messages: list[dict[str, str]], **_: Any) -> LLMReply:
        user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        text = self._reply(user)
        self.remember(text)
        return LLMReply(text=text, backend=self.name)

    @staticmethod
    def _count_clause(count: int) -> str:
        """Parenthetical naming how many times she's seen this exact item."""
        words = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
                 7: "seven", 8: "eight", 9: "nine"}
        return f"(that's {words.get(count, count)}.)"

    def _reply(self, user: str) -> str:
        u = " ".join(user.lower().split())

        if u.startswith("scan:"):
            return self._scan_reply()

        # Apologies, stops, thanks, whys — she deflects, in stage voice.
        if any(w in u for w in self._STOPWORDS):
            return self._pick("deflect")

        if "barcode" in u or "beep" in u:
            return self._pick("beep")

        # They're asking about the thing they always scan.
        if any(w in u for w in ("again", "last time", "remember", "same one")):
            top = sorted(self.persona.memory.snapshot().item_counts.items(),
                         key=lambda kv: -kv[1])
            if top:
                return f"{self._pick('repeat')} The {top[0][0]} — again."
            return self._pick("deflect")

        # The hypothetical-seafood question ("what if I scanned a lobster?").
        hit = next((w for w in self._SEAFOOD if w in u), None)
        if hit:
            return self._pick("ask", fmt=lambda s: s.format(item=hit))

        # Walking up / saying hi / a fresh run start.
        if not u or self._GREETING_RE.search(u) or "walk in" in u:
            return self._pick("greeting")

        # Anything else: a dry in-character reaction to what you actually said.
        return self._pick("deflect")

    def _scan_reply(self) -> str:
        """A scan happened. Rotates pools AND names the repetition count, so
        scanning the same fish twice can never produce the same line twice."""
        scans = self.persona.memory.recent_scans(1)
        ev: Optional[ScanEvent] = scans[0] if scans else None
        if ev is None:
            return self._pick("beep")

        if ev.novelty == "escalation":
            line = self.persona.react_to_scan(ev)
        elif ev.novelty == "repeat":
            line = self._pick("repeat")
        elif ev.looks_like_barcode:
            line = self._pick("beep")
        else:
            line = f"A {ev.item}. Of course. That's a new one."

        count = self.persona.memory.count_of(ev.item)
        if count >= 2:
            line = f"{line} {self._count_clause(count)}"
        return line


class HTTPBackend:
    """OpenAI-compatible /chat/completions client. Stdlib only."""

    name = "http"

    def __init__(self, config: LLMConfig):
        if not config.base_url:
            raise LLMError("FISHBENCH_LLM_BASE_URL is not set")
        self.config = config

    def complete(self, system: str, messages: list[dict[str, str]], **_: Any) -> LLMReply:
        import time

        payload = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        req = urllib.request.Request(
            f"{self.config.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}),
                **self.config.extra_headers,
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            text = body["choices"][0]["message"]["content"] or ""
            if not isinstance(text, str):
                raise TypeError(f"content is {type(text).__name__}, not str")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise LLMError(f"HTTP {e.code} from LLM endpoint: {detail}") from e
        except Exception as e:
            # ConnectionReset, socket.timeout, RemoteDisconnected, IncompleteRead,
            # malformed JSON, non-string content — all of them must land HERE so
            # Receptionist.say can fall back to offline Denise instead of 500ing.
            raise LLMError(f"LLM endpoint failure: {e}") from e

        latency = int((time.monotonic() - started) * 1000)
        return LLMReply(
            text=text.strip(),
            backend=self.name,
            model=self.config.model,
            latency_ms=latency,
            broke_character=detect_break(text),
        )


class Receptionist:
    """Facade the game server talks to: memory + persona + one backend."""

    def __init__(self, memory: MemoryStore, config: Optional[LLMConfig] = None):
        self.memory = memory
        self.persona = Persona(memory)
        self.config = config or LLMConfig.from_env()
        self.degraded_reason: str = ""
        # One offline Denise per receptionist: her no-repeat history has to
        # survive an HTTP failure, so the fallback reuses THIS instance
        # instead of constructing a fresh one mid-conversation.
        self.offline = OfflineBackend(self.persona)
        if self.config.backend == "http":
            try:
                self.backend: OfflineBackend | HTTPBackend = HTTPBackend(self.config)
            except LLMError as e:
                # Misconfigured boot (backend=http, no URL): warn and run offline
                # instead of dying with a traceback.
                self.degraded_reason = str(e)
                print(f"fishbench: {e} — falling back to offline Denise")
                self.backend = self.offline
        else:
            self.backend = self.offline

    @property
    def backend_name(self) -> str:
        return self.backend.name

    def note_line(self, text: str) -> None:
        """Record a Denise line produced outside any backend (the run-start
        greeting) so offline rotation never hands it back verbatim."""
        self.offline.remember(text)

    def say(self, user_message: str) -> LLMReply:
        """One turn: inject memory as system prompt, get Denise's reply."""
        system = self.persona.system_prompt()
        messages = [{"role": "user", "content": user_message}]
        try:
            reply = self.backend.complete(system, messages)
        except Exception:
            # Any backend failure — LLMError, socket error, parse bug — degrades
            # to offline Denise. The demo never dies because a key expired.
            if not isinstance(self.backend, HTTPBackend):
                raise
            reply = self.offline.complete(system, messages)
            reply.meta["degraded"] = True
            reply.meta["reason"] = "http backend failed; fell back to offline Denise"
        reply.broke_character = reply.broke_character or detect_break(reply.text)
        return reply

    def react_to_scan(self, event: ScanEvent) -> LLMReply:
        """A scan happened. Denise comments."""
        line = f"SCAN: {event.item}"
        if event.notes:
            line += f" ({event.notes})"
        if event.looks_like_barcode:
            line += " [beep]"
        reply = self.say(line)
        reply.meta["event_id"] = event.event_id
        reply.meta["novelty"] = event.novelty
        return reply
