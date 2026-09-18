from __future__ import annotations

import http.client
import inspect
import math
import os
import queue
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest
from markupsafe import Markup

from scoring_service import report
from scoring_service.config import EvaluationConfig
from scoring_service.models import (
    BatchResult, CaseResult, Claim, ClaimSupport, DocumentEvidence, EvidenceItem,
    EvidenceRef, GateVote, JudgeRecord, StepResult, TodoPlan, TodoStep, ToolCall,
)


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(child.text if isinstance(child, _Node) else child for child in self.children)

    def find(self, tag: str, **attrs: str) -> _Node:
        return next(node for node in self.walk() if node.tag == tag and all(node.attrs.get(k) == v for k, v in attrs.items()))

    def walk(self) -> Iterator[_Node]:
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()


class _Page(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]
        self.feed(html)
        self.close()
        assert len(self.stack) == 1, "Unclosed HTML elements"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {"meta", "link", "br", "hr", "img", "input"}:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        assert self.stack[-1].tag == tag, f"Unbalanced HTML: {tag}"
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)

    def find(self, tag: str, **attrs: str) -> _Node:
        return self.root.find(tag, **attrs)

    @property
    def nodes(self) -> list[_Node]:
        return list(self.root.walk())

    @property
    def text(self) -> str:
        return self.root.text


def _scored_step(
    step_id: str = "step-1",
    values: tuple[float, float, float, float] = (1, 1, 0.5, 1),
    weights: dict[str, int] | None = None,
    **changes: Any,
) -> StepResult:
    weights = weights if weights is not None else EvaluationConfig().weights
    keys = ("faithfulness", "coverage", "source_trust", "freshness")
    fields: dict[str, Any] = {
        "id": step_id, "title": "Check evidence", "disposition": "EVALUATE", "included": True,
        "score": sum(weights[key] * value for key, value in zip(keys, values)),
        **dict(zip(keys, values)),
    }
    fields.update(changes)
    return StepResult(**fields)


def _case(**changes: Any) -> CaseResult:
    fields: dict[str, Any] = {
        "case_id": "case-1", "incident_id": "incident-123", "message_id": "message-456",
        "synthetic": False, "status": "SCORED", "score": 90.0, "cutoff": "2026-09-17T09:00:00Z",
        "steps": [_scored_step()],
        "response_text": "The first diagnostic response.", "data_sha256": "case-data-hash",
        "policy_sha256": "case-policy-hash",
    }
    fields.update(changes)
    return CaseResult(**fields)


def _batch(*cases: CaseResult, **changes: Any) -> BatchResult:
    fields: dict[str, Any] = {
        "run_id": "run-local", "created_at": "2026-09-17T10:00:00Z",
        "policy_version": "custom-policy", "policy_sha256": "batch-policy-hash",
        "target_real_cases": 10, "selected_real_cases": 999, "results": list(cases),
    }
    fields.update(changes)
    return BatchResult(**fields)


def _render(tmp_path: Path, *cases: CaseResult, **changes: Any) -> tuple[Path, _Page]:
    path = report.render_report(_batch(*cases, **changes), tmp_path / "reports")
    return path, _Page(path.read_text(encoding="utf-8"))


def _raw_case(sentinel: str) -> CaseResult:
    return _case(
        response_text=sentinel, error=sentinel, limitations=[sentinel], data_sha256=sentinel, policy_sha256=sentinel,
        todo=TodoPlan(source_call_id=sentinel, created_at=sentinel, raw=sentinel,
                      steps=[TodoStep(id=sentinel, title=sentinel, condition=sentinel)]),
        claims=[Claim(id=sentinel, quote=sentinel, step_id="step-1", claim_type="observation")],
        calls=[ToolCall(id=sentinel, name=sentinel, thread_id=sentinel, trace_id=sentinel,
                        input={"context": sentinel}, input_raw=sentinel, output_raw=sentinel)],
        evidence=[EvidenceItem(id=sentinel, source_kind=sentinel, origin=sentinel, query=sentinel,
                               content=sentinel, source_record_id=sentinel)],
        documents=[DocumentEvidence(id=sentinel, url=sentinel, step_id="step-1", status="AVAILABLE",
                                    content=sentinel, content_sha256=sentinel, version=sentinel, metadata_provenance=sentinel)],
        judges=[JudgeRecord(role="gpt", stage="step", step_id="step-1", model=sentinel, request_sha256=sentinel,
                            prompt_sha256=sentinel, mode="replay",
                            output={"rationale": "Observed signals support the response.", "full_context": sentinel})],
    )


def test_public_api_and_only_local_ui_assets(tmp_path: Path) -> None:
    path, page = _render(tmp_path, _case())
    assert path == tmp_path / "reports" / "index.html"
    assert {item.name for item in path.parent.iterdir()} == {"index.html", "report.css", "report.js"}
    css = path.with_name("report.css").read_text(encoding="utf-8")
    js = path.with_name("report.js").read_text(encoding="utf-8")
    assert "dialog" in css and "@media" in css and "focus-visible" in css
    assert "url(" not in css and "@import" not in css
    assert "innerHTML" not in js and "eval(" not in js and "fetch(" not in js
    assert "textContent" in js and "cloneNode" in js
    assert page.find("link", rel="stylesheet").attrs["href"] == "report.css"
    scripts = [node for node in page.nodes if node.tag == "script"]
    assert len(scripts) == 1 and scripts[0].attrs == {"src": "report.js", "defer": None}
    assert not scripts[0].text
    csp = page.find("meta", **{"http-equiv": "Content-Security-Policy"}).attrs["content"]
    assert csp and "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert "Uncalibrated evidence index" in page.text
    assert page.find("label", **{"for": "incident-select"}).text == "Select incident"
    card = page.find("div", id="scorecard-host")
    assert card.find("strong", **{"data-score": "total"}).text == "90.00"
    assert len([node for node in card.walk() if node.tag == "button"]) == 4
    assert card.find("h3", **{"class": "dimensions-heading"}).text == "Dimension contributions"
    assert not any("data-consistency" in node.attrs for node in card.walk())
    assert not any(node.attrs.get("class") == "status-note" for node in card.walk())
    assert "before display rounding" not in page.text
    assert "not an averaged tri-score" not in page.text
    dialog = page.find("dialog", id="dimension-dialog")
    assert dialog.attrs["aria-labelledby"] == "dimension-title"
    assert dialog.attrs["aria-describedby"] == "dimension-definition"
    assert page.find("button", id="close-dimension").attrs["aria-label"] == "Close dimension details"
    assert not list(path.parent.glob(".*"))
    signature = inspect.signature(report.serve_reports)
    assert tuple(signature.parameters) == ("directory", "host", "port")
    assert signature.parameters["host"].default == "127.0.0.1"
    assert signature.parameters["port"].default == 8080
    assert tuple(inspect.signature(report.render_report).parameters) == ("batch", "output_dir")


def test_only_first_scorecard_is_outside_inert_templates(tmp_path: Path) -> None:
    _, page = _render(tmp_path, _case(), _case(case_id="second", incident_id="other", synthetic=True))
    host = page.find("div", id="scorecard-host")
    assert len([child for child in host.children if isinstance(child, _Node) and child.tag == "article"]) == 1
    assert host.find("h2", id="incident-title").text == "Incident incident-123"
    selector = page.find("select", id="incident-select")
    options = [child for child in selector.children if isinstance(child, _Node)]
    assert [option.attrs["value"] for option in options] == ["case-0", "case-1"]
    assert "[Synthetic]" in options[1].text
    assert page.find("template", **{"data-case-template": "case-1"}).find("h2").text == "Incident other"


@pytest.mark.parametrize(
    ("weights", "expected"),
    [
        ({"faithfulness": 35, "coverage": 35, "source_trust": 20, "freshness": 10}, [14, 10.5, 6, 5]),
        ({"freshness": 5, "source_trust": 15, "coverage": 40, "faithfulness": 40}, [16, 12, 4.5, 2.5]),
    ],
    ids=["default-policy", "alternate-policy"],
)
def test_weighted_points_retain_structural_denominator(
    tmp_path: Path, weights: dict[str, int], expected: list[float],
) -> None:
    config = EvaluationConfig(weights=weights)
    steps = [
        _scored_step("a", (1, 0.5, 0.5, 1), weights),
        _scored_step("b", (0.5, 1, 1, 0.5), weights),
        _scored_step("c", (0.5, 0, 0, 1), weights),
        StepResult(id="missing", title="Missing work", disposition="MISSING_REQUIRED", included=True, score=0),
        StepResult(id="unassigned", title="Unassigned", disposition="UNASSIGNED", included=True, score=0),
        _scored_step("excluded", (1, 1, 1, 1), weights, included=False, disposition="HOUSEKEEPING"),
    ]
    case = _case(steps=steps, score=sum(expected))
    before = case.model_dump_json()
    view = report._case_view(case, config.weights)
    assert [dimension["points"] for dimension in view["dimensions"]] == expected
    assert view["consistency_state"] == "match"
    assert all(dimension["included_count"] == 5 and dimension["structural_count"] == 2 for dimension in view["dimensions"])
    assert all(dimension["missing_count"] == 0 for dimension in view["dimensions"])
    _, page = _render(tmp_path, case, weights=config.weights)
    card = page.find("div", id="scorecard-host")
    for dimension, points in zip(view["dimensions"], expected):
        key = dimension["key"]
        tile = card.find("button", **{"data-dimension": key})
        assert tile.find("strong").text == f"{points:.2f} pt"
        assert f"Weight {weights[key]}%" in tile.text
        detail = card.find("template", **{"data-dimension-detail": key})
        assert "/ 5 included steps" in detail.text
        assert "3 contributing steps; 1 excluded step." in detail.text
        assert "2 required/unassigned steps add no points but remain included." in detail.text
    assert case.model_dump_json() == before
    assert "Mismatch" not in card.text


def test_zero_is_judged_zero_but_missing_scores_are_not(tmp_path: Path) -> None:
    zero = _case(steps=[_scored_step(values=(0, 0, 0, 0))], score=0)
    _, page = _render(tmp_path, zero)
    host = page.find("div", id="scorecard-host")
    assert host.find("strong", **{"data-score": "total"}).text == "0.00"
    assert "1 contributing step." in host.text
    assert "0 at 0.5" not in host.text and "0 at 1" not in host.text
    assert host.find("strong", **{"data-contribution": "faithfulness"}).text == "0.00 pt"
    for step in (
        _scored_step(score=None),
        _scored_step(faithfulness=None),
        StepResult(id="missing", title="Missing", disposition="MISSING_REQUIRED", included=True, score=None),
    ):
        view = report._case_view(_case(steps=[step]), EvaluationConfig().weights)
        assert view["dimensions"][0]["points"] is None
        assert view["dimensions"][0]["label"] == "Unavailable"
        assert "Missing scores are not zero" in view["dimensions"][0]["calculation"]
        assert view["consistency_state"] == "unavailable"


def test_consistency_uses_unrounded_points_and_never_changes_scores(tmp_path: Path) -> None:
    steps = [
        _scored_step(values=(1, 1, 1, 1)),
        StepResult(id="m", title="Missing", included=True, disposition="MISSING_REQUIRED", score=0),
        StepResult(id="u", title="Unassigned", included=True, disposition="UNASSIGNED", score=0),
    ]
    case = _case(steps=steps, score=100 / 3)
    view = report._case_view(case, EvaluationConfig().weights)
    assert math.isclose(sum(dimension["points"] for dimension in view["dimensions"]), case.score)
    assert view["consistency_state"] == "match" and "before display rounding" in view["consistency"]
    _, page = _render(tmp_path, _case(score=12.34))
    card = page.find("div", id="scorecard-host")
    assert card.find("strong", **{"data-score": "total"}).text == "12.34"
    warning = card.find("p", **{"data-consistency": "mismatch"}).text
    assert "Review required" in warning and "recorded incident score has not been changed" in warning
    assert all(node.text == "Unavailable" for node in card.walk() if "data-contribution" in node.attrs)


@pytest.mark.parametrize(
    ("case", "state"),
    [
        (_case(score=12.34), "mismatch"),
        (_case(steps=[_scored_step(score=50)]), "mismatch"),
        (_case(steps=[_scored_step("a", score=80), _scored_step("b", score=100)]), "mismatch"),
        (_case(score=80, steps=[_scored_step(score=80)]), "mismatch"),
        (_case(score=10, steps=[StepResult(id="m", title="Missing", disposition="MISSING_REQUIRED", included=True, score=10)]), "mismatch"),
        (_case(steps=[_scored_step(coverage=None)]), "unavailable"),
    ],
    ids=["total-mismatch", "step-total-mismatch", "cancelling-step-mismatches", "dimension-step-mismatch", "nonzero-structural", "incomplete-dimensions"],
)
def test_archived_breakdowns_withhold_all_unverified_contributions(tmp_path: Path, case: CaseResult, state: str) -> None:
    original = case.model_dump_json()
    view = report._case_view(case, EvaluationConfig().weights)
    assert view["consistency_state"] == state
    assert view["score"] == case.score
    for dimension in view["dimensions"]:
        assert dimension["points"] is None and dimension["label"] == "Unavailable"
        assert "Review required" in dimension["calculation"]
        assert " x " not in dimension["calculation"] and " = " not in dimension["calculation"]
    path, page = _render(tmp_path, case)
    assert all(node.text == "Unavailable" for node in page.nodes if "data-contribution" in node.attrs)
    assert "Recorded step notes for review" in page.text
    assert "verified incident contribution breakdown" in page.text
    assert "Final arithmetic unavailable" in page.text
    assert all(dimension["points"] is None for dimension in view["dimensions"])
    assert case.model_dump_json() == original


@pytest.mark.parametrize("status", ["UNSCORABLE", "JUDGE_ERROR", "IMPORT_ERROR", "NOT_APPLICABLE"])
@pytest.mark.parametrize("score", [None, 0.0, 100.0])
def test_non_scored_outcomes_are_unavailable_not_zero(tmp_path: Path, status: str, score: float | None) -> None:
    _, page = _render(tmp_path, _case(status=status, score=score))
    host = page.find("div", id="scorecard-host")
    assert host.find("strong", **{"data-score": "total"}).text == "Unavailable"
    assert status in host.text
    assert all(node.text == "Not computed" for node in host.walk() if "data-contribution" in node.attrs)
    assert "Final dimension contributions were not computed" in host.text
    assert "Contribution sum unavailable" in host.text


def test_gate_failure_zero_is_not_dimension_evaluation(tmp_path: Path) -> None:
    case = _case(status="GATE_FAILED", score=0, steps=[], gate_votes={
        "gpt": GateVote(decision="FAIL", rationale="The initial plan omits the required investigation."),
    })
    _, page = _render(tmp_path, case)
    host = page.find("div", id="scorecard-host")
    assert host.find("strong", **{"data-score": "total"}).text == "0.00"
    assert all(node.text == "Not computed" for node in host.walk() if "data-contribution" in node.attrs)
    assert "The initial plan omits" in host.text
    assert host.find("p", **{"data-consistency": "not-evaluated"}).text == "Gate outcome only; no dimension sum."
    case.score = None
    assert report._case_view(case, EvaluationConfig().weights)["score"] is None


def test_brief_dimension_specific_reasons_and_neutral_freshness(tmp_path: Path) -> None:
    step = _scored_step(
        freshness_reason="NO_DOCUMENT_NEUTRAL_CONVENTION: no referenced documents; not verified freshness",
        votes={"gpt": 1, "claude": 0.5, "gemini": 1},
        support=[ClaimSupport(claim_id="c", verdict="PARTIAL", rationale="Only part of the material claim is supported.")],
    )
    judges = [
        JudgeRecord(role="gpt", stage="step", step_id="step-1", model="model", request_sha256="hash",
                    prompt_sha256="hash", mode="replay",
                    output={"rationale": "The observations match the tool evidence.",
                            "trust_rationale": "The approved production telemetry rule limits source trust."}),
        JudgeRecord(role="claude", stage="step", step_id="excluded", model="model", request_sha256="hash",
                    prompt_sha256="hash", mode="replay", output={"rationale": "EXCLUDED_REASON_SENTINEL"}),
    ]
    _, page = _render(tmp_path, _case(steps=[step], judges=judges))
    host = page.find("div", id="scorecard-host")
    for key, expected in {
        "faithfulness": "The observations match the tool evidence.",
        "coverage": "Only part of the material claim is supported.",
        "source_trust": "The approved production telemetry rule limits source trust.",
        "freshness": "neutral no-document convention, not verified freshness",
    }.items():
        assert expected in host.find("template", **{"data-dimension-detail": key}).text
    assert "Model judgments differed; each step uses the median vote." in host.text
    assert "EXCLUDED_REASON_SENTINEL" in page.text
    assert "EXCLUDED_REASON_SENTINEL" not in host.find("template", **{"data-dimension-detail": "faithfulness"}).text
    assert "not verified freshness" in host.find("button", **{"data-dimension": "freshness"}).text
    doc = DocumentEvidence(id="d", url="https://example.test", step_id="step-1", status="AVAILABLE")
    view = report._case_view(_case(steps=[step], documents=[doc]), EvaluationConfig().weights)
    assert view["dimensions"][3]["neutral_count"] == 0


def test_single_report_contains_complete_payloads_as_inert_text(tmp_path: Path) -> None:
    sentinel = "SENSITIVE_RAW_SENTINEL " * 1000
    path, page = _render(tmp_path, _raw_case(sentinel), run_id=sentinel, policy_sha256=sentinel, policy_version=sentinel)
    html = path.read_text(encoding="utf-8")
    assert sentinel in html
    assert page.find("pre", **{"class": "evaluated-response"}).text == sentinel
    assert not any(node.tag in {"textarea", "iframe", "img", "object", "embed"} for node in page.nodes)
    assert "application/json" not in html and "full_context" in html
    for section in ("Incident metadata", "Exact SRE agent response", "LLM-as-judge evaluations",
                    "Tool call inventory", "Source and Kusto evidence", "Score analysis and formula"):
        assert page.find("section", **{"aria-label": section})
    assert "Observed signals support the response." in page.text


def test_replay_rich_report_survives_persistence_with_all_details(corpus, tmp_path: Path) -> None:
    from scoring_service.executor import evaluate_case

    root, manifest, config = corpus
    results = [evaluate_case(spec, root, config, tmp_path / "cache") for spec in manifest.cases]
    batch = _batch(*results, weights=config.weights)
    restored = BatchResult.model_validate_json(batch.model_dump_json())
    path = report.render_report(restored, tmp_path / "report")
    page = _Page(path.read_text(encoding="utf-8"))
    host = page.find("div", id="scorecard-host")
    assert host.find("pre", **{"class": "evaluated-response"}).text == results[0].response_text
    assert host.find("pre", **{"class": "original-response"}).text == results[0].response_raw
    assert host.find("p", **{"data-aggregation": "response"}).text == "(100) / 1 = 100.00 / 100."
    for value in (manifest.cases[0].thread_id, manifest.cases[0].post_call_id,
                  manifest.cases[0].response_sha256, manifest.cases[0].selection_reason):
        assert value in host.text
    assert "ServiceState | project region" in host.text
    assert '"region":"eastus"' in host.text
    assert "Synthetic one-step binding" in host.text
    assert len([node for node in host.walk() if node.attrs.get("class") == "judge-output"]) == 7
    assert len([node for node in host.walk() if node.attrs.get("class") == "evaluation-input"]) == 3
    for judge in results[0].judges:
        assert judge.model in host.text and judge.request_sha256 in host.text and judge.prompt_sha256 in host.text
        if judge.output.get("rationale"):
            assert judge.output["rationale"] in host.text
    partial = page.find("template", **{"data-case-template": "case-1"})
    assert "(65) / 1 = 65.00 / 100." in partial.text
    assert "PARTIAL" in partial.text
    contradicted = page.find("template", **{"data-case-template": "case-2"})
    assert "CONTRADICTED" in contradicted.text
    gate_failed = page.find("template", **{"data-case-template": "case-3"})
    assert "NOT_EVALUATED" in gate_failed.text and "NOT_INVOKED" in gate_failed.text
    assert not any(node.attrs.get("class") == "evidence-query" and node.text == "fabricated" for node in page.nodes)
    assert "age exceeds 1095 days" in page.find("template", **{"data-case-template": "case-5"}).text
    missing = page.find("template", **{"data-case-template": "case-8"})
    assert "Structural zero" in missing.text and "(0) / 1 = 0.00 / 100." in missing.text
    error = page.find("template", **{"data-case-template": "case-9"})
    assert "FAILED" in error.text and "No valid response recorded" in error.text
    assert "gemini" in error.text
    assert {item.name for item in path.parent.iterdir()} == {"index.html", "report.css", "report.js"}


def test_new_raw_surfaces_escape_hostile_content_and_preserve_whitespace(tmp_path: Path) -> None:
    from scoring_service.models import EvaluationInput

    attack = '</pre></details></template><img src="https://evil.test" onerror="alert(1)"><script>alert(1)</script>'
    raw = "\n  Exact response\r\n\r\n" + attack + "\r  Last line  "
    case = _raw_case(raw)
    case.response_raw = raw
    case.context = raw
    case.evaluation_inputs = [EvaluationInput(stage="step", step_id="step-1", payload={"evidence": [{"id": "e", "content": raw}]})]
    path, page = _render(tmp_path, case)
    assert page.find("pre", **{"class": "evaluated-response"}).text == raw
    assert page.find("pre", **{"class": "original-response"}).text == raw
    assert raw in page.text
    assert attack not in path.read_text(encoding="utf-8")
    assert not any(node.tag in {"img", "iframe", "svg"} for node in page.nodes)
    assert len([node for node in page.nodes if node.tag == "script"]) == 1


def test_reason_previews_are_bounded_but_full_audit_is_not_truncated(tmp_path: Path) -> None:
    judges = [
        JudgeRecord(role="gpt", stage="step", step_id="step-1", model="m", request_sha256="h",
                    prompt_sha256="h", mode="replay",
                    output={"rationale": f"Brief reason {i}. " + "x" * 400 + "DO_NOT_EMBED_TAIL"})
        for i in range(20)
    ]
    path, _ = _render(tmp_path, _case(judges=judges))
    html = path.read_text()
    assert "DO_NOT_EMBED_TAIL" in html and "Brief reason 19." in html
    assert "Brief reason 0." in html and "..." in html
    reasons = report._case_view(_case(judges=judges), EvaluationConfig().weights)["dimensions"][0]["reasons"]
    assert len(reasons) == 2 and all(len(reason) <= 180 for reason in reasons)


def test_identical_model_explanations_are_combined_without_zero_count_prose(tmp_path: Path) -> None:
    shared = "The observations support the diagnosis."
    second = "An independent result corroborates the finding."
    texts = [shared, f"  {shared}\n", shared, second, "THIRD_EXPLANATION_SENTINEL"]
    roles = ["gpt", "claude", "gemini", "gpt", "claude"]
    judges = [
        JudgeRecord(role=role, stage="step", step_id="step-1", model="model", request_sha256="hash",
                    prompt_sha256="hash", mode="replay", output={"rationale": text})
        for role, text in zip(roles, texts)
    ]
    case = _case(judges=judges, steps=[_scored_step(votes={"gpt": 1, "claude": 1, "gemini": 1})])
    original = case.model_dump_json()
    view = report._case_view(case, EvaluationConfig().weights)
    assert view["dimensions"][0]["reasons"] == [shared, second]
    path, page = _render(tmp_path, case)
    detail = page.find("div", id="scorecard-host").find("template", **{"data-dimension-detail": "faithfulness"})
    assert detail.text.count(shared) == 1 and detail.text.count(second) == 1
    assert detail.find("p", **{"class": "distribution"}).text == "1 contributing step."
    assert "Each step uses the median model faithfulness vote." in detail.text
    for unwanted in ("0 at", "0 unavailable", "0 excluded", "0 structural", "disagree on 0", "Model judgments differed"):
        assert unwanted not in detail.text
    assert "THIRD_EXPLANATION_SENTINEL" in path.read_text(encoding="utf-8")
    assert "THIRD_EXPLANATION_SENTINEL" not in detail.text
    assert case.model_dump_json() == original


def test_untrusted_markup_and_html_marked_strings_are_escaped(tmp_path: Path) -> None:
    attack = '<img src=x onerror="alert(1)">'
    step = _scored_step(freshness_reason=attack, support=[
        ClaimSupport(claim_id="c", verdict="PARTIAL", rationale=attack, references=[EvidenceRef(evidence_id="e", quote=attack)]),
    ])
    judge = JudgeRecord(role="gpt", stage="step", step_id="step-1", model="m", request_sha256="h",
                        prompt_sha256="h", mode="replay", output={})
    judge.output["rationale"] = Markup(attack)
    judge.output["trust_rationale"] = Markup(attack)
    path, page = _render(tmp_path, _case(case_id=attack, incident_id=attack, message_id=attack, steps=[step], judges=[judge]))
    html = path.read_text(encoding="utf-8")
    assert attack not in html and "&lt;img" in html and attack in page.text
    for node in page.nodes:
        assert node.tag not in {"img", "svg", "iframe", "object", "embed"}
        assert not any(key.lower().startswith("on") for key in node.attrs)
    assert len([node for node in page.nodes if node.tag == "script"]) == 1
    assert "|safe" not in (Path(report.__file__).parent / "templates" / "report.html.j2").read_text()


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", "file:///C:/private.txt", "data:text/html,<script>alert(1)</script>",
    "vbscript:alert(1)", "//evil.example/path", " https://example.test", "https:\n//example.test",
    "https://example.test\\@evil.test", "https://example.test:bad/path", "https://", "https://[bad-ip]/",
    "relative/path", "\x00https://example.test", "https://example.test/a?x=1&y=2#section",
    "HTTP://example.test/path", 'https://example.test/?q="<svg>',
])
def test_source_urls_are_preserved_as_text_never_active_links(tmp_path: Path, url: str) -> None:
    path, page = _render(tmp_path, _case(
        evidence=[EvidenceItem(id="e", source_kind="source", origin=url, content="Text")],
        documents=[DocumentEvidence(id="d", url=url, step_id="step-1", status="MISSING")],
    ))
    assert url in page.text
    assert not any(node.tag == "a" for node in page.nodes)
    assert not any(node.tag in {"svg", "iframe", "img"} for node in page.nodes)


@pytest.mark.parametrize("selected", [0, 1, 10, 12])
def test_one_small_real_corpus_line(tmp_path: Path, selected: int) -> None:
    _, page = _render(tmp_path, _case(), _case(synthetic=True), selected_real_cases=selected)
    note = page.find("p", id="corpus-note").text
    assert note == "Includes synthetic data. 1/10 real cases."
    assert "Batch summary" not in page.text


@pytest.mark.parametrize(
    ("synthetic", "expected"),
    [(True, "Synthetic data. 0/10 real cases."), (False, "1/10 real cases.")],
)
def test_synthetic_banner_uses_one_clear_real_case_count(tmp_path: Path, synthetic: bool, expected: str) -> None:
    _, page = _render(tmp_path, _case(synthetic=synthetic), selected_real_cases=0 if synthetic else 1)
    assert page.find("p", id="corpus-note").text == expected


def test_empty_batch_and_cli_owned_log_guidance(tmp_path: Path) -> None:
    output = tmp_path / "reports"
    output.mkdir()
    for name in ("results.json", "scoring-service.log"):
        (output / name).write_bytes(b"PRIVATE_UNCHANGED")
    path, page = _render(tmp_path, runtime_log_file=r"C:\private\context\scoring-service.log")
    assert "No incident results available" in page.text
    assert not any(node.tag == "select" for node in page.nodes)
    assert "scoring-service.log" in page.text
    assert "private" not in path.read_text() and "PRIVATE_UNCHANGED" not in path.read_text()
    assert not any(node.tag == "a" for node in page.nodes)
    for name in ("results.json", "scoring-service.log"):
        assert (output / name).read_bytes() == b"PRIVATE_UNCHANGED"
    assert {item.name for item in output.iterdir()} == {"index.html", "report.css", "report.js", "results.json", "scoring-service.log"}


@pytest.mark.parametrize("log_file", [None, "scoring-service.log"], ids=["legacy-no-log-field", "new-log-pointer"])
def test_render_never_fabricates_runtime_logs(tmp_path: Path, log_file: str | None) -> None:
    payload = _batch(_case()).model_dump(mode="json", exclude={"runtime_log_file"})
    if log_file is not None:
        payload["runtime_log_file"] = log_file
    batch = BatchResult.model_validate(payload)
    assert batch.runtime_log_file == log_file
    path = report.render_report(batch, tmp_path / "reports")
    assert {item.name for item in path.parent.iterdir()} == {"index.html", "report.css", "report.js"}
    assert not path.with_name("scoring-service.log").exists()
    assert not path.with_name("results.json").exists()
    html = path.read_text(encoding="utf-8")
    assert ("and <code>scoring-service.log</code>" in html) == (log_file is not None)
    assert "This is the single evaluation report." in html


@contextmanager
def _running_server(directory: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, int]]:
    ready: queue.Queue[ThreadingHTTPServer] = queue.Queue()
    failures: list[BaseException] = []

    class ObservedServer(ThreadingHTTPServer):
        def serve_forever(self, poll_interval: float = 0.01) -> None:
            ready.put(self)
            super().serve_forever(poll_interval=poll_interval)

    monkeypatch.setattr(report, "ThreadingHTTPServer", ObservedServer)

    def run() -> None:
        try:
            report.serve_reports(directory, host="localhost", port=0)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run, name="report-test-server")
    thread.start()
    server: ThreadingHTTPServer | None = None
    try:
        server = ready.get(timeout=5)
        assert server.server_address[0] == "127.0.0.1"
        yield "127.0.0.1", server.server_port
    finally:
        if server is not None:
            server.shutdown()
        thread.join(timeout=5)
        assert not thread.is_alive(), "Report server thread leaked"
        assert not failures, failures
        if server is not None:
            assert server.fileno() == -1, "Report server listening socket leaked"


def _request(address: tuple[str, int], path: str, method: str = "GET", headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(*address, timeout=3)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_server_only_exposes_report_assets_and_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index, _ = _render(tmp_path, _case(incident_id="visible report"))
    (tmp_path / "parent-secret.txt").write_text("PRIVATE PARENT DATA")
    for name in ("config.json", "results.json", "scoring-service.log"):
        (index.parent / name).write_text("PRIVATE DATA")
    for name in ("cache", "corpus", "nested"):
        (index.parent / name).mkdir()
        (index.parent / name / "index.html").write_text("PRIVATE NESTED DATA")
    with _running_server(index.parent, monkeypatch) as address:
        for path in ("/", "/index.html", "/%69ndex.html", "/index.html?view=1"):
            status, headers, body = _request(address, path)
            assert status == 200 and b"visible report" in body
            assert headers["Content-Type"] == "text/html; charset=utf-8"
            assert headers["Cache-Control"] == "no-store"
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert headers["Referrer-Policy"] == "no-referrer"
            assert "default-src 'none'" in headers["Content-Security-Policy"]
            assert "script-src 'self'" in headers["Content-Security-Policy"]
            assert "unsafe-inline" not in headers["Content-Security-Policy"]
        status, headers, body = _request(address, "/report.css")
        assert status == 200 and b"system-ui" in body
        assert headers["Content-Type"] == "text/css; charset=utf-8"
        status, headers, body = _request(address, "/report.js")
        assert status == 200 and b"showModal" in body
        assert headers["Content-Type"] == "text/javascript; charset=utf-8"
        assert _request(address, "/", method="HEAD")[2] == b""
        for path in (
            "/../parent-secret.txt", "/%2e%2e/parent-secret.txt", "/..%2fparent-secret.txt",
            "/%252e%252e/parent-secret.txt", "/..\\parent-secret.txt", "/%2e%2e%5cparent-secret.txt",
            "/config.json", "/results.json", "/scoring-service.log", "/cache/", "/corpus/", "/nested/index.html",
            "/./index.html", "/report.css/../config.json", "/report.js/../results.json", "/index.html/extra",
            "/index.html%00", "/%ff", "/C:/private.txt", "http://evil.example/index.html",
        ):
            status, _, body = _request(address, path, headers={"Host": f"{address[0]}:{address[1]}"})
            assert status == 404, path
            assert b"PRIVATE" not in body and b"Directory listing" not in body
        for host in ("evil.example", "localhost@evil.example", "localhost:1", "0.0.0.0"):
            assert _request(address, "/", headers={"Host": host})[0] == 403
        connection = http.client.HTTPConnection(*address, timeout=3)
        try:
            connection.putrequest("GET", "/", skip_host=True)
            connection.putheader("Host", f"{address[0]}:{address[1]}")
            connection.putheader("Host", "evil.example")
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == 403
            response.read()
        finally:
            connection.close()


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.1", "8.8.8.8", "example.test", "localhost.evil.test", "127.1", "", "::1%eth0", "::ffff:127.0.0.1"])
def test_non_loopback_hosts_rejected_before_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    def no_bind(*args: Any, **kwargs: Any) -> None:
        pytest.fail("An invalid host must not create a server")

    monkeypatch.setattr(report, "ThreadingHTTPServer", no_bind)
    monkeypatch.setattr(report, "_IPv6ReportServer", no_bind)
    with pytest.raises(ValueError, match="loopback"):
        report.serve_reports(tmp_path, host=host)


@pytest.mark.parametrize("port", [-1, 65536, True, "8080"])
def test_invalid_ports_rejected(tmp_path: Path, port: Any) -> None:
    with pytest.raises(ValueError, match="port"):
        report.serve_reports(tmp_path, port=port)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "::1"])
def test_loopback_binding_and_keyboard_interrupt_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    index, _ = _render(tmp_path, _case())
    events: list[Any] = []

    class InterruptServer:
        def __init__(self, address: tuple[str, int], handler: Any) -> None:
            events.append(address)

        def __enter__(self) -> InterruptServer:
            return self

        def __exit__(self, *args: Any) -> None:
            events.append("closed")

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

    assert report._IPv6ReportServer.address_family == socket.AF_INET6
    monkeypatch.setattr(report, "_IPv6ReportServer" if ":" in host else "ThreadingHTTPServer", InterruptServer)
    assert report.serve_reports(index.parent, host=host) is None
    assert events == [("127.0.0.1" if host == "localhost" else host, 8080), "closed"]


def test_unrendered_directories_and_directory_assets_rejected(tmp_path: Path) -> None:
    for path in (tmp_path / "missing", tmp_path):
        with pytest.raises(ValueError, match="Report directory"):
            report.serve_reports(path, port=0)
    (tmp_path / "index.html").mkdir()
    (tmp_path / "report.css").write_text("css")
    with pytest.raises(ValueError, match="ordinary"):
        report.serve_reports(tmp_path, port=0)
    with pytest.raises(ValueError, match="ordinary"):
        report.render_report(_batch(), tmp_path)


def test_missing_javascript_rejected_before_serving(tmp_path: Path) -> None:
    path, _ = _render(tmp_path, _case())
    path.with_name("report.js").unlink()
    with pytest.raises(ValueError, match="report.js"):
        report.serve_reports(path.parent, port=0)


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"Symlink creation is not available: {error}")


def test_symlink_roots_and_ancestors_rejected(tmp_path: Path) -> None:
    index, _ = _render(tmp_path, _case())
    linked = tmp_path / "linked-report"
    _symlink(linked, index.parent)
    for directory in (linked, linked / "new-child"):
        with pytest.raises(ValueError, match="symlinks"):
            report.render_report(_batch(), directory)
        with pytest.raises(ValueError, match="symlinks"):
            report.serve_reports(directory, port=0)
    assert not (index.parent / "new-child").exists()


@pytest.mark.parametrize("name", ["index.html", "report.css", "report.js"])
def test_linked_assets_never_read_or_overwritten(tmp_path: Path, name: str) -> None:
    index, _ = _render(tmp_path, _case())
    outside = tmp_path / "private.txt"
    outside.write_text("SECRET")
    asset = index.parent / name
    asset.unlink()
    _symlink(asset, outside)
    with pytest.raises(ValueError, match="symlinks"):
        report.render_report(_batch(), index.parent)
    with pytest.raises(ValueError, match="symlinks"):
        report.serve_reports(index.parent, port=0)
    assert outside.read_text() == "SECRET"


def test_hardlinked_assets_rejected(tmp_path: Path) -> None:
    index, _ = _render(tmp_path, _case())
    outside = tmp_path / "private.txt"
    outside.write_text("SECRET")
    index.unlink()
    os.link(outside, index)
    with pytest.raises(ValueError, match="non-linked"):
        report.render_report(_batch(), index.parent)
    with pytest.raises(ValueError, match="non-linked"):
        report.serve_reports(index.parent, port=0)
    assert outside.read_text() == "SECRET"


@pytest.mark.parametrize("name", ["index.html", "report.js"])
def test_asset_symlink_swap_after_startup_is_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    index, _ = _render(tmp_path, _case())
    outside = tmp_path / "private.txt"
    outside.write_text("PRIVATE OUTSIDE")
    candidate = tmp_path / "candidate-link"
    _symlink(candidate, outside)
    with _running_server(index.parent, monkeypatch) as address:
        asset = index.parent / name
        asset.unlink()
        candidate.replace(asset)
        status, _, body = _request(address, f"/{name}")
        assert status == 404 and b"PRIVATE OUTSIDE" not in body


def test_asset_directory_swap_after_startup_is_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index, _ = _render(tmp_path, _case())
    with _running_server(index.parent, monkeypatch) as address:
        index.unlink()
        index.mkdir()
        (index / "secret").write_text("PRIVATE")
        status, _, body = _request(address, "/")
        assert status == 404 and b"PRIVATE" not in body


def test_handler_explicitly_refuses_directory_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handler = object.__new__(report._ReportHandler)
    errors: list[tuple[int, str]] = []
    monkeypatch.setattr(handler, "send_error", lambda code, message: errors.append((code, message)))
    assert handler.list_directory(str(tmp_path.parent)) is None
    assert errors == [(403, "Directory listings are disabled")]
