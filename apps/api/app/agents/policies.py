"""Hard limits for one agent run. The orchestrator enforces all of them."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.core.exceptions import AppError


class AgentLimitError(AppError):
    status_code = 504
    code = "AGENT_LIMIT_EXCEEDED"
    default_message = "The request took too long or needed too many steps."


class AgentCancelledError(AppError):
    status_code = 409
    code = "QUERY_CANCELLED"
    default_message = "The query was cancelled."


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    max_steps: int
    step_timeout_seconds: float
    total_timeout_seconds: float
    execution_timeout_seconds: float
    max_plan_corrections: int = 1
    min_confidence: float = 0.5

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentPolicy:
        return cls(
            # one full pass (17 states) + one plan correction (3 more)
            max_steps=max(settings.max_agent_steps, 22),
            step_timeout_seconds=settings.agent_step_timeout_seconds,
            total_timeout_seconds=settings.agent_total_timeout_seconds,
            execution_timeout_seconds=settings.max_query_seconds + 5,
            min_confidence=settings.min_plan_confidence,
        )
