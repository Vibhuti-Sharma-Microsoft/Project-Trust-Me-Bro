from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .models import Contract, ModelRole, TriScore


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


class ModelEndpoint(Contract):
    model: str
    endpoint_env: str
    auth_env: str
    auth_header: Literal["Authorization", "api-key"] = "Authorization"
    protocol: Literal["openai_chat", "openai_responses"] = "openai_chat"
    approved_for_incident_data: bool = False
    allowed_host: str


class TrustRule(Contract):
    id: str
    source_kind: str
    origin_prefix: str
    maximum_score: TriScore
    instructions: str


class EvaluationConfig(Contract):
    policy_version: str = "poc-v1"
    weights: dict[str, int] = Field(default_factory=lambda: {"faithfulness": 35, "coverage": 35, "source_trust": 20, "freshness": 10})
    models: dict[ModelRole, ModelEndpoint] = Field(default_factory=dict)
    trust_rules: list[TrustRule] = Field(default_factory=list)
    allowed_document_hosts: list[str] = Field(default_factory=list)
    document_auth_env: str | None = None
    fresh_days: Literal[365] = 365
    partial_days: Literal[1095] = 1095
    request_timeout_seconds: float = Field(default=60, gt=0)
    max_attempts: int = Field(default=2, ge=1, le=4)
    max_request_characters: int = Field(default=200_000, gt=0)
    max_document_bytes: int = Field(default=2_000_000, gt=0)

    @model_validator(mode="after")
    def valid_policy(self) -> EvaluationConfig:
        if set(self.weights) != {"faithfulness", "coverage", "source_trust", "freshness"}:
            raise ValueError("Exactly the four v1 dimension weights are required")
        if any(type(n) is not int or n <= 0 for n in self.weights.values()) or sum(self.weights.values()) != 100:
            raise ValueError("Positive integer weights must sum to 100")
        if self.weights["faithfulness"] != self.weights["coverage"] or not (
            self.weights["faithfulness"] > self.weights["source_trust"] > self.weights["freshness"]
        ):
            raise ValueError("Faithfulness and coverage must be equal, then trust, then freshness")
        if self.partial_days < self.fresh_days:
            raise ValueError("Partial freshness boundary cannot precede fresh boundary")
        if len({rule.id for rule in self.trust_rules}) != len(self.trust_rules):
            raise ValueError("Trust rule IDs must be unique")
        return self

    @property
    def policy_sha256(self) -> str:
        return digest(self.model_dump(mode="json"))


def load_config(path: Path | None) -> EvaluationConfig:
    if path is None:
        return EvaluationConfig()
    return EvaluationConfig.model_validate_json(path.read_text(encoding="utf-8-sig"))
