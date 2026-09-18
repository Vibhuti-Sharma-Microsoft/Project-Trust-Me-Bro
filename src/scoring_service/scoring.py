from __future__ import annotations

import json
import re
from statistics import median

from .config import EvaluationConfig, TrustRule
from .models import ClaimSupport, DocumentEvidence, EvidenceItem, EvidenceRef, GateVote, StepJudgment, StepResult
from .time_utils import timestamp_ns

ROLES = ("gpt", "claude", "gemini")
_JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*"')


def _partial_json_string(value: str) -> str | None:
    if not value.startswith('"'):
        return None
    result: list[str] = []
    index = 1
    escapes = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
    while index < len(value):
        char = value[index]
        if char != "\\":
            result.append(char)
            index += 1
            continue
        if index + 1 >= len(value):
            break
        escaped = value[index + 1]
        if escaped in escapes:
            result.append(escapes[escaped])
            index += 2
        elif escaped == "u" and index + 5 < len(value):
            digits = value[index + 2:index + 6]
            try:
                result.append(chr(int(digits, 16)))
            except ValueError:
                break
            index += 6
        else:
            break
    return "".join(result)


def reference_texts(
    content: str,
    quality_flags: list[str] | None = None,
    query: str = "",
) -> tuple[str, ...]:
    """Return raw evidence plus exact string values from a JSON-encoded payload."""
    texts = [content]
    pending = [(content, 0)]
    while pending:
        encoded, depth = pending.pop()
        for token in _JSON_STRING.findall(encoded):
            try:
                decoded = json.loads(token)
            except (ValueError, RecursionError):
                continue
            if decoded not in texts:
                texts.append(decoded)
        try:
            value = json.loads(encoded)
        except (ValueError, RecursionError):
            if depth < 2 and encoded.startswith('"'):
                try:
                    decoded = json.loads(encoded + '"')
                except (ValueError, RecursionError):
                    decoded = _partial_json_string(encoded)
                if decoded is not None:
                    if decoded not in texts:
                        texts.append(decoded)
                    pending.append((decoded, depth + 1))
            continue
        stack = [value]
        while stack:
            current = stack.pop()
            if isinstance(current, str):
                if current not in texts:
                    texts.append(current)
                if depth < 2 and current.lstrip().startswith(("{", "[", '"')):
                    pending.append((current, depth + 1))
            elif isinstance(current, dict):
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
    if quality_flags:
        texts.append(json.dumps({"quality_flags": quality_flags}))
    if query:
        texts.append(query)
    for text in tuple(texts):
        unescaped = _partial_json_string('"' + text)
        if unescaped and unescaped != text and unescaped not in texts:
            texts.append(unescaped)
        escaped = json.dumps(text, ensure_ascii=False)[1:-1]
        if escaped not in texts:
            texts.append(escaped)
        control_escaped = (
            text.replace("\\", "\\\\")
            .replace("\r", "\\r")
            .replace("\n", "\\n")
            .replace("\t", "\\t")
        )
        if control_escaped not in texts:
            texts.append(control_escaped)
    return tuple(texts)


def invalid_reference_quote(quote: str) -> bool:
    lowered = quote.strip().lower()
    return (
        not lowered
        or "<redacted" in lowered
        or "\\u003credacted" in lowered
        or lowered in {"...", "…", "null"}
    )


def gate_decision(votes: dict[str, GateVote]) -> str:
    if set(votes) != set(ROLES):
        raise ValueError("The todo gate requires all three valid model responses")
    decisions = [vote.decision for vote in votes.values()]
    if decisions.count("PASS") >= 2:
        return "PASS"
    if decisions.count("FAIL") >= 2:
        return "FAIL"
    return "INSUFFICIENT_EVIDENCE"


def validate_references(references: list[EvidenceRef], evidence: list[EvidenceItem]) -> None:
    allowed = {item.id: item for item in evidence}
    for ref in references:
        item = allowed.get(ref.evidence_id)
        if item is None or not item.eligible:
            raise ValueError(f"Judge cited ineligible or unknown evidence: {ref.evidence_id}")
        if not any(
            ref.quote in text
            for text in reference_texts(item.content, item.quality_flags, item.query)
        ):
            raise ValueError(f"Judge quote is absent from evidence: {ref.evidence_id}")
        if invalid_reference_quote(ref.quote):
            raise ValueError("A redaction/placeholder is not a valid evidence quotation")


def coverage(support: list[ClaimSupport]) -> float:
    if not support:
        return 0.0
    if all(item.verdict == "SUPPORTED" for item in support):
        return 1.0
    if any(item.verdict in {"SUPPORTED", "PARTIAL"} for item in support):
        return 0.5
    return 0.0


def faithfulness(judgments: dict[str, StepJudgment]) -> float:
    if set(judgments) != set(ROLES):
        raise ValueError("Faithfulness requires all three valid model responses")
    return float(median(vote.faithfulness for vote in judgments.values()))


def document_freshness(documents: list[DocumentEvidence], cutoff: str, config: EvaluationConfig) -> tuple[float, str]:
    if not documents:
        return 1.0, "NO_DOCUMENT_NEUTRAL_CONVENTION: no referenced documents; not verified freshness"
    scores = []
    reasons = []
    for document in documents:
        if document.status != "AVAILABLE" or not document.content or not document.historical_version_verified:
            scores.append(0.0)
            reasons.append(f"{document.id}: unavailable content or historical version")
            continue
        if not document.last_updated:
            scores.append(0.0)
            reasons.append(f"{document.id}: update timestamp missing")
            continue
        try:
            age_ns = timestamp_ns(cutoff) - timestamp_ns(document.last_updated)
        except ValueError:
            scores.append(0.0)
            reasons.append(f"{document.id}: update timestamp invalid")
            continue
        day_ns = 86_400 * 1_000_000_000
        if age_ns < 0:
            score = 0.0
            reason = "version after response cutoff"
        elif age_ns <= config.fresh_days * day_ns:
            score = 1.0
            reason = f"age within {config.fresh_days} days"
        elif age_ns <= config.partial_days * day_ns:
            score = 0.5
            reason = f"age within {config.partial_days} days"
        else:
            score = 0.0
            reason = f"age exceeds {config.partial_days} days"
        scores.append(score)
        reasons.append(f"{document.id}: {reason}")
    return min(scores), "; ".join(reasons)


def rule_matches(rule: TrustRule, item: EvidenceItem) -> bool:
    if rule.source_kind != item.source_kind:
        return False
    prefix = rule.origin_prefix
    if prefix.endswith(":"):
        return item.origin.startswith(prefix)
    return item.origin == prefix.rstrip("/") or item.origin.startswith(prefix.rstrip("/") + "/")


def validate_trust(judgment: StepJudgment, evidence: list[EvidenceItem], config: EvaluationConfig) -> None:
    if judgment.source_trust is None:
        raise ValueError("The GPT step judgment must include source_trust")
    rules = {rule.id: rule for rule in config.trust_rules}
    if any(rule_id not in rules for rule_id in judgment.trust_policy_ids):
        raise ValueError("Source-trust judgment cites an unknown policy")
    refs = judgment.references + [ref for support in judgment.claim_support for ref in support.references]
    used_ids = {ref.evidence_id for ref in refs}
    used = [item for item in evidence if item.id in used_ids]
    maxima = [
        max((rules[rule_id].maximum_score for rule_id in judgment.trust_policy_ids if rule_matches(rules[rule_id], item)), default=0.0)
        for item in used
    ]
    ceiling = min(maxima, default=0.0)
    if judgment.source_trust > ceiling:
        raise ValueError("Source trust exceeds the reviewed policy ceiling for the cited evidence")


def step_score(f: float, c: float, t: float, p: float, config: EvaluationConfig) -> float:
    values = {"faithfulness": f, "coverage": c, "source_trust": t, "freshness": p}
    if any(value not in {0, 0.5, 1} for value in values.values()):
        raise ValueError("Dimension score is outside the tri-valued contract")
    return sum(config.weights[key] * int(value * 2) for key, value in values.items()) / 2


def response_score(steps: list[StepResult]) -> float | None:
    included = [step for step in steps if step.included]
    if not included:
        return None
    if any(step.score is None for step in included):
        raise ValueError("An included scoring step has no contribution")
    return sum(step.score or 0.0 for step in included) / len(included)
