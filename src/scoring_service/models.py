from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, field_validator
from pydantic.functional_validators import BeforeValidator


def _tri_score(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("A dimension score must be a number: 0, 0.5 or 1")
    if value not in (0, 0.5, 1):
        raise ValueError("A dimension score must be exactly 0, 0.5 or 1")
    return float(value)


TriScore = Annotated[float, BeforeValidator(_tri_score), WithJsonSchema({"type": "number", "enum": [0, 0.5, 1]})]
ModelRole = Literal["gpt", "claude", "gemini"]
Stage = Literal["todo_gate", "claims", "step"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EvidenceRef(Contract):
    evidence_id: str
    quote: str = Field(min_length=1)


class EvidenceItem(Contract):
    id: str
    source_kind: str
    origin: str
    content: str
    source_record_id: str = ""
    call_id: str = ""
    observed_at: str | None = None
    completed_at: str | None = None
    query: str = ""
    quality_flags: list[str] = Field(default_factory=list)
    eligible: bool = True


class DocumentSpec(Contract):
    id: str
    url: str
    step_id: str
    version: str | None = None
    snapshot_path: str | None = None
    snapshot_sha256: str | None = None
    last_updated: str | None = None
    historical_version_verified: bool = False
    call_ids: list[str] = Field(default_factory=list)


class DocumentEvidence(Contract):
    id: str
    url: str
    step_id: str
    status: str
    content: str = ""
    content_sha256: str = ""
    last_updated: str | None = None
    retrieved_at: str | None = None
    historical_version_verified: bool = False
    version: str | None = None
    metadata_provenance: str = ""
    reason: str = ""


class ToolCall(Contract):
    id: str
    name: str
    thread_id: str
    trace_id: str
    started_at: str | None = None
    completed_at: str | None = None
    start_record_ids: list[str] = Field(default_factory=list)
    end_record_ids: list[str] = Field(default_factory=list)
    input_raw: str = ""
    output_raw: str = ""
    input: dict[str, Any] | None = None
    status: Literal["PAIRED", "ORPHAN", "DUPLICATE", "AFTER_CUTOFF", "CONFLICT"] = "ORPHAN"
    quality_flags: list[str] = Field(default_factory=list)


class TodoStep(Contract):
    id: str
    title: str = Field(min_length=1)
    description: str = ""
    kind: Literal["evidence", "housekeeping", "conditional"] = "evidence"
    condition: str = ""


class TodoPlan(Contract):
    source_call_id: str
    created_at: str
    steps: list[TodoStep] = Field(min_length=1)
    raw: str


class CaseSpec(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    incident_id: str
    icm_instance: str = "portal.microsofticm.com"
    message_id: str
    thread_id: str
    post_call_id: str
    cutoff: str
    response_path: str
    context_path: str
    log_files: dict[str, str]
    synthetic: bool = False
    response_sha256: str
    mapping_verified: bool
    selection_reason: str
    expected_rows: dict[str, int] = Field(default_factory=dict)
    documents: list[DocumentSpec] = Field(default_factory=list)
    replay_path: str | None = None
    carry_forward_call_ids: list[str] = Field(default_factory=list)
    task_instructions: str = ""
    context_available_at: str | None = None


class CorpusManifest(Contract):
    schema_version: Literal["1"] = "1"
    target_real_cases: int = Field(default=10, ge=1)
    cases: list[CaseSpec] = Field(min_length=1)

    @field_validator("cases")
    @classmethod
    def unique_cases(cls, cases: list[CaseSpec]) -> list[CaseSpec]:
        ids = [case.id for case in cases]
        if len(set(ids)) != len(ids):
            raise ValueError("Case IDs must be unique")
        real_incidents = [(case.icm_instance, case.incident_id) for case in cases if not case.synthetic]
        if len(real_incidents) != len(set(real_incidents)):
            raise ValueError("Select only one first diagnostic response per real incident")
        return cases


class ResponseBundle(Contract):
    case: CaseSpec
    response_text: str
    context: str
    todo: TodoPlan | None
    calls: list[ToolCall]
    evidence: list[EvidenceItem]
    warnings: list[str] = Field(default_factory=list)
    data_sha256: str


class GateVote(Contract):
    decision: Literal["PASS", "FAIL", "INSUFFICIENT_EVIDENCE"]
    rationale: str = Field(min_length=1)
    references: list[EvidenceRef] = Field(default_factory=list)


class Claim(Contract):
    id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    step_id: str | None
    claim_type: Literal["observation", "conclusion", "recommendation", "uncertainty"]
    material: bool = True


class StepBinding(Contract):
    step_id: str
    call_ids: list[str] = Field(default_factory=list)
    disposition: Literal["EVALUATE", "HOUSEKEEPING", "NOT_APPLICABLE", "MISSING_REQUIRED"]
    rationale: str
    condition_evidence: list[EvidenceRef] = Field(default_factory=list)


class ClaimsDecision(Contract):
    claims: list[Claim]
    bindings: list[StepBinding]


class ClaimSupport(Contract):
    claim_id: str
    verdict: Literal["SUPPORTED", "PARTIAL", "UNSUPPORTED", "CONTRADICTED"]
    references: list[EvidenceRef] = Field(default_factory=list)
    rationale: str


class StepJudgment(Contract):
    faithfulness: TriScore
    rationale: str = Field(min_length=1)
    references: list[EvidenceRef] = Field(default_factory=list)
    claim_support: list[ClaimSupport] = Field(default_factory=list)
    source_trust: TriScore | None = None
    trust_rationale: str = ""
    trust_policy_ids: list[str] = Field(default_factory=list)


class JudgeRecord(Contract):
    role: ModelRole
    stage: Stage
    step_id: str | None = None
    model: str
    request_sha256: str
    prompt_sha256: str
    mode: Literal["live", "copilot", "replay", "cache"]
    output: dict[str, Any]


class StepResult(Contract):
    id: str
    title: str
    disposition: str
    included: bool
    call_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    faithfulness: TriScore | None = None
    coverage: TriScore | None = None
    source_trust: TriScore | None = None
    freshness: TriScore | None = None
    freshness_reason: str = ""
    score: float | None = None
    votes: dict[str, float] = Field(default_factory=dict)
    support: list[ClaimSupport] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class CaseResult(Contract):
    case_id: str
    incident_id: str
    message_id: str
    synthetic: bool
    status: Literal["SCORED", "GATE_FAILED", "UNSCORABLE", "JUDGE_ERROR", "IMPORT_ERROR", "NOT_APPLICABLE"]
    score: float | None
    cutoff: str
    response_text: str = ""
    todo: TodoPlan | None = None
    gate_votes: dict[str, GateVote] = Field(default_factory=dict)
    claims: list[Claim] = Field(default_factory=list)
    steps: list[StepResult] = Field(default_factory=list)
    calls: list[ToolCall] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    documents: list[DocumentEvidence] = Field(default_factory=list)
    judges: list[JudgeRecord] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    error: str | None = None
    data_sha256: str = ""
    policy_sha256: str = ""


class BatchResult(Contract):
    schema_version: Literal["1"] = "1"
    run_id: str
    created_at: str
    calibrated: Literal[False] = False
    policy_version: str
    policy_sha256: str
    weights: dict[str, int] = Field(default_factory=lambda: {"faithfulness": 35, "coverage": 35, "source_trust": 20, "freshness": 10})
    target_real_cases: int
    selected_real_cases: int
    results: list[CaseResult]
    runtime_log_file: str | None = None
