"""Token budgeting for Context compilation.

Pre-selection may use a cheap estimate, but the final prompt is always
counted through a model adapter (or a conservative local fallback) before it
is returned to the runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .errors import ContextBudgetError


DEFAULT_CONTEXT_WINDOW = 16_384


class TokenCounter(Protocol):
    def count_tokens(self, text: str) -> int:
        """Return the token count used by the target model adapter."""


class ConservativeTokenCounter:
    """Dependency-free, conservative fallback for adapters without a tokenizer.

    One UTF-8 byte is treated as one token.  This can over-count, but never
    relies on the unsafe ``len(text) / 4`` assumption at the final limit
    check.  Production adapters should expose ``count_tokens`` for their
    exact model tokenizer.
    """

    exact = False

    def count_tokens(self, text: str) -> int:
        return max(1, len(str(text).encode("utf-8")))


class CallableTokenCounter:
    exact = True

    def __init__(self, counter):
        self._counter = counter

    def count_tokens(self, text: str) -> int:
        value = int(self._counter(str(text)))
        if value < 0:
            raise ContextBudgetError("model tokenizer returned a negative token count")
        return value


def token_counter_for(model_adapter=None) -> TokenCounter:
    counter = getattr(model_adapter, "count_tokens", None)
    if callable(counter):
        return CallableTokenCounter(counter)
    tokenizer = getattr(model_adapter, "tokenizer", None)
    tokenizer_counter = getattr(tokenizer, "count_tokens", None)
    if callable(tokenizer_counter):
        return CallableTokenCounter(tokenizer_counter)
    encode = getattr(tokenizer, "encode", None)
    if callable(encode):
        return CallableTokenCounter(lambda text: len(encode(text)))
    return ConservativeTokenCounter()


@dataclass(frozen=True)
class BudgetConfig:
    context_window: int = DEFAULT_CONTEXT_WINDOW
    output_tokens: int = 2_048
    tool_tokens: int = 1_024
    safety_tokens: int = 512
    minimum_input_tokens: int = 1_024
    soft_ratio: float = 0.70
    hard_ratio: float = 0.88

    def __post_init__(self):
        integer_fields = (
            "context_window",
            "output_tokens",
            "tool_tokens",
            "safety_tokens",
            "minimum_input_tokens",
        )
        for field_name in integer_fields:
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ContextBudgetError(f"{field_name} must be a non-negative integer")
        if self.context_window <= 0:
            raise ContextBudgetError("context_window must be positive")
        if not 0 < float(self.soft_ratio) < float(self.hard_ratio) < 1:
            raise ContextBudgetError("budget ratios must satisfy 0 < soft_ratio < hard_ratio < 1")
        if self.input_budget < self.minimum_input_tokens:
            raise ContextBudgetError(
                "model budget leaves fewer input tokens than minimum_input_tokens",
                details=self.to_dict(validate=False),
            )

    @property
    def input_budget(self) -> int:
        return self.context_window - self.output_tokens - self.tool_tokens - self.safety_tokens

    @property
    def soft_target(self) -> int:
        return int(self.soft_ratio * self.input_budget)

    @property
    def hard_trigger(self) -> int:
        return int(self.hard_ratio * self.input_budget)

    def to_dict(self, *, validate: bool = True) -> dict[str, int | float]:
        if validate and self.input_budget < self.minimum_input_tokens:
            raise ContextBudgetError("invalid budget configuration")
        return {
            "context_window": self.context_window,
            "output_reserve": self.output_tokens,
            "tool_reserve": self.tool_tokens,
            "safety_buffer": self.safety_tokens,
            "minimum_input_tokens": self.minimum_input_tokens,
            "input_budget": self.input_budget,
            "soft_target": self.soft_target,
            "hard_trigger": self.hard_trigger,
        }


def compute_budget(config: BudgetConfig | int, **overrides) -> dict[str, int | float]:
    """Compute and validate the design's dynamic budget formula.

    Passing an integer is a convenience for model adapters that only declare
    a context window.  Reserves remain configurable; they are not hidden
    business constants in the compiler.
    """

    if isinstance(config, BudgetConfig):
        if overrides:
            raise TypeError("overrides cannot be used with an existing BudgetConfig")
        value = config
    else:
        value = BudgetConfig(context_window=int(config), **overrides)
    return value.to_dict()


def budget_for_adapter(model_adapter, *, output_tokens=None, legacy_input_tokens=None) -> BudgetConfig:
    window = getattr(model_adapter, "context_window", None)
    if window:
        requested_output = int(output_tokens or getattr(model_adapter, "max_output_tokens", 2_048))
        requested_output = min(requested_output, max(1, int(window) // 3))
        tool_tokens = min(1_024, max(256, int(window) // 16))
        safety_tokens = min(512, max(128, int(window) // 32))
        minimum = min(1_024, max(256, int(window) // 16))
        return BudgetConfig(
            context_window=int(window),
            output_tokens=requested_output,
            tool_tokens=tool_tokens,
            safety_tokens=safety_tokens,
            minimum_input_tokens=minimum,
        )

    # The local CLI historically exposed an input-only character budget.
    # Give the compatibility adapter enough room for that input while still
    # representing every reserve explicitly.
    legacy_input_tokens = max(3_000, int(legacy_input_tokens or 3_000))
    return BudgetConfig(
        context_window=legacy_input_tokens + 2_048 + 1_024 + 512,
        output_tokens=2_048,
        tool_tokens=1_024,
        safety_tokens=512,
        minimum_input_tokens=min(1_024, legacy_input_tokens),
    )
