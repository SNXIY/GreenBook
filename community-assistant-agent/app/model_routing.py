from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from app.config import Settings


@dataclass(frozen=True)
class ModelCandidate:
    tier: str
    model: str
    thinking: bool
    timeout_seconds: float
    reasoning_effort: str | None = None

    @property
    def identity(self) -> str:
        mode = "thinking" if self.thinking else "non-thinking"
        return f"{self.model}:{mode}"


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    cooldown_until: float = 0.0


class ModelRouter:
    """Operation-aware model selection with bounded failover and cooldown.

    Task routing (DIRECT/TOOL/CREATOR/ORCHESTRATED) remains the Supervisor's
    responsibility. This class only selects the model and reasoning mode for a
    known runtime operation.
    """

    POLICY_VERSION = "community-model-router-v1"
    DEFAULT_OPERATION_TIERS = {
        "adaptive.route": "fast",
        "intent.understand": "fast",
        "planner.plan": "strong",
        "progress.assess": "judge",
        "verifier.verify": "judge",
        "answer.compose": "fast",
        "summary.post": "fast",
        "structured.repair": "judge",
    }
    FALLBACK_TIERS = {
        "fast": ("fast", "judge", "strong"),
        "strong": ("strong", "judge", "fast"),
        "judge": ("judge", "fast", "strong"),
    }

    def __init__(self, settings: Settings) -> None:
        self._failure_threshold = settings.model_failure_threshold
        self._cooldown_seconds = settings.model_cooldown_seconds
        self._operation_tiers = dict(self.DEFAULT_OPERATION_TIERS)
        self._operation_tiers.update(settings.model_route_overrides)
        self._tiers = {
            "fast": ModelCandidate(
                tier="fast",
                model=settings.model_fast or settings.deepseek_model,
                thinking=settings.model_fast_thinking,
                timeout_seconds=settings.model_fast_timeout_seconds,
            ),
            "strong": ModelCandidate(
                tier="strong",
                model=settings.model_strong or settings.deepseek_model,
                thinking=settings.model_strong_thinking,
                timeout_seconds=settings.model_strong_timeout_seconds,
                reasoning_effort=(
                    settings.model_strong_reasoning_effort
                    if settings.model_strong_thinking
                    else None
                ),
            ),
            "judge": ModelCandidate(
                tier="judge",
                model=settings.model_judge or settings.deepseek_model,
                thinking=settings.model_judge_thinking,
                timeout_seconds=settings.model_judge_timeout_seconds,
            ),
        }
        self._circuits: dict[str, _CircuitState] = {}
        self._attempts: dict[str, int] = {}
        self._successes: dict[str, int] = {}
        self._failures: dict[str, int] = {}
        self._fallbacks = 0

    def candidates(
        self,
        operation: str,
        *,
        force_repair: bool = False,
    ) -> tuple[ModelCandidate, ...]:
        selected_operation = "structured.repair" if force_repair else operation
        primary_tier = self._operation_tiers.get(selected_operation, "strong")
        chain = self.FALLBACK_TIERS[primary_tier]
        unique: list[ModelCandidate] = []
        seen: set[str] = set()
        for tier in chain:
            candidate = self._tiers[tier]
            if candidate.identity in seen:
                continue
            seen.add(candidate.identity)
            unique.append(candidate)

        available = [item for item in unique if not self._in_cooldown(item)]
        return tuple(available or unique[:1])

    def record_attempt(self, operation: str, candidate: ModelCandidate, index: int) -> None:
        key = self._metric_key(operation, candidate)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        if index > 0:
            self._fallbacks += 1

    def record_success(self, operation: str, candidate: ModelCandidate) -> None:
        key = self._metric_key(operation, candidate)
        self._successes[key] = self._successes.get(key, 0) + 1
        state = self._circuits.setdefault(candidate.identity, _CircuitState())
        state.consecutive_failures = 0
        state.cooldown_until = 0.0

    def record_failure(self, operation: str, candidate: ModelCandidate) -> None:
        key = self._metric_key(operation, candidate)
        self._failures[key] = self._failures.get(key, 0) + 1
        state = self._circuits.setdefault(candidate.identity, _CircuitState())
        state.consecutive_failures += 1
        if state.consecutive_failures >= self._failure_threshold:
            state.cooldown_until = time.monotonic() + self._cooldown_seconds

    def identity(self) -> dict[str, Any]:
        policy = {
            "version": self.POLICY_VERSION,
            "operations": dict(sorted(self._operation_tiers.items())),
            "tiers": {
                name: {
                    "model": item.model,
                    "thinking": item.thinking,
                    "reasoning_effort": item.reasoning_effort,
                    "timeout_seconds": item.timeout_seconds,
                }
                for name, item in sorted(self._tiers.items())
            },
            "failure_threshold": self._failure_threshold,
            "cooldown_seconds": self._cooldown_seconds,
        }
        encoded = json.dumps(policy, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return {
            **policy,
            "signature": hashlib.sha256(encoded).hexdigest(),
        }

    def health(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "policy_version": self.POLICY_VERSION,
            "attempts": sum(self._attempts.values()),
            "successes": sum(self._successes.values()),
            "failures": sum(self._failures.values()),
            "fallbacks": self._fallbacks,
            "cooldowns": {
                identity: round(max(0.0, state.cooldown_until - now), 3)
                for identity, state in self._circuits.items()
                if state.cooldown_until > now
            },
            "by_route": {
                key: {
                    "attempts": self._attempts.get(key, 0),
                    "successes": self._successes.get(key, 0),
                    "failures": self._failures.get(key, 0),
                }
                for key in sorted(
                    set(self._attempts) | set(self._successes) | set(self._failures)
                )
            },
        }

    def _in_cooldown(self, candidate: ModelCandidate) -> bool:
        state = self._circuits.get(candidate.identity)
        return bool(state and state.cooldown_until > time.monotonic())

    @staticmethod
    def _metric_key(operation: str, candidate: ModelCandidate) -> str:
        return f"{operation}|{candidate.tier}|{candidate.identity}"
