from __future__ import annotations

import json
import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

from .bundle import build_bundle, payload_flags
from .config import EvaluationConfig
from .documents import DocumentStore
from .imports import safe_path
from .judges import JudgeError, JudgeService
from .runtime_log import RuntimeJournal, RuntimeLogError
from .models import (
    CaseResult, CaseSpec, ClaimsDecision, DocumentEvidence, EvidenceItem,
    GateVote, ModelRole, ResponseBundle, Stage, StepJudgment, StepResult,
    TodoPlan,
)
from .scoring import (
    coverage, document_freshness, faithfulness, gate_decision,
    response_score, step_score, validate_references, validate_trust,
)
from .time_utils import timestamp_ns

log = logging.getLogger(__name__)
ROLES: tuple[ModelRole, ...] = ("gpt", "claude", "gemini")


class PanelFailure(JudgeError):
    def __init__(self, stage: Stage, partial: dict[str, Any], failures: list[str]):
        super().__init__("Incomplete three-model panel: " + "; ".join(failures))
        self.stage = stage
        self.partial = partial


def _emit(journal: RuntimeJournal | None, event: str, case_id: str, **details: Any) -> None:
    if journal is not None:
        journal.event(event, case_id=case_id, **details)


def _judge(
    service: JudgeService, role: ModelRole, stage: Stage, payload: dict[str, Any],
    step_id: str | None = None, journal: RuntimeJournal | None = None,
) -> GateVote | ClaimsDecision | StepJudgment:
    case_id = str(payload["case_id"])
    started = time.perf_counter()
    _emit(journal, "judge.started", case_id, role=role, stage=stage, step_id=step_id, evaluator_input=payload)
    try:
        judgment = service.judge(role, stage, payload, step_id=step_id)
    except (JudgeError, ValueError) as exc:
        _emit(journal, "judge.failed", case_id, role=role, stage=stage, step_id=step_id,
              elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
              error_type=type(exc).__name__, error=str(exc))
        raise
    record = next((entry for entry in reversed(service.records)
                   if entry.role == role and entry.stage == stage and entry.step_id == step_id), None)
    _emit(journal, "judge.returned", case_id, role=role, stage=stage, step_id=step_id,
          elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
          judgment=judgment.model_dump(mode="json"),
          provenance=record.model_dump(mode="json") if record else None,
          note="Schema-valid model response; domain/reference checks still apply before scoring.")
    return judgment


def _panel(
    service: JudgeService, stage: Stage, payload: dict[str, Any],
    step_id: str | None = None, journal: RuntimeJournal | None = None,
) -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {role: pool.submit(_judge, service, role, stage, payload, step_id, journal) for role in ROLES}
        partial = {}
        failures = []
        for role, future in futures.items():
            try:
                partial[role] = future.result()
            except (JudgeError, ValueError) as exc:
                failures.append(f"{role}: {exc}")
        if failures:
            raise PanelFailure(stage, partial, failures)
        return partial


def _validate_claims(decision: ClaimsDecision, bundle: ResponseBundle) -> None:
    if bundle.todo is None:
        raise ValueError("Claims cannot be evaluated without an initial todo")
    steps = {step.id: step for step in bundle.todo.steps}
    calls = {call.id for call in bundle.calls if call.id != bundle.case.post_call_id}
    if len({claim.id for claim in decision.claims}) != len(decision.claims):
        raise ValueError("Claim IDs must be unique")
    for claim in decision.claims:
        if claim.quote not in bundle.response_text:
            raise ValueError("Extracted claim is not an exact quote from the selected response")
        if claim.step_id is not None and claim.step_id not in steps:
            raise ValueError("Claim refers to an unknown logical step")
    if len({binding.step_id for binding in decision.bindings}) != len(decision.bindings) or {binding.step_id for binding in decision.bindings} != set(steps):
        raise ValueError("Every initial todo step requires exactly one disposition")
    for binding in decision.bindings:
        if not set(binding.call_ids) <= calls:
            raise ValueError("Step binding refers to unknown or target-posting calls")
        if binding.disposition == "HOUSEKEEPING" and steps[binding.step_id].kind != "housekeeping":
            raise ValueError("An evidence-bearing todo cannot silently be dropped as housekeeping")
        if binding.disposition == "NOT_APPLICABLE":
            if steps[binding.step_id].kind != "conditional" or not binding.condition_evidence:
                raise ValueError("Conditional exclusion requires an explicit condition and evidence")
            validate_references(binding.condition_evidence, bundle.evidence)


def _step_evidence(bundle: ResponseBundle, call_ids: list[str], docs: list[DocumentEvidence]) -> list[EvidenceItem]:
    items = [item for item in bundle.evidence if item.eligible and (item.call_id in call_ids or item.id == "context")]
    for document in docs:
        items.append(EvidenceItem(
            id=f"doc:{document.id}", source_kind="document", origin=document.url, content=document.content,
            eligible=document.status == "AVAILABLE" and document.historical_version_verified and bool(document.content),
            quality_flags=payload_flags(document.content), observed_at=document.last_updated,
        ))
    return items


def _document_as_of(document: DocumentEvidence, cutoff: str) -> DocumentEvidence:
    if not document.last_updated:
        return document
    try:
        future = timestamp_ns(document.last_updated) > timestamp_ns(cutoff)
    except ValueError:
        return document.model_copy(update={
            "status": "INVALID_DATE", "content": "", "historical_version_verified": False,
            "reason": "Document timestamp is invalid; content withheld from all semantic judges.",
        })
    if future:
        return document.model_copy(update={
            "status": "AFTER_CUTOFF", "content": "", "historical_version_verified": False,
            "reason": "Document version is after the response cutoff; content withheld from all semantic judges.",
        })
    return document


def _evaluate_case(
    case: CaseSpec, root: Path, config: EvaluationConfig, cache_dir: Path,
    mode: str = "replay", journal: RuntimeJournal | None = None,
) -> CaseResult:
    result = CaseResult(case_id=case.id, incident_id=case.incident_id, message_id=case.message_id,
                        synthetic=case.synthetic, status="IMPORT_ERROR", score=None,
                        cutoff=case.cutoff, policy_sha256=config.policy_sha256)
    try:
        bundle = build_bundle(case, root)
    except (ValueError, OSError, UnicodeError) as exc:
        result.error = f"Import failed: {exc}"
        log.error("Case %s import failed: %s", case.id, exc)
        _emit(journal, "import.failed", case.id, error_type=type(exc).__name__, error=str(exc))
        return result
    result.response_text = bundle.response_text
    result.todo = bundle.todo
    result.calls = bundle.calls
    result.evidence = bundle.evidence
    result.data_sha256 = bundle.data_sha256
    result.limitations = list(bundle.warnings)
    _emit(journal, "bundle.prepared", case.id,
          selected_response=bundle.response_text, initial_todo=bundle.todo.model_dump(mode="json") if bundle.todo else None,
          context_available_at=case.context_available_at, cutoff=case.cutoff,
          calls=[call.model_dump(mode="json") for call in bundle.calls],
          evidence=[item.model_dump(mode="json") for item in bundle.evidence],
          warnings=bundle.warnings, data_sha256=bundle.data_sha256,
          note="Normalization and correlation only; downstream quality analysis has not run yet.")
    if bundle.todo is None or any(warning.startswith("CONTEXT_") for warning in bundle.warnings):
        result.status = "UNSCORABLE"
        result.limitations.append("Todo gate stopped: initial plan/context is missing, unreadable, or temporally unverified.")
        _emit(journal, "gate.blocked", case.id, decision="INSUFFICIENT_EVIDENCE",
              reasons=result.limitations, downstream_skipped=True)
        return result
    service: JudgeService | None = None
    try:
        replay = None
        if mode == "replay":
            if not case.replay_path:
                raise JudgeError("No recorded judge responses are configured for replay")
            replay = json.loads(safe_path(root, case.replay_path).read_text(encoding="utf-8-sig"))
        service = JudgeService(config, cache_dir / "judges", mode=mode, replay=replay, allow_unbound_replay=case.synthetic)
        gate_evidence = [
            item for item in bundle.evidence
            if item.id in {"context", "todo", "requirements"} and item.eligible
        ]
        gate_reference_evidence = _gate_reference_evidence(bundle.todo, bundle.evidence)
        gate_payload = {
            "case_id": case.id, "data_sha256": bundle.data_sha256,
            "initial_plan": bundle.todo.model_dump(mode="json"),
            "todo": bundle.todo.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in gate_evidence],
        }
        votes = cast(dict[str, GateVote], _panel(service, "todo_gate", gate_payload, journal=journal))
        for vote in votes.values():
            validate_references(vote.references, gate_reference_evidence)
            if vote.decision in {"PASS", "FAIL"} and not vote.references:
                raise ValueError("A decisive todo vote must cite supplied context or the plan")
        result.gate_votes = votes
        gate = gate_decision(votes)
        _emit(journal, "gate.decided", case.id, decision=gate,
              rule="All three valid responses required; majority PASS/FAIL, otherwise insufficient evidence.",
              votes={role: vote.model_dump(mode="json") for role, vote in votes.items()},
              downstream_skipped=gate != "PASS")
        if gate != "PASS":
            result.status = "GATE_FAILED" if gate == "FAIL" else "UNSCORABLE"
            result.score = 0.0 if gate == "FAIL" else None
            result.limitations.append("Downstream claims, document fetching and step judgments were not invoked.")
            return result
        claims_payload = {
            "case_id": case.id, "data_sha256": bundle.data_sha256, "response_text": bundle.response_text,
            "steps": [step.model_dump(mode="json") for step in bundle.todo.steps],
            "calls": [call.model_dump(mode="json") for call in bundle.calls if call.id != case.post_call_id],
            "evidence": [item.model_dump(mode="json") for item in bundle.evidence if item.eligible],
        }
        decision = cast(ClaimsDecision, _judge(service, "gpt", "claims", claims_payload, journal=journal))
        _validate_claims(decision, bundle)
        result.claims = decision.claims
        _emit(journal, "claims.validated", case.id, claims=decision.model_dump(mode="json"))
        if not any(claim.material for claim in decision.claims):
            result.status = "NOT_APPLICABLE"
            result.limitations.append("The selected response has no material claims.")
            _emit(journal, "response.not_applicable", case.id, reason=result.limitations[-1])
            return result
        document_store = DocumentStore(
            config,
            root,
            cache_dir / "documents",
            mode="replay" if mode == "replay" else "live",
        )
        todo = {step.id: step for step in bundle.todo.steps}
        if any(spec.step_id not in todo for spec in case.documents):
            raise ValueError("A configured document refers to an unknown todo step")
        for binding in decision.bindings:
            step = todo[binding.step_id]
            claims = [claim for claim in decision.claims if claim.material and claim.step_id == step.id]
            step_result = StepResult(id=step.id, title=step.title, disposition=binding.disposition,
                                     included=binding.disposition not in {"HOUSEKEEPING", "NOT_APPLICABLE"},
                                     call_ids=binding.call_ids, claim_ids=[claim.id for claim in claims])
            _emit(journal, "step.started", case.id, step=step.model_dump(mode="json"),
                  binding=binding.model_dump(mode="json"), assigned_claims=[claim.model_dump(mode="json") for claim in claims])
            if not step_result.included:
                step_result.limitations.append(binding.rationale)
                result.steps.append(step_result)
                _emit(journal, "step.excluded", case.id, step=step_result.model_dump(mode="json"),
                      rule="Housekeeping or evidence-backed conditional exclusion; not in the scoring denominator.")
                continue
            if binding.disposition == "MISSING_REQUIRED" or not claims or not binding.call_ids:
                step_result.disposition = "MISSING_REQUIRED"
                step_result.score = 0.0
                step_result.limitations.append("Required work, response claims, or producing calls are missing; denominator retained.")
                result.steps.append(step_result)
                _emit(journal, "step.missing_required", case.id, step=step_result.model_dump(mode="json"),
                      contribution=0, denominator_retained=True)
                continue
            producing = [item for item in bundle.evidence if item.call_id in binding.call_ids and item.eligible]
            if not producing:
                step_result.disposition = "MISSING_REQUIRED"
                step_result.score = 0.0
                step_result.limitations.append("No eligible pre-cutoff producing result exists; context cannot substitute for missing required work.")
                result.steps.append(step_result)
                _emit(journal, "step.missing_required", case.id, step=step_result.model_dump(mode="json"),
                      contribution=0, denominator_retained=True)
                continue
            specs = [spec for spec in case.documents if spec.step_id == step.id]
            docs = [_document_as_of(document_store.fetch(spec, case.cutoff), case.cutoff) for spec in specs]
            doc_calls = [item for item in bundle.evidence if item.call_id in binding.call_ids and item.source_kind == "document"]
            for item in doc_calls:
                matched = any(item.call_id in spec.call_ids or item.origin == spec.url for spec in specs)
                if not matched:
                    docs.append(DocumentEvidence(
                        id=f"unresolved-{step.id}-{item.call_id}", url=item.origin, step_id=step.id, status="UNRESOLVED",
                        reason=f"No matching document descriptor/version for referenced call {item.call_id}",
                    ))
            result.documents.extend(docs)
            _emit(journal, "documents.prepared", case.id, step_id=step.id,
                  documents=[doc.model_dump(mode="json") for doc in docs],
                  note="Current/future or unverified versions cannot become historical source evidence.")
            step_evidence = _step_evidence(bundle, binding.call_ids, docs)
            step_payload = {
                "case_id": case.id, "data_sha256": bundle.data_sha256,
                "step": step.model_dump(mode="json"), "claims": [claim.model_dump(mode="json") for claim in claims],
                "calls": [call.model_dump(mode="json") for call in bundle.calls if call.id in binding.call_ids],
                "evidence": [item.model_dump(mode="json") for item in step_evidence],
                "documents": [doc.model_dump(mode="json") for doc in docs],
                "trust_rules": [rule.model_dump(mode="json") for rule in config.trust_rules],
                "response_cutoff": case.cutoff,
            }
            judgments = cast(dict[str, StepJudgment], _panel(service, "step", step_payload, step.id, journal))
            for judgment in judgments.values():
                validate_references(judgment.references, step_evidence)
                if judgment.faithfulness > 0 and not judgment.references:
                    raise ValueError("Positive faithfulness requires an evidence quotation")
            gpt = judgments["gpt"]
            expected_claims = {claim.id for claim in claims}
            if {support.claim_id for support in gpt.claim_support} != expected_claims or len(gpt.claim_support) != len(expected_claims):
                raise ValueError("GPT must classify every assigned claim exactly once")
            for support in gpt.claim_support:
                validate_references(support.references, step_evidence)
                if support.verdict in {"SUPPORTED", "PARTIAL", "CONTRADICTED"} and not support.references:
                    raise ValueError("A claim-support verdict requires evidence")
            validate_trust(gpt, step_evidence, config)
            f = faithfulness(judgments)
            c = coverage(gpt.claim_support)
            t = gpt.source_trust
            if t is None:
                raise ValueError("Source trust was not returned")
            p, reason = document_freshness(docs, case.cutoff, config)
            step_result.faithfulness, step_result.coverage = f, c
            step_result.source_trust, step_result.freshness = t, p
            step_result.freshness_reason = reason
            step_result.score = step_score(f, c, t, p, config)
            step_result.votes = {role: vote.faithfulness for role, vote in judgments.items()}
            step_result.support = gpt.claim_support
            if len(set(step_result.votes.values())) > 1:
                step_result.limitations.append("Faithfulness judges disagree; displayed score uses the median.")
            step_result.limitations.extend(f"{item.id}: {', '.join(item.quality_flags)}" for item in step_evidence if item.quality_flags)
            step_result.limitations.extend(doc.reason for doc in docs if doc.reason)
            result.steps.append(step_result)
            _emit(journal, "step.scored", case.id, step=step_result.model_dump(mode="json"),
                  calculations={
                      "faithfulness": {"votes": step_result.votes, "operator": "median", "result": f},
                      "coverage": {"verdicts": [support.model_dump(mode="json") for support in gpt.claim_support],
                                   "rule": "All supported -> 1; some supported/partial -> 0.5; none -> 0", "result": c},
                      "source_trust": {"result": t, "rationale": gpt.trust_rationale, "policy_ids": gpt.trust_policy_ids},
                      "document_freshness": {"result": p, "reason": reason, "fresh_days": config.fresh_days,
                                             "partial_days": config.partial_days, "cutoff": case.cutoff},
                      "weighted_terms": {"faithfulness": config.weights["faithfulness"] * f,
                                         "coverage": config.weights["coverage"] * c,
                                         "source_trust": config.weights["source_trust"] * t,
                                         "freshness": config.weights["freshness"] * p},
                      "step_score": step_result.score,
                  })
        excluded_steps = {step.id for step in result.steps if not step.included}
        unassigned = [
            claim.id for claim in decision.claims
            if claim.material and (claim.step_id is None or claim.step_id in excluded_steps)
        ]
        if unassigned:
            result.steps.append(StepResult(id="unassigned-claims", title="Unsupported/unassigned response claims",
                                          disposition="UNASSIGNED", included=True, score=0.0, claim_ids=unassigned,
                                          limitations=["Material claims cannot disappear into excluded housekeeping or conditional steps."]))
            _emit(journal, "claims.unassigned", case.id, claim_ids=unassigned, contribution=0, denominator_retained=True)
        result.score = response_score(result.steps)
        result.status = "SCORED" if result.score is not None else "UNSCORABLE"
        return result
    except (JudgeError, ValueError, OSError) as exc:
        if isinstance(exc, PanelFailure) and exc.stage == "todo_gate":
            result.gate_votes = {role: vote for role, vote in exc.partial.items() if isinstance(vote, GateVote)}
        result.status = "JUDGE_ERROR"
        result.score = None
        result.error = f"Evaluation stopped: {exc}"
        log.error("Case %s evaluation failed: %s", case.id, exc)
        _emit(journal, "evaluation.failed", case.id, error_type=type(exc).__name__, error=str(exc),
              stack_trace=traceback.format_exc(), partial_steps=[step.model_dump(mode="json") for step in result.steps],
              note="Execution error; not a vote and not a zero-scored incorrect response.")
        return result
    finally:
        if service is not None:
            stage_order = {"todo_gate": 0, "claims": 1, "step": 2}
            result.judges = sorted(service.records, key=lambda record: (
                stage_order[record.stage], record.step_id or "", ROLES.index(record.role)))


def _gate_reference_evidence(todo: TodoPlan, bundle_evidence: list[EvidenceItem]) -> list[EvidenceItem]:
    evidence = [
        item for item in bundle_evidence
        if item.id in {"context", "requirements"} and item.eligible
    ]
    todo_texts = [todo.raw]
    for step in todo.steps:
        step_texts = [text for text in (step.title, step.description, step.condition) if text]
        if step.title and step.description:
            step_texts.append(f"{step.title}: {step.description}")
        todo_texts.extend(step_texts)
        evidence.append(EvidenceItem(
            id=f"todo:{step.id}",
            source_kind="initial_plan",
            origin=f"tool:{todo.source_call_id}",
            content="\n".join(step_texts),
            eligible=True,
            observed_at=todo.created_at,
        ))
    evidence.append(EvidenceItem(
        id="todo",
        source_kind="initial_plan",
        origin=f"tool:{todo.source_call_id}",
        content="\n".join(todo_texts),
        eligible=True,
        observed_at=todo.created_at,
    ))
    return evidence


def evaluate_case(
    case: CaseSpec, root: Path, config: EvaluationConfig, cache_dir: Path,
    mode: str = "replay", journal: RuntimeJournal | None = None,
) -> CaseResult:
    _emit(journal, "case.started", case.id, incident_id=case.incident_id, message_id=case.message_id,
          synthetic=case.synthetic, cutoff=case.cutoff, mode=mode, policy_sha256=config.policy_sha256)
    try:
        result = _evaluate_case(case, root, config, cache_dir, mode, journal)
    except RuntimeLogError:
        raise
    except Exception as exc:
        _emit(journal, "case.unexpected_error", case.id, error_type=type(exc).__name__,
              error=str(exc), stack_trace=traceback.format_exc())
        raise
    included = [step for step in result.steps if step.included]
    _emit(journal, "case.finished", case.id, status=result.status, score=result.score, error=result.error,
          included_step_count=len(included),
          contributions=[{"step_id": step.id, "step_score": step.score,
                          "contribution": step.score / len(included) if step.score is not None else None}
                         for step in included],
          rule="Mean of included logical-step contributions; failed todo gate overrides to zero.",
          limitations=result.limitations, data_sha256=result.data_sha256)
    return result
