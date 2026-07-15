"""Structured failures raised by the Context subsystem.

Context assembly is a security boundary.  Callers must be able to
distinguish a missing fact (which can lead to a clarification turn) from an
invalid budget or a permission failure (which must fail closed), so the
public errors carry a stable code and machine-readable details.
"""

from __future__ import annotations

from typing import Any


class ContextError(RuntimeError):
    """Base class for failures that are safe to expose to an orchestrator."""

    code = "context_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "details": dict(self.details)}


class ContextConfigurationError(ContextError):
    code = "context_configuration_error"


class ContextBudgetError(ContextConfigurationError):
    code = "invalid_context_budget"


class ContextTooLargeError(ContextError):
    code = "context_too_large"


class MissingRequiredBlocksError(ContextError):
    code = "missing_required_context"

    def __init__(self, recipe_id: str, missing: list[str] | tuple[str, ...]):
        missing = tuple(sorted({str(item) for item in missing}))
        super().__init__(
            f"recipe {recipe_id!r} is missing required context: {', '.join(missing)}",
            details={"recipe_id": recipe_id, "missing": list(missing), "action": "clarify_or_fetch"},
        )
        self.recipe_id = recipe_id
        self.missing = missing


class ContextPermissionError(ContextError):
    code = "context_permission_denied"


class ContextScopeError(ContextError):
    code = "invalid_context_scope"


class ArtifactIntegrityError(ContextError):
    code = "artifact_integrity_error"
