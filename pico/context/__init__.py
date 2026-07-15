"""Scoped, policy-driven Context compilation, recovery, and memory APIs.

The compiler is the sole prompt-assembly path.  This package also exposes
recoverable session compaction and scoped long-term-memory operations; all
three surfaces fail closed when identity, authorization, or provenance is
incomplete.
"""

from .artifact_store import ArtifactExpiredError, ArtifactIntegrityError, ArtifactNotFoundError
from .block import ContextBlock, ContextScope
from .budget import BudgetConfig, compute_budget
from .compiler import CompileRequest, CompiledContext, ContextCompiler, compile_context
from .compression import (
    CompactSessionResult,
    CompactionCheckpointError,
    CompactionConfigurationError,
    CompactionReferenceError,
    CompressionService,
    SessionCompactionError,
    compact_session,
)
from .errors import (
    ContextBudgetError,
    ContextConfigurationError,
    ContextError,
    ContextPermissionError,
    ContextScopeError,
    ContextTooLargeError,
    MissingRequiredBlocksError,
)
from .manifest import ContextManifest
from .memory import (
    DuplicateHint,
    MemoryAuthorizationError,
    MemoryCandidateExtractionResult,
    MemoryExtractionError,
    MemoryRetrievalResult,
    MemoryRetrievalTrace,
    RejectedSignal,
    extract_memory_candidates,
    retrieve_memory,
)
from .skill import LoadedSkill, SkillMetadata, SkillRegistry, load_skill
from .transcript import TranscriptPersistenceError, TranscriptRecord, TranscriptStore

__all__ = [
    "BudgetConfig",
    "ArtifactExpiredError",
    "ArtifactIntegrityError",
    "ArtifactNotFoundError",
    "CompactSessionResult",
    "CompileRequest",
    "CompiledContext",
    "CompactionCheckpointError",
    "CompactionConfigurationError",
    "CompactionReferenceError",
    "CompressionService",
    "ContextBlock",
    "ContextBudgetError",
    "ContextCompiler",
    "ContextConfigurationError",
    "ContextError",
    "ContextManifest",
    "ContextPermissionError",
    "ContextScope",
    "ContextScopeError",
    "ContextTooLargeError",
    "DuplicateHint",
    "LoadedSkill",
    "MemoryAuthorizationError",
    "MemoryCandidateExtractionResult",
    "MemoryExtractionError",
    "MemoryRetrievalResult",
    "MemoryRetrievalTrace",
    "MissingRequiredBlocksError",
    "RejectedSignal",
    "SessionCompactionError",
    "SkillMetadata",
    "SkillRegistry",
    "TranscriptRecord",
    "TranscriptPersistenceError",
    "TranscriptStore",
    "compact_session",
    "compile_context",
    "compute_budget",
    "extract_memory_candidates",
    "load_skill",
    "retrieve_memory",
]
