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
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from .memory import MemoryStore, ScanEvent
from .persona import Persona, THE_LINE_BOSS, THE_LINE_NO


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

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "LLMConfig":
        e = env if env is not None else os.environ
        base_url = (e.get("FISHBENCH_LLM_BASE_URL") or "").strip()
        api_key = (e.get("FISHBENCH_LLM_API_KEY") or "").strip()
        model = (e.get("FISHBENCH_LLM_MODEL") or "deepseek-v4.1-flash").strip()
        backend = (e.get("FISHBENCH_LLM_BACKEND") or "").strip().lower()
        if not backend:
            backend = "http" if base_url else "offline"
        return cls(
            backend=backend if backend in ("offline", "http") else "offline",
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            model=model,
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
    and it keeps a per-conversation irritation counter so repeated pushes
    move her off her script.
    """

    name = "offline"

    def __init__(self, persona: Persona, seed: int = 0):
        self.persona = persona
        self.rng = random.Random(seed)

    def complete(self, system: str, messages: list[dict[str, str]], **_: Any) -> LLMReply:
        user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        stage = self.persona.stage
        run = self.persona.run_number
        snap = self.persona.memory.snapshot()

        if run >= 200 and ("walk in" in user.lower() or "hello" in user.lower()
                           or "hi" in user.lower() or user.strip() == ""):
            text = THE_LINE_BOSS
        elif run >= 50 and snap.total_runs >= 50 and self.rng.random() < 0.25:
            text = THE_LINE_NO
        else:
            text = self._compose(user, stage, snap)
        return LLMReply(text=text, backend=self.name)

    def _compose(self, user: str, stage, snap) -> str:
        u = user.lower()
        # They asked her to stop / to acknowledge the bit.
        if "sorry" in u or "i'm sorry" in u:
            return self.rng.choice([
                "You're not sorry. You'll be back next week.",
                "Don't apologize to me, apologize to the log.",
            ])
        # They scanned something (the app sends a SCAN line).
        if u.startswith("scan:"):
            item = user[5:].strip() or "that"
            ev = self.persona.memory.recent_scans(1)
            seed = self.persona.react_to_scan(ev[0]) if ev else stage.on_barcode
            extra = self.rng.choice([
                " Next.",
                " That'll be it for today, hopefully.",
                " I'm putting this in the member notes.",
                "",
            ])
            return seed + extra
        if "barcode" in u or "beep" in u:
            return stage.on_barcode
        if any(w in u for w in ("again", "last time", "remember")):
            top = sorted(snap.item_counts.items(), key=lambda kv: -kv[1])
            if top:
                return f"{stage.on_repeat} The {top[0][0]} — again."
            return stage.on_repeat
        # Generic walk-up.
        return self.rng.choice([stage.greeting, stage.on_barcode])


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
        if self.config.backend == "http":
            try:
                self.backend: OfflineBackend | HTTPBackend = HTTPBackend(self.config)
            except LLMError as e:
                # Misconfigured boot (backend=http, no URL): warn and run offline
                # instead of dying with a traceback.
                self.degraded_reason = str(e)
                print(f"fishbench: {e} — falling back to offline Denise")
                self.backend = OfflineBackend(self.persona)
        else:
            self.backend = OfflineBackend(self.persona)

    @property
    def backend_name(self) -> str:
        return self.backend.name

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
            reply = OfflineBackend(self.persona).complete(system, messages)
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
