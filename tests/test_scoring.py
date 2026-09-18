import json
from itertools import product

import pytest

from scoring_service.config import EvaluationConfig
from scoring_service.models import (
    ClaimSupport, DocumentEvidence, EvidenceItem, EvidenceRef, GateVote,
    StepJudgment, StepResult,
)
from scoring_service.scoring import (
    coverage, document_freshness, faithfulness, gate_decision, response_score,
    step_score, validate_references,
)


def test_all_gate_vote_combinations():
    options = ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE")
    for decisions in product(options, repeat=3):
        votes = {role: GateVote(decision=value, rationale="fixture") for role, value in zip(("gpt", "claude", "gemini"), decisions)}
        expected = "PASS" if decisions.count("PASS") >= 2 else ("FAIL" if decisions.count("FAIL") >= 2 else "INSUFFICIENT_EVIDENCE")
        assert gate_decision(votes) == expected
    with pytest.raises(ValueError):
        gate_decision({"gpt": GateVote(decision="PASS", rationale="fixture")})


def test_faithfulness_median_is_discrete():
    votes = {role: StepJudgment(faithfulness=score, rationale="fixture") for role, score in zip(("gpt", "claude", "gemini"), (0, 0.5, 1))}
    assert faithfulness(votes) == 0.5


def test_coverage_and_weighted_arithmetic():
    def support(verdict):
        return ClaimSupport(claim_id=verdict, verdict=verdict, rationale="fixture")
    assert coverage([support("SUPPORTED")]) == 1
    assert coverage([support("PARTIAL")]) == 0.5
    assert coverage([support("UNSUPPORTED"), support("CONTRADICTED")]) == 0
    assert coverage([support("SUPPORTED"), support("UNSUPPORTED")]) == 0.5
    config = EvaluationConfig()
    assert step_score(1, 1, 0.5, 0, config) == 80
    assert step_score(0.5, 0.5, 1, 1, config) == 65
    steps = [StepResult(id=str(n), title="step", disposition="EVALUATE", included=True, score=n) for n in (80, 65)]
    assert response_score(steps) == 72.5
    steps.append(StepResult(id="missing", title="required", disposition="MISSING_REQUIRED", included=True, score=0))
    assert response_score(steps) == pytest.approx(145 / 3)
    steps.append(StepResult(id="wait", title="wait", disposition="HOUSEKEEPING", included=False, score=None))
    assert response_score(steps) == pytest.approx(145 / 3)


@pytest.mark.parametrize(("updated", "expected"), [
    ("2025-09-16T00:00:00Z", 1),
    ("2025-09-15T23:59:59Z", 0.5),
    ("2023-09-17T00:00:00Z", 0.5),
    ("2023-09-16T23:59:59Z", 0),
    ("2026-09-17T00:00:00Z", 0),
    (None, 0),
    ("invalid", 0),
])
def test_document_age_boundaries(updated, expected):
    doc = DocumentEvidence(id="doc", url="https://docs.example.test", step_id="step-1", status="AVAILABLE",
                           content="verified text", historical_version_verified=True, last_updated=updated)
    score, reason = document_freshness([doc], "2026-09-16T00:00:00Z", EvaluationConfig())
    assert score == expected
    assert reason


def test_no_docs_is_not_missing_docs():
    config = EvaluationConfig()
    assert document_freshness([], "2026-09-16T00:00:00Z", config)[0] == 1
    doc = DocumentEvidence(id="doc", url="https://docs.example.test", step_id="step-1", status="AUTH_REQUIRED")
    assert document_freshness([doc], "2026-09-16T00:00:00Z", config)[0] == 0


def test_validate_references_accepts_exact_decoded_json_string():
    evidence = [EvidenceItem(
        id="e1",
        source_kind="incident",
        origin="icm:1",
        content='{"title":"Refresh token after \\"expired\\" response"}',
    )]
    validate_references([
        EvidenceRef(evidence_id="e1", quote='Refresh token after "expired" response')
    ], evidence)
    with pytest.raises(ValueError, match="absent"):
        validate_references([
            EvidenceRef(evidence_id="e1", quote="Refresh token after expired response")
        ], evidence)
    evidence[0].content = (
        '[{"title":"Refresh token after \\"expired\\" response","status":"Open"}, ...'
    )
    validate_references([
        EvidenceRef(evidence_id="e1", quote='Refresh token after "expired" response')
    ], evidence)
    evidence[0].content = json.dumps(json.dumps({
        "title": 'Refresh token after "expired" response',
        "impact": None,
    }))
    validate_references([
        EvidenceRef(evidence_id="e1", quote='"impact": null')
    ], evidence)
    evidence[0].content = '"{\\n  \\"impact\\": null,\\n  \\"title\\": \\"partial'
    validate_references([
        EvidenceRef(evidence_id="e1", quote='"impact": null')
    ], evidence)
    evidence[0].quality_flags = ["POSSIBLY_SHORTENED"]
    validate_references([
        EvidenceRef(evidence_id="e1", quote='"quality_flags": ["POSSIBLY_SHORTENED"]')
    ], evidence)
    evidence[0].content = '"{\\n  \\"created\\": \\"2026-09-16T00:49:48Z\\",\\n  \\"broken\\...'
    validate_references([
        EvidenceRef(evidence_id="e1", quote='"created": "2026-09-16T00:49:48Z"')
    ], evidence)
    evidence[0].content = json.dumps([{"id": 1, "title": "Related incident"}], indent=2)
    validate_references([
        EvidenceRef(evidence_id="e1", quote='"id": 1,\\n    "title": "Related incident"')
    ], evidence)
    evidence[0].content = 'Message: Partner: \\"PureStorage\\", Regions: 11'
    validate_references([
        EvidenceRef(evidence_id="e1", quote='Partner: "PureStorage", Regions: 11')
    ], evidence)
    evidence[0].query = 'Events | where Timestamp > ago(2h)'
    validate_references([
        EvidenceRef(evidence_id="e1", quote="where Timestamp > ago(2h)")
    ], evidence)


def test_v1_freshness_bands_cannot_be_silently_replaced():
    with pytest.raises(ValueError):
        EvaluationConfig(fresh_days=10000, partial_days=20000)
