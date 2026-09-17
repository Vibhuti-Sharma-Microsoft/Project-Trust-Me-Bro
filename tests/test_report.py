from __future__ import annotations

import http.client
import inspect
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
from urllib.parse import urlsplit

import pytest
from markupsafe import Markup

from sre_assurance import report
from sre_assurance.config import EvaluationConfig
from sre_assurance.models import (
    BatchResult,
    CaseResult,
    Claim,
    ClaimSupport,
    DocumentEvidence,
    EvidenceItem,
    EvidenceRef,
    GateVote,
    JudgeRecord,
    StepResult,
    TodoPlan,
    TodoStep,
    ToolCall,
)


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(child.text if isinstance(child, _Node) else child for child in self.children)


class _Page(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[_Node] = []
        self.stack = [_Node("root")]
        self.feed(html)
        self.close()
        assert len(self.stack) == 1, "Unclosed HTML elements"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, dict(attrs))
        self.nodes.append(node)
        self.stack[-1].children.append(node)
        if tag not in {"meta", "link", "br", "hr", "img", "input"}:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        assert self.stack[-1].tag == tag, f"Unbalanced HTML: {tag}"
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)

    def find(self, tag: str, **attrs: str) -> _Node:
        return next(node for node in self.nodes if node.tag == tag and all(node.attrs.get(k) == v for k, v in attrs.items()))

    @property
    def text(self) -> str:
        return self.stack[0].text


def _case(**changes: Any) -> CaseResult:
    fields: dict[str, Any] = {
        "case_id": "case-1",
        "incident_id": "incident-123",
        "message_id": "message-456",
        "synthetic": False,
        "status": "SCORED",
        "score": 75.0,
        "cutoff": "2026-09-17T09:00:00Z",
        "response_text": "The first diagnostic response.",
        "data_sha256": "case-data-hash",
        "policy_sha256": "case-policy-hash",
    }
    fields.update(changes)
    return CaseResult(**fields)


def _batch(*cases: CaseResult, **changes: Any) -> BatchResult:
    fields: dict[str, Any] = {
        "run_id": "run-local",
        "created_at": "2026-09-17T10:00:00Z",
        "policy_version": "custom-policy",
        "policy_sha256": "batch-policy-hash",
        "target_real_cases": 10,
        "selected_real_cases": 999,
        "results": list(cases),
    }
    fields.update(changes)
    return BatchResult(**fields)


def _render(tmp_path: Path, *cases: CaseResult, **changes: Any) -> tuple[Path, _Page]:
    path = report.render_report(_batch(*cases, **changes), tmp_path / "reports")
    return path, _Page(path.read_text(encoding="utf-8"))


def test_public_api_and_local_assets(tmp_path: Path) -> None:
    path, page = _render(tmp_path, _case())
    assert path == tmp_path / "reports" / "index.html"
    assert {item.name for item in path.parent.iterdir()} == {"index.html", "report.css"}
    css = path.with_name("report.css").read_text(encoding="utf-8")
    assert "details" in css and "@media" in css and "focus-visible" in css
    assert "url(" not in css and "@import" not in css
    assert page.find("link", rel="stylesheet").attrs["href"] == "report.css"
    assert not any(node.tag in {"script", "iframe", "img", "object", "embed"} for node in page.nodes)
    assert "Uncalibrated evidence index" in page.text
    for dimension, weight in {"faithfulness": 35, "coverage": 35, "source_trust": 20, "freshness": 10}.items():
        assert page.find("dd", **{"data-weight": dimension}).text == f"{weight}%"
    assert "Policy weights are not recorded" not in page.text
    assert "Weights unavailable" not in page.text
    assert "batch-policy-hash" in page.text and "case-policy-hash" in page.text
    assert "case-data-hash" in page.text and "First diagnostic message ID" in page.text
    assert "incident-123" in page.text and "message-456" in page.text
    assert "report.css" not in report.render_report(_batch(), path.parent).read_text().split("<body>")[1]
    assert "No case results" in path.read_text()
    assert not list(path.parent.glob(".*"))
    signature = inspect.signature(report.serve_reports)
    assert tuple(signature.parameters) == ("directory", "host", "port")
    assert signature.parameters["host"].default == "127.0.0.1"
    assert signature.parameters["port"].default == 8080
    assert tuple(inspect.signature(report.render_report).parameters) == ("batch", "output_dir")


@pytest.mark.parametrize(
    "weights",
    [
        {"faithfulness": 35, "coverage": 35, "source_trust": 20, "freshness": 10},
        {"freshness": 5, "source_trust": 15, "coverage": 40, "faithfulness": 40},
    ],
    ids=["default-policy", "alternate-policy"],
)
def test_actual_policy_weights_in_summary_formula_and_steps(tmp_path: Path, weights: dict[str, int]) -> None:
    config = EvaluationConfig(weights=weights)
    score = weights["faithfulness"] + 0.5 * weights["coverage"] + weights["freshness"]
    steps = [
        StepResult(id="weighted", title="Evidence", disposition="EVALUATE", included=True,
                   faithfulness=1, coverage=0.5, source_trust=0, freshness=1, score=score),
        StepResult(id="zero", title="Unsupported", disposition="EVALUATE", included=True,
                   faithfulness=0, coverage=0, source_trust=0, freshness=0, score=0),
        StepResult(id="excluded", title="Housekeeping", disposition="HOUSEKEEPING", included=False),
    ]
    _, page = _render(tmp_path, _case(steps=steps, score=score / 2), weights=config.weights)
    summary = page.find("section", **{"aria-labelledby": "batch-heading"})
    weight_list = page.find("dl", **{"aria-label": "Policy dimension weights"})
    assert weight_list in summary.children
    caption = page.find("caption").text
    for key, abbreviation, label in (
        ("faithfulness", "F", "faithfulness"), ("coverage", "C", "coverage"),
        ("source_trust", "T", "source trust"), ("freshness", "P", "freshness"),
    ):
        assert page.find("dd", **{"data-weight": key}).text == f"{weights[key]}%"
        assert f"{abbreviation}: {label} ({weights[key]}%)" in caption
    formula = (
        f"Step index = F x {weights['faithfulness']} + C x {weights['coverage']} "
        f"+ T x {weights['source_trust']} + P x {weights['freshness']}."
    )
    assert formula in page.text
    assert "Policy weights are not recorded" not in page.text
    assert "Weights unavailable" not in page.text
    if weights["faithfulness"] != 35:
        assert "35%" not in page.text
    row = page.find("tr", **{"data-step-id": "weighted"})
    assert f"{score:.2f}" in row.text and f"{score / 2:.2f}" in row.text
    assert "/ 2 included steps" in row.text
    excluded = page.find("tr", **{"data-step-id": "excluded"}).text
    assert "Unavailable" in excluded and "Excluded - no contribution" in excluded
    assert page.find("strong", **{"data-metric": "mean-score"}).text == f"{score / 2:.2f}"


def test_actual_counts_statuses_and_null_scores(tmp_path: Path) -> None:
    statuses = ["SCORED", "SCORED", "SCORED", "GATE_FAILED", "UNSCORABLE", "JUDGE_ERROR", "IMPORT_ERROR", "NOT_APPLICABLE"]
    scores = [80.0, 0.0, None, None, None, None, None, None]
    cases = [
        _case(case_id=f"case-{i}", status=status, score=score, synthetic=i >= 5)
        for i, (status, score) in enumerate(zip(statuses, scores))
    ]
    _, page = _render(tmp_path, *cases)
    assert page.find("strong", **{"data-metric": "real-count"}).text == "5"
    assert page.find("strong", **{"data-metric": "synthetic-count"}).text == "3"
    assert page.find("strong", **{"data-metric": "mean-score"}).text == "40.00"
    assert "2 available scores" in page.text
    assert "Selection metadata and actual real case count differ" in page.text
    for i, status in enumerate(statuses, start=1):
        text = page.find("article", id=f"case-{i}").text
        assert status in text
        if i > 2:
            assert "Reported index: Unavailable" in text
            assert "Reported index: 0.00" not in text
    gate_details = next(
        node for node in page.nodes
        if node.tag == "details" and "Todo gate model votes" in node.text and node.attrs.get("open") is None and "open" in node.attrs
    )
    assert "Missing votes are not passes" in gate_details.text


@pytest.mark.parametrize("selected", [0, 1, 10, 12])
def test_synthetic_selection_note_is_visible_and_counts_are_independent(tmp_path: Path, selected: int) -> None:
    _, page = _render(
        tmp_path,
        _case(case_id="real", score=None),
        _case(case_id="synthetic", synthetic=True, score=None),
        selected_real_cases=selected,
        target_real_cases=10,
    )
    note = page.find("p", id="input-selection-note")
    summary = page.find("section", **{"aria-labelledby": "batch-heading"})
    assert note in summary.children
    assert "Input cases labeled synthetic are shown separately" in note.text
    assert "do not count toward the real-case target" in note.text
    assert f"Selected real cases: {selected} / target real cases: 10." in note.text
    assert ("The real-case selection is below target." in note.text) == (selected < 10)
    assert page.find("strong", **{"data-metric": "real-count"}).text == "1"
    assert page.find("strong", **{"data-metric": "synthetic-count"}).text == "1"
    assert page.find("strong", **{"data-metric": "mean-score"}).text == "Unavailable"
    assert "0 available scores" in page.text
    assert "Synthetic case" in page.find("article", id="case-2").text
    assert "Null scores are unavailable, never zero" in page.text


def test_renderer_preserves_cli_owned_results_json(tmp_path: Path) -> None:
    output = tmp_path / "reports"
    output.mkdir()
    results = output / "results.json"
    original = b'{"results":[{"score":null}]}'
    results.write_bytes(original)
    report.render_report(_batch(_case(score=None)), output)
    assert results.read_bytes() == original
    assert {item.name for item in output.iterdir()} == {"index.html", "report.css", "results.json"}


@pytest.mark.parametrize("status", ["GATE_FAILED", "UNSCORABLE", "JUDGE_ERROR", "IMPORT_ERROR", "NOT_APPLICABLE"])
def test_non_scored_values_never_enter_mean(tmp_path: Path, status: str) -> None:
    _, page = _render(tmp_path, _case(status=status, score=100.0))
    assert page.find("strong", **{"data-metric": "mean-score"}).text == "Unavailable"
    assert "Reported index: 100.00" in page.text
    assert "not a scored success" in page.text


def test_included_excluded_missing_and_zero_step_scores(tmp_path: Path) -> None:
    steps = [
        StepResult(id="first", title="Observe", disposition="EVALUATE", included=True,
                   faithfulness=1, coverage=0.5, source_trust=0, freshness=1, score=90,
                   votes={"gpt": 1, "claude": 0.5, "gemini": 0},
                   freshness_reason="No documentation used", limitations=["Missing logs"]),
        StepResult(id="missing", title="Missing", disposition="MISSING_REQUIRED", included=True),
        StepResult(id="zero", title="Unsupported", disposition="EVALUATE", included=True,
                   faithfulness=0, coverage=0, source_trust=0, freshness=0, score=0,
                   votes={"gpt": 0.5, "claude": 0.5, "gemini": 0.5}),
        StepResult(id="house", title="Tidy", disposition="HOUSEKEEPING", included=False, score=99),
        StepResult(id="conditional", title="Inactive condition", disposition="NOT_APPLICABLE", included=False),
    ]
    _, page = _render(tmp_path, _case(steps=steps))
    first = page.find("tr", **{"data-step-id": "first"})
    cells = [child for child in first.children if isinstance(child, _Node) and child.tag == "td"]
    assert [cell.text for cell in cells[2:5]] == ["1.0", "0.5", "0.0"]
    assert "30.00" in cells[-1].text and "/ 3 included steps" in cells[-1].text
    assert "Neutral convention; no docs, not verified freshness" in cells[5].text
    missing = page.find("tr", **{"data-step-id": "missing"}).text
    assert missing.count("Unavailable") == 6 and "0.00" not in missing
    zero = page.find("tr", **{"data-step-id": "zero"}).text
    assert zero.count("0.00") == 2
    for name in ("house", "conditional"):
        row = page.find("tr", **{"data-step-id": name}).text
        assert "No - excluded" in row and "Excluded - no contribution" in row
    first_details = page.find("details", id="case-1-step-1")
    assert "Model disagreement" in first_details.text and "Missing logs" in first_details.text
    votes = next(child for child in first_details.children if isinstance(child, _Node) and child.tag == "dl" and child.attrs.get("class") == "vote-list")
    assert "gpt1.0" in votes.text and "claude0.5" in votes.text and "gemini0.0" in votes.text
    assert "Recorded model votes agree" in page.find("details", id="case-1-step-3").text
    assert "no agreement can be inferred" in page.find("details", id="case-1-step-2").text


def test_full_audit_content_and_document_freshness(tmp_path: Path) -> None:
    ref = EvidenceRef(evidence_id="ev-1", quote="Supporting log quote")
    case = _case(
        todo=TodoPlan(source_call_id="todo-call", created_at="todo-created", raw="Original todo",
                      steps=[TodoStep(id="step-1", title="Check logs", kind="conditional", condition="If failing")]),
        gate_votes={
            "gpt": GateVote(decision="PASS", rationale="Complete plan", references=[ref]),
            "claude": GateVote(decision="FAIL", rationale="Missing an investigation"),
            "gemini": GateVote(decision="INSUFFICIENT_EVIDENCE", rationale="No context"),
        },
        claims=[Claim(id="claim-1", quote="Response claim", step_id="step-1", claim_type="conclusion")],
        steps=[StepResult(id="step-1", title="Check logs", disposition="EVALUATE", included=True,
                          freshness=1, score=50, call_ids=["call-1"], claim_ids=["claim-1"],
                          support=[ClaimSupport(claim_id="claim-1", verdict="PARTIAL", references=[ref], rationale="Only partial")])],
        calls=[ToolCall(id="call-1", name="query_logs", thread_id="thread-1", trace_id="trace-1", status="CONFLICT",
                        started_at="start-time", completed_at="end-time", start_record_ids=["start-row"],
                        end_record_ids=["end-row"], input={"table": "selected-table"}, input_raw="raw selector",
                        output_raw="raw log output", quality_flags=["conflicting outputs"])],
        evidence=[EvidenceItem(id="ev-1", source_kind="Kusto", origin="https://logs.example.test/query",
                               content="complete evidence text", source_record_id="evidence-row", call_id="call-1",
                               observed_at="observed-time", completed_at="completed-time", query="Table | take 1",
                               quality_flags=["missing source details"], eligible=False)],
        documents=[
            DocumentEvidence(id="doc-1", url="https://docs.example.test/runbook", step_id="step-1", status="AVAILABLE",
                             content="Runbook text", content_sha256="document-content-hash", last_updated="2025-01-02",
                             retrieved_at="2026-09-16", version="version-42", historical_version_verified=False,
                             metadata_provenance="lastupdated header", reason="Historical snapshot not verified"),
            DocumentEvidence(id="doc-missing", url="https://docs.example.test/missing", step_id="step-1",
                             status="MISSING", reason="No approved snapshot available"),
        ],
        judges=[JudgeRecord(role="gpt", stage="step", step_id="step-1", model="approved-model",
                            request_sha256="request-hash", prompt_sha256="prompt-hash", mode="cache",
                            output={"rationale": "Model explanation", "source_trust": 0.5})],
        limitations=["Only one diagnostic response"], error="Recorded audit diagnostic",
    )
    _, page = _render(tmp_path, case)
    for expected in (
        "Original todo", "todo-call", "todo-created", "If failing", "Complete plan", "Missing an investigation",
        "INSUFFICIENT_EVIDENCE", "recorded gate decisions differ", "Supporting log quote", "Response claim",
        "PARTIAL", "Only partial", "thread-1", "trace-1", "start-row", "end-row", "selected-table",
        "raw selector", "raw log output", "CONFLICT", "conflicting outputs", "evidence-row", "Table | take 1",
        "complete evidence text", "Ineligible", "observed-time", "completed-time", "missing source details",
        "AVAILABLE", "Runbook text", "document-content-hash", "2025-01-02", "2026-09-16", "version-42",
        "Cache version", "Not recorded in batch schema", "lastupdated header", "Historical snapshot not verified",
        "Unknown - not verified", "No approved snapshot available", "request-hash", "prompt-hash",
        "approved-model", "cache", "Model explanation", "Only one diagnostic response", "Recorded audit diagnostic",
    ):
        assert expected in page.text
    assert "Neutral convention" not in page.find("tr", **{"data-step-id": "step-1"}).text
    for node in page.nodes:
        if node.tag == "a" and node.attrs.get("target") == "_blank":
            assert node.attrs["rel"] == "noopener noreferrer"


def test_all_untrusted_content_is_text_not_html(tmp_path: Path) -> None:
    attack = '<script>alert("incident")</script><img src=x onerror="alert(1)"><svg/onload=alert(2)>'
    ref = EvidenceRef(evidence_id=attack, quote=attack)
    case = _case(
        case_id=attack, incident_id=attack, message_id=attack, cutoff=attack, response_text=attack,
        error=attack, policy_sha256=attack, data_sha256=attack, limitations=[attack],
        todo=TodoPlan(source_call_id=attack, created_at=attack, raw=attack,
                      steps=[TodoStep(id=attack, title=attack, condition=attack)]),
        gate_votes={attack: GateVote(decision="FAIL", rationale=attack, references=[ref])},
        claims=[Claim(id=attack, quote=attack, step_id=attack, claim_type="observation")],
        steps=[StepResult(id=attack, title=attack, disposition=attack, included=True,
                          freshness_reason=attack, limitations=[attack], call_ids=[attack], claim_ids=[attack],
                          votes={attack: 0.5}, support=[ClaimSupport(claim_id=attack, verdict="UNSUPPORTED",
                                                                  references=[ref], rationale=attack)])],
        calls=[ToolCall(id=attack, name=attack, thread_id=attack, trace_id=attack, started_at=attack,
                        completed_at=attack, start_record_ids=[attack], end_record_ids=[attack],
                        input_raw=attack, output_raw=attack, input={"selector": attack}, quality_flags=[attack])],
        evidence=[EvidenceItem(id=attack, source_kind=attack, origin=attack, content=attack, source_record_id=attack,
                               call_id=attack, observed_at=attack, completed_at=attack, query=attack, quality_flags=[attack])],
        documents=[DocumentEvidence(id=attack, url=attack, step_id=attack, status=attack, content=attack,
                                    content_sha256=attack, last_updated=attack, retrieved_at=attack,
                                    version=attack, metadata_provenance=attack, reason=attack)],
        judges=[JudgeRecord(role="gpt", stage="step", model=attack, request_sha256=attack,
                            prompt_sha256=attack, step_id=attack, mode="replay", output={"rationale": attack})],
    )
    path, page = _render(tmp_path, case, run_id=attack, created_at=attack, policy_version=attack, policy_sha256=attack)
    html = path.read_text(encoding="utf-8")
    assert attack not in html
    assert "&lt;script&gt;" in html and "&lt;img" in html
    assert attack in page.text
    assert page.find("tr", **{"data-step-id": attack})
    for node in page.nodes:
        assert node.tag not in {"script", "img", "svg", "iframe", "object", "embed"}
        assert not any(key.lower().startswith("on") for key in node.attrs)
    assert "|safe" not in (Path(report.__file__).parent / "templates" / "report.html.j2").read_text()


def test_html_marked_string_subclasses_are_still_untrusted(tmp_path: Path) -> None:
    attack = '<svg onload="alert(1)">untrusted</svg>'
    case = _case()
    case.limitations.append(Markup(attack))
    case.gate_votes[Markup(attack)] = GateVote(decision="FAIL", rationale="Missing plan")
    step = StepResult(id="s", title="Step", included=True, disposition="EVALUATE")
    step.limitations.append(Markup(attack))
    case.steps.append(step)
    _, page = _render(tmp_path, case)
    assert attack in page.text
    assert not any(node.tag == "svg" for node in page.nodes)
    assert not any("onload" in node.attrs for node in page.nodes)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)", "JaVaScRiPt:alert(1)", "file:///C:/private.txt",
        "data:text/html,<script>alert(1)</script>", "vbscript:alert(1)", "//evil.example/path",
        " https://example.test", "https:\n//example.test", "https://example.test\\@evil.test",
        "https://example.test:bad/path", "https://user:password@example.test", "https://",
        "https://[bad-ip]/", "relative/path", "\x00https://example.test",
    ],
)
def test_unsafe_source_urls_are_not_links(tmp_path: Path, url: str) -> None:
    _, page = _render(
        tmp_path,
        _case(
            evidence=[EvidenceItem(id="e", source_kind="source", origin=url, content="Text")],
            documents=[DocumentEvidence(id="d", url=url, step_id="s", status="MISSING")],
        ),
    )
    assert url in page.text
    assert not any(node.tag == "a" and node.attrs.get("target") == "_blank" for node in page.nodes)


@pytest.mark.parametrize("url", ["https://example.test/a?x=1&y=2#section", "HTTP://example.test/path", 'https://example.test/?q="<svg>'])
def test_http_source_urls_are_escaped_and_isolated(tmp_path: Path, url: str) -> None:
    _, page = _render(tmp_path, _case(documents=[DocumentEvidence(id="d", url=url, step_id="s", status="AVAILABLE")]))
    anchor = page.find("a", target="_blank")
    assert anchor.attrs == {"href": url, "target": "_blank", "rel": "noopener noreferrer"}
    assert urlsplit(anchor.attrs["href"]).scheme.lower() in {"http", "https"}
    assert not any(node.tag == "svg" for node in page.nodes)


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
    index, _ = _render(tmp_path, _case(response_text="visible report"))
    (tmp_path / "parent-secret.txt").write_text("PRIVATE PARENT DATA")
    (index.parent / "config.json").write_text("PRIVATE CONFIG DATA")
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
        status, headers, body = _request(address, "/report.css")
        assert status == 200 and b"system-ui" in body
        assert headers["Content-Type"] == "text/css; charset=utf-8"
        assert _request(address, "/", method="HEAD")[2] == b""
        for path in (
            "/../parent-secret.txt", "/%2e%2e/parent-secret.txt", "/..%2fparent-secret.txt",
            "/%252e%252e/parent-secret.txt", "/..\\parent-secret.txt", "/%2e%2e%5cparent-secret.txt",
            "/config.json", "/cache/", "/corpus/", "/nested/index.html", "/./index.html",
            "/report.css/../config.json", "/index.html/extra", "/index.html%00", "/%ff",
            "/C:/private.txt", "http://evil.example/index.html",
        ):
            status, _, body = _request(address, path, headers={"Host": f"{address[0]}:{address[1]}"})
            assert status == 404, path
            assert b"PRIVATE" not in body and b"Directory listing" not in body
        assert _request(address, "/", headers={"Host": "evil.example"})[0] == 403
        assert _request(address, "/", headers={"Host": "localhost@evil.example"})[0] == 403
        assert _request(address, "/", headers={"Host": "localhost:1"})[0] == 403


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


@pytest.mark.parametrize("name", ["index.html", "report.css"])
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


def test_asset_symlink_swap_after_startup_is_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index, _ = _render(tmp_path, _case())
    outside = tmp_path / "private.txt"
    outside.write_text("PRIVATE OUTSIDE")
    candidate = tmp_path / "candidate-link"
    _symlink(candidate, outside)
    with _running_server(index.parent, monkeypatch) as address:
        index.unlink()
        candidate.replace(index)
        status, _, body = _request(address, "/")
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
