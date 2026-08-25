"""GreenBook Agent Runtime command boundary."""

from .correction import CorrectionEvent
from .interpreter import (
    CommandInterpretationError,
    CommandInterpreter,
    LLMCommandInterpreter,
)
from .models import (
    Command,
    ResolvedSemanticItem,
    ResolvedSemanticState,
    CommandContext,
    CommandTarget,
    CommandType,
    DeliverableSegment,
    DeliverableSegmentation,
    InputSpan,
    SpanAssignment,
    SpanGrouping,
    StructuredCommandOutput,
    TargetKind,
    TargetReferenceType,
)
from .semantic_derivation import (
    DerivedSemanticFacts,
    apply_semantic_derivation,
    derive_semantic_facts,
)
from .target import (
    Ambiguous,
    NotFound,
    Resolved,
    Resolver,
    TargetCandidate,
    TargetResolution,
    TargetResolutionStatus,
    TargetResolver,
    UnifiedTargetResolver,
)

__all__ = [
    "Ambiguous",
    "Command",
    "ResolvedSemanticItem",
    "ResolvedSemanticState",
    "CorrectionEvent",
    "DerivedSemanticFacts",
    "CommandContext",
    "CommandInterpretationError",
    "CommandInterpreter",
    "CommandTarget",
    "CommandType",
    "DeliverableSegment",
    "DeliverableSegmentation",
    "apply_semantic_derivation",
    "derive_semantic_facts",
    "InputSpan",
    "SpanAssignment",
    "SpanGrouping",
    "LLMCommandInterpreter",
    "NotFound",
    "Resolved",
    "Resolver",
    "StructuredCommandOutput",
    "TargetCandidate",
    "TargetKind",
    "TargetReferenceType",
    "TargetResolution",
    "TargetResolutionStatus",
    "TargetResolver",
    "UnifiedTargetResolver",
]
