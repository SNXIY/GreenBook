"""Durable long-term Memory Runtime.

Memory is a retrieval-augmented decision input, not a replacement for
Task/Execution/Artifact facts.  The package exposes one repository contract,
one retriever, and a conservative write policy.
"""

from .extractor import (
    PreferenceExtraction,
    PreferenceMemoryExtractor,
    PreferenceMemoryService,
    ProceduralMemoryExtractor,
)
from .manager import MemoryManager
from .models import MemoryQuery, MemoryRecord, MemoryStatus, MemoryType
from .policy import MemoryWriteDecision, MemoryWritePolicy
from .preference_retriever import PreferenceRetriever
from .relevance import (
    MemoryRelevanceGate,
    MemoryRelevanceResult,
    ScoredMemory,
    lexical_relevance,
)
from .repository import (
    InMemoryMemoryRepository,
    MemoryRepository,
    PostgresMemoryRepository,
)
from .retriever import MemoryRetriever
from .strategy import StrategyRetriever

__all__ = [
    "InMemoryMemoryRepository",
    "MemoryQuery",
    "MemoryRecord",
    "MemoryRepository",
    "MemoryRetriever",
    "PreferenceRetriever",
    "MemoryRelevanceGate",
    "MemoryRelevanceResult",
    "ScoredMemory",
    "lexical_relevance",
    "MemoryManager",
    "StrategyRetriever",
    "ProceduralMemoryExtractor",
    "PreferenceExtraction",
    "PreferenceMemoryExtractor",
    "PreferenceMemoryService",
    "MemoryType",
    "MemoryStatus",
    "MemoryWriteDecision",
    "MemoryWritePolicy",
    "PostgresMemoryRepository",
]
