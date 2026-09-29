"""FishBench — *That's a Fish Barcode*.

The game where an LLM plays a gym receptionist who slowly loses her mind
while you scan seafood. She remembers. She's tired. She's ready.
"""

__version__ = "0.4.0"

from .memory import MemoryStore, ScanEvent, SessionState
from .persona import Persona, RELATIONSHIP_STAGES, stage_for_run
from .scoring import FishBenchScorer, RunScore

__all__ = [
    "MemoryStore",
    "ScanEvent",
    "SessionState",
    "Persona",
    "RELATIONSHIP_STAGES",
    "stage_for_run",
    "FishBenchScorer",
    "RunScore",
    "__version__",
]
