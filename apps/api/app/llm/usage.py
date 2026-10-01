"""Per-run LLM usage accounting.

Providers report every completed model call with :func:`record_llm_usage`; whoever
wants the totals for a unit of work (one analytics question) wraps it in
:func:`track_llm_usage`. A ``ContextVar`` carries the accumulator, so concurrent
questions in one process never mix their counts (asyncio tasks inherit the context
of the task that created them).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class TokenUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


_current: ContextVar[TokenUsage | None] = ContextVar("llm_usage", default=None)


def record_llm_usage(prompt_tokens: int, completion_tokens: int) -> None:
    usage = _current.get()
    if usage is not None:
        usage.calls += 1
        usage.prompt_tokens += max(0, prompt_tokens)
        usage.completion_tokens += max(0, completion_tokens)


@contextmanager
def track_llm_usage() -> Iterator[TokenUsage]:
    usage = TokenUsage()
    token = _current.set(usage)
    try:
        yield usage
    finally:
        _current.reset(token)
