"""Twitch integration — chat becomes the old man.

Chat votes on his reaction:
    silent_disappointment   he just stands there
    what_do_you_want        "What do you want me to do?"
    inspects_the_fish       he takes the fish and inspects it like evidence

Chat can also donate to spawn seafood. The receptionist acknowledges it:

    "Who sent the crab? Was it it you? Of course it was you."

Pure state machine over vote/donation events — the Twitch socket wiring
(IRC/OAuth) is deliberately stubbed; swap in a real client behind `TwitchFeed`.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

MAX_DONATIONS = 100  # bounded feed — chat can spam

OLD_MAN_REACTIONS: dict[str, str] = {
    "silent_disappointment": (
        "The old man just looks at it. Doesn't say a word. Somehow worse."
    ),
    "what_do_you_want": (
        'The old man: "What do you want me to do?"'
    ),
    "inspects_the_fish": (
        "The old man takes the fish and inspects it like evidence."
    ),
}

# Seafood chat can spawn. The receptionist acknowledges each one.
SPAWNABLE: dict[str, str] = {
    "crab": "Who sent the crab? Was it you? Of course it was you.",
    "lobster": "A lobster. From chat. This is my life now.",
    "shrimp": "Twelve shrimp. Individually wrapped. Thank you, chat.",
    "salmon": "Chat sent salmon. I asked for none of this.",
    "oysters": "Oysters. Fancy. Still not a member card.",
    "clam": "One clam. It's judging me and so is chat.",
    "fish_taped_to_fish": (
        "Someone in chat taped a second fish to the first fish. "
        "I'm adding it to the permanent record."
    ),
}


@dataclass
class VoteTally:
    """Current poll for the old man's reaction."""

    options: tuple[str, ...] = (
        "silent_disappointment",
        "what_do_you_want",
        "inspects_the_fish",
    )
    votes: dict[str, int] = field(default_factory=dict)
    deadline: float = 0.0

    def cast(self, option: str, weight: int = 1) -> bool:
        if option not in self.options or weight < 0:
            return False
        self.votes[option] = self.votes.get(option, 0) + weight
        return True

    @property
    def leader(self) -> Optional[str]:
        if not self.votes:
            return None
        return max(self.votes.items(), key=lambda kv: kv[1])[0]

    def close(self) -> Optional[str]:
        """Winner's reaction line, or None if nobody voted."""
        win = self.leader
        if win is None:
            return None
        return OLD_MAN_REACTIONS[win]

    def to_dict(self) -> dict[str, Any]:
        total = sum(self.votes.values()) or 1
        return {
            "options": [
                {
                    "id": opt,
                    "line": OLD_MAN_REACTIONS[opt],
                    "votes": self.votes.get(opt, 0),
                    "pct": round(100.0 * self.votes.get(opt, 0) / total, 1),
                }
                for opt in self.options
            ],
            "leader": self.leader,
        }


@dataclass
class Donation:
    item: str
    from_user: str
    amount_cents: int = 0
    timestamp: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "from_user": self.from_user,
            "amount_cents": self.amount_cents,
            "timestamp": self.timestamp,
            "id": self.id,
        }


class TwitchFeed:
    """In-memory feed of votes + donations.

    This is the stub boundary: wire a real Twitch IRC client (or a test
    harness) by calling `cast_vote` / `donate` from wherever messages arrive.
    """

    def __init__(self, ack_line_for=None):
        # ThreadingHTTPServer serves requests concurrently — votes, the poll
        # reset, and the donation buffer all mutate shared state.
        self._lock = threading.RLock()
        self.poll = VoteTally()
        self.donations: deque[Donation] = deque(maxlen=MAX_DONATIONS)
        self._ack = ack_line_for or (lambda d: SPAWNABLE.get(
            d.item, f'Nobody warned me about the {d.item}. Thanks, {d.from_user}.'
        ))

    # ---------------------------------------------------------------- votes

    def cast_vote(self, option: str, weight: int = 1, voter: str = "") -> bool:
        """Chat votes on the old man's reaction."""
        with self._lock:
            return self.poll.cast(option, weight)

    def resolve_old_man(self) -> Optional[str]:
        """Close the poll and return the chosen reaction line."""
        with self._lock:
            line = self.poll.close()
            self.poll = VoteTally()  # next beat gets a fresh poll
        return line

    # ------------------------------------------------------------ donations

    def donate(self, item: str, from_user: str, amount_cents: int = 0) -> Donation:
        """Chat spawns seafood. Returns the donation for the log.

        Unknown items are kept as-is — the fallback acknowledgment teases
        the exact (often cursed) thing chat sent.
        """
        key = item.strip().lower().replace(" ", "_") or "clam"
        d = Donation(item=key, from_user=from_user or "anonymous", amount_cents=amount_cents)
        with self._lock:
            self.donations.append(d)
        return d

    def acknowledge(self, donation: Donation) -> str:
        """The receptionist acknowledges the seafood."""
        return self._ack(donation)

    def recent_donations(self, limit: int = 5) -> list[Donation]:
        if limit <= 0:
            return []
        with self._lock:
            return list(self.donations)[-limit:]

    # -------------------------------------------------------------- state

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            poll = self.poll.to_dict()
            recent = [d.to_dict() for d in list(self.donations)[-5:]]
        return {"poll": poll, "recent_donations": recent}


def simulate_chat_turn(feed: TwitchFeed, actions: Iterable[dict]) -> list[str]:
    """Apply a batch of chat actions; returns narration lines (for tests/demo)."""
    lines: list[str] = []
    for a in actions:
        kind = a.get("type")
        if kind == "vote":
            feed.cast_vote(a.get("option", ""), int(a.get("weight", 1)), a.get("voter", ""))
        elif kind == "donate":
            d = feed.donate(a.get("item", "clam"), a.get("user", "anon"), int(a.get("amount", 0)))
            lines.append(feed.acknowledge(d))
        elif kind == "resolve":
            line = feed.resolve_old_man()
            if line:
                lines.append(line)
    return lines
