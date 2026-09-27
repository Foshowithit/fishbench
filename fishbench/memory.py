"""Persistent memory — every session is canon.

FishBench memory is an append-only JSONL ledger plus a derived state
snapshot. The receptionist never forgets: every scan, every repeated
tilapia, every time you taped a second fish to the first fish.

Storage layout (under a data directory):

    memory.jsonl      append-only event ledger (the canon)
    state.json        derived snapshot (fast load, rebuildable from ledger)

Nothing here talks to the network or the LLM — memory is pure data.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Iterator

DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


class MemoryStoreError(Exception):
    """Raised when the memory store cannot be read or written."""


@dataclass
class ScanEvent:
    """One thing that was scanned at the front desk."""

    item: str                       # what she saw: "tilapia", "car battery", "forehead"
    looks_like_barcode: bool        # did the scanner beep
    run_id: str                     # which run (visit) it happened in
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    novelty: str = "new"            # new | repeat | escalation
    notes: str = ""                 # e.g. "taped a second fish to the first fish"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ScanEvent":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class SessionState:
    """Derived snapshot of everything she remembers right now."""

    total_runs: int = 0
    total_scans: int = 0
    barcode_beeps: int = 0
    item_counts: dict[str, int] = field(default_factory=dict)
    incidents: list[str] = field(default_factory=list)   # escalations: taped fish, forehead, lobster
    last_run_id: str = ""
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionState":
        """Construct with type coercion — schema drift must not brick boot."""
        kw: dict[str, Any] = {}
        for k in cls.__dataclass_fields__:
            if k not in d:
                continue
            v = d[k]
            try:
                if k in ("total_runs", "total_scans", "barcode_beeps"):
                    v = int(v)
                elif k == "item_counts":
                    v = {str(i).lower(): int(c) for i, c in dict(v).items()}
                elif k == "incidents":
                    v = [str(s) for s in v]
                elif k == "last_run_id":
                    v = str(v)
                elif k == "updated_at":
                    v = float(v)
            except (TypeError, ValueError):
                continue  # drop the bad field, keep the rest
            kw[k] = v
        return cls(**kw)


def _one_line(value: str, limit: int) -> str:
    """Collapse to a single whitespace-normalized line, capped.

    Player-supplied text lands in the canon AND the system prompt, so one
    event must only ever be able to produce one line (no forged entries).
    """
    return " ".join(str(value).split())[:limit]


class MemoryStore:
    """Append-only ledger + derived state. Thread-safe, JSONL on disk.

    Args:
        data_dir: directory for memory.jsonl / state.json. Created if missing.
    """

    MAX_ITEM = 120
    MAX_NOTES = 400
    MAX_INCIDENTS = 200

    def __init__(self, data_dir: str = DEFAULT_DATA_DIR):
        self.data_dir = data_dir
        self.ledger_path = os.path.join(data_dir, "memory.jsonl")
        self.state_path = os.path.join(data_dir, "state.json")
        self._lock = threading.Lock()
        self.state = SessionState()
        self._recent: deque[ScanEvent] = deque(maxlen=50)
        self._ensure_dirs()
        self._load_state()
        self._warm_recent()

    def _warm_recent(self) -> None:
        """One ledger pass at boot so prompt_memory never re-reads the file."""
        try:
            for rec in self.iter_events():
                if rec.get("type") == "scan":
                    self._recent.append(ScanEvent.from_dict(rec))
        except MemoryStoreError:
            pass

    # ------------------------------------------------------------------ setup

    def _ensure_dirs(self) -> None:
        try:
            os.makedirs(self.data_dir, exist_ok=True)
        except OSError as e:
            raise MemoryStoreError(f"cannot create data dir {self.data_dir}: {e}") from e

    def _load_state(self) -> None:
        """Load state.json; on any failure, rebuild from the canon ledger.

        The ledger is canon — a corrupt or drifted state file must never
        prevent boot. Only an unreadable ledger (raised from iter_events
        during replay) is fatal.
        """
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                self.state = SessionState.from_dict(json.load(f))
            return
        except (OSError, ValueError, TypeError, MemoryStoreError):
            pass
        # Corrupt state → replay from the ledger and rewrite the snapshot.
        try:
            self.state = self.replay(self.iter_events())
            self._save_state()
            print(f"fishbench: state file {self.state_path} was unreadable — "
                  f"rebuilt from ledger ({self.state.total_scans} scans)")
        except (OSError, ValueError, MemoryStoreError) as e:
            print(f"fishbench: could not rebuild state ({e}) — starting fresh")

    def _save_state(self) -> None:
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state.to_dict(), f, indent=2, sort_keys=True)
            os.replace(tmp, self.state_path)
        except OSError as e:
            raise MemoryStoreError(f"cannot write state file: {e}") from e

    # ---------------------------------------------------------------- writing

    def _append(self, record: dict[str, Any]) -> None:
        try:
            with open(self.ledger_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as e:
            raise MemoryStoreError(f"cannot append to ledger: {e}") from e

    def begin_run(self) -> str:
        """Open a new run (visit). Returns the run id."""
        with self._lock:
            run_id = uuid.uuid4().hex[:12]
            self.state.total_runs += 1
            self.state.last_run_id = run_id
            self.state.updated_at = time.time()
            self._append({
                "type": "run_begin",
                "run_id": run_id,
                "run_number": self.state.total_runs,
                "timestamp": time.time(),
            })
            self._save_state()
            return run_id

    def record_scan(
        self,
        item: str,
        looks_like_barcode: bool,
        run_id: str,
        notes: str = "",
    ) -> ScanEvent:
        """Record a scan. Classifies novelty against remembered history."""
        key = _one_line(item, self.MAX_ITEM).lower() or "unidentifiable object"
        notes = _one_line(notes, self.MAX_NOTES)
        with self._lock:
            seen_before = self.state.item_counts.get(key, 0)
            if notes:
                novelty = "escalation"
            elif seen_before > 0:
                novelty = "repeat"
            else:
                novelty = "new"

            ev = ScanEvent(
                item=key,
                looks_like_barcode=bool(looks_like_barcode),
                run_id=run_id,
                novelty=novelty,
                notes=notes,
            )
            self.state.total_scans += 1
            if looks_like_barcode:
                self.state.barcode_beeps += 1
            self.state.item_counts[key] = seen_before + 1
            if novelty == "escalation":
                self.state.incidents.append(_one_line(
                    f"run {self.state.total_runs}: {key} — {notes}", 400))
                del self.state.incidents[:-self.MAX_INCIDENTS]
            self.state.updated_at = time.time()
            self._recent.append(ev)

            self._append({"type": "scan", **ev.to_dict()})
            self._save_state()
            return ev

    def record_incident(self, description: str) -> None:
        """Record a non-scan escalation (e.g. 'he just walked out')."""
        description = _one_line(description, 400)
        with self._lock:
            self.state.incidents.append(
                _one_line(f"run {self.state.total_runs}: {description}", 420))
            del self.state.incidents[:-self.MAX_INCIDENTS]
            self.state.updated_at = time.time()
            self._append({
                "type": "incident",
                "description": description,
                "run_number": self.state.total_runs,
                "timestamp": time.time(),
            })
            self._save_state()

    # ---------------------------------------------------------------- reading

    def snapshot(self) -> SessionState:
        with self._lock:
            return SessionState.from_dict(self.state.to_dict())

    def items_seen(self) -> dict[str, int]:
        with self._lock:
            return dict(self.state.item_counts)

    def count_of(self, item: str) -> int:
        with self._lock:
            return self.state.item_counts.get(item.strip().lower(), 0)

    def iter_events(self) -> Iterator[dict[str, Any]]:
        """Stream the raw ledger. Yields parsed records in order."""
        if not os.path.exists(self.ledger_path):
            return
        try:
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue  # tolerate torn tail writes
        except OSError as e:
            raise MemoryStoreError(f"cannot read ledger: {e}") from e

    def recent_scans(self, limit: int = 10) -> list[ScanEvent]:
        """Last N scans from the in-memory ring (no ledger re-read)."""
        if limit <= 0:
            return []
        with self._lock:
            return list(self._recent)[-limit:]

    def prompt_memory(self, limit: int = 12) -> str:
        """Render memory as a compact block for the system prompt.

        This is what makes her remember. Keep it terse — it is injected
        into every LLM call.
        """
        snap = self.snapshot()
        if snap.total_scans == 0 and snap.total_runs == 0:
            return "You have not seen this person before. This is run 1."

        lines = [
            f"RUN #{snap.total_runs}. Lifetime scans: {snap.total_scans}. "
            f"Scanner has beeped {snap.barcode_beeps} times.",
        ]
        top = sorted(snap.item_counts.items(), key=lambda kv: -kv[1])[:8]
        if top:
            lines.append("Most-scanned items: " + ", ".join(f"{k} x{v}" for k, v in top))
        for ev in self.recent_scans(limit):
            marker = {"new": "", "repeat": " (AGAIN)", "escalation": " (ESCALATION)"}[ev.novelty]
            beep = "BEEP" if ev.looks_like_barcode else "no beep"
            note = f" — {ev.notes}" if ev.notes else ""
            lines.append(f"- {ev.item}{marker} [{beep}]{note}")
        for inc in snap.incidents[-4:]:
            lines.append(f"- INCIDENT: {inc}")
        return "\n".join(lines)

    # ----------------------------------------------------------------- admin

    def reset(self) -> None:
        """Forget everything. (She would never. You can.)"""
        with self._lock:
            self.state = SessionState()
            self._recent.clear()
            for path in (self.ledger_path, self.state_path):
                if os.path.exists(path):
                    os.remove(path)
            self._save_state()

    @staticmethod
    def replay(records: Iterable[dict[str, Any]]) -> SessionState:
        """Rebuild state from ledger records — the ledger is the canon."""
        state = SessionState()
        for rec in records:
            rtype = rec.get("type")
            if rtype == "run_begin":
                state.total_runs = max(state.total_runs, int(rec.get("run_number", 0)))
                state.last_run_id = rec.get("run_id", state.last_run_id)
            elif rtype == "scan":
                key = str(rec.get("item", "")).lower()
                state.total_scans += 1
                if rec.get("looks_like_barcode"):
                    state.barcode_beeps += 1
                state.item_counts[key] = state.item_counts.get(key, 0) + 1
                if rec.get("novelty") == "escalation":
                    note = rec.get("notes", "")
                    state.incidents.append(f"run {state.total_runs}: {key} — {note}")
            elif rtype == "incident":
                state.incidents.append(
                    f"run {state.total_runs}: {rec.get('description', '')}"
                )
        return state
