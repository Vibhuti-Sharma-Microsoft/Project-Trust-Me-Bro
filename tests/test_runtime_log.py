from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scoring_service.runtime_log import RuntimeJournal, RuntimeLogError
from scoring_service.models import (
    Claim,
    ClaimsDecision,
    ClaimSupport,
    DocumentEvidence,
    EvidenceItem,
    EvidenceRef,
    GateVote,
    JudgeRecord,
    StepBinding,
    StepJudgment,
    StepResult,
    TodoPlan,
    TodoStep,
    ToolCall,
)


def read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_order_schema_and_flushed_visibility(tmp_path):
    path = tmp_path / "scoring-service.log"
    before = datetime.now(timezone.utc)
    with RuntimeJournal(path) as journal:
        assert path.read_bytes() == b""
        journal.event("gate.completed", "case-1", decision="PASS", rationale="The plan addresses the incident.")
        first = read_events(path)
        assert len(first) == 1
        assert first[0]["sequence"] == 1
        assert first[0]["event"] == "gate.completed"
        assert first[0]["case_id"] == "case-1"
        assert first[0]["details"]["decision"] == "PASS"
        journal.event("score.calculated", score=0.85, arithmetic={"weighted": 85, "denominator": 100})
        events = read_events(path)
        assert [event["sequence"] for event in events] == [1, 2]
        assert events[1]["case_id"] is None
        assert events[1]["details"]["arithmetic"] == {"weighted": 85, "denominator": 100}
        assert all(set(event) == {"sequence", "recorded_at", "event", "case_id", "details"} for event in events)
        stamps = [datetime.fromisoformat(event["recorded_at"]) for event in events]
        assert before <= stamps[0] <= stamps[1] <= datetime.now(timezone.utc)
        assert all(event["recorded_at"].endswith("Z") for event in events)
        assert path.read_bytes().endswith(b"\n")
    assert read_events(path) == events
    assert list(tmp_path.iterdir()) == [path]


def test_concurrent_events_have_complete_contiguous_sequences(tmp_path):
    path = tmp_path / "scoring-service.log"
    with RuntimeJournal(path) as journal:
        def panel(role):
            for index in range(80):
                journal.event("judge.completed", "case-1", role=role, index=index, rationale="Evidence agrees.")

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(panel, ["gpt", "claude", "gemini"]))
        events = read_events(path)
    assert len(events) == 240
    assert [event["sequence"] for event in events] == list(range(1, 241))
    assert [event["recorded_at"] for event in events] == sorted(event["recorded_at"] for event in events)
    assert {(event["details"]["role"], event["details"]["index"]) for event in events} == {
        (role, index) for role in ("gpt", "claude", "gemini") for index in range(80)
    }


def test_utf8_quotes_newlines_and_useful_metadata_preserved(tmp_path):
    path = tmp_path / "scoring-service.log"
    details = {
        "rationale": "Telemetry says caf\u00e9 is healthy; \u6771\u4eac agrees.\nNo degradation.",
        "references": [{"evidence_id": "tool:call-1", "quote": 'Status was "OK".\nNext line.'}],
        "observed_at": "2026-09-17T21:32:44.754+05:30",
        "date": "2026-09-17", "InputTokens": 1234567890,
        "OutputTokens": 321, "reasoning_tokens": 42, "token_count": 99,
        "nullable": None, "verified": True, "faithfulness": 0.5,
    }
    with RuntimeJournal(path) as journal:
        journal.event("step.scored", "case-\u6771\u4eac", **details)
    assert read_events(path)[0]["details"] == details
    raw = path.read_bytes()
    assert b"caf\xc3\xa9" in raw
    assert raw.count(b"\n") == 1
    assert not raw.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("name", [
    "Authorization", "Proxy-Authorization", "api_key", "x-api-key",
    "access_token", "refreshToken", "client_secret", "database_password",
    "private_key", "Secret", "shared_access_key", "connection_string",
    "Ocp-Apim-Subscription-Key", "Cookie", "Set-Cookie", "AWS_SECRET_ACCESS_KEY",
])
def test_explicit_credential_values_redacted_recursively(tmp_path, name):
    path = tmp_path / "scoring-service.log"
    value = {name: {"secret-value": ["never-persist-this"]}, "InputTokens": 123}
    with RuntimeJournal(path) as journal:
        journal.event("judge.completed", nested=[{"data": value}])
    result = read_events(path)[0]["details"]["nested"][0]["data"]
    assert result == {name: "[REDACTED]", "InputTokens": 123}
    assert "never-persist-this" not in path.read_text()
    assert value[name] == {"secret-value": ["never-persist-this"]}


def test_json_encoded_dict_and_list_strings_are_sanitized(tmp_path):
    path = tmp_path / "scoring-service.log"
    inner = {"access_token": "inner-secret", "InputTokens": 55, "when": "2026-09-17"}
    outer = [{"blob": json.dumps(inner), "password": "outer-secret", "rationale": "Accurate evidence."}]
    with RuntimeJournal(path) as journal:
        journal.event("judge.completed", encoded=json.dumps(outer))
    encoded = read_events(path)[0]["details"]["encoded"]
    assert isinstance(encoded, str)
    output = json.loads(encoded)
    assert output[0]["password"] == "[REDACTED]"
    assert json.loads(output[0]["blob"]) == {"access_token": "[REDACTED]", "InputTokens": 55, "when": "2026-09-17"}
    assert "inner-secret" not in path.read_text()
    assert "outer-secret" not in path.read_text()


def test_noncredential_json_quotes_preserved_verbatim(tmp_path):
    path = tmp_path / "scoring-service.log"
    quote = '{ "InputTokens":12345, "when":"2026-09-17T21:32:44Z", "status" : "OK" }\n'
    with RuntimeJournal(path) as journal:
        journal.event("judge.completed", references=[{"evidence_id": "tool:1", "quote": quote}])
    assert read_events(path)[0]["details"]["references"][0]["quote"] == quote


def test_nested_json_string_encoding_cannot_hide_credentials(tmp_path):
    path = tmp_path / "scoring-service.log"
    encoded = json.dumps(json.dumps({"api_key": "double-encoded-secret", "InputTokens": 99}))
    with RuntimeJournal(path) as journal:
        journal.event("judge.completed", encoded=encoded)
    actual = read_events(path)[0]["details"]["encoded"]
    assert json.loads(json.loads(actual)) == {"api_key": "[REDACTED]", "InputTokens": 99}
    assert "double-encoded-secret" not in path.read_text()


@pytest.mark.parametrize(("text", "secrets"), [
    ("Authorization: Bearer abC_123-xyz.456", ["abC_123-xyz.456"]),
    ('Authorization: Digest username="alice", response="digest-secret"', ["alice", "digest-secret"]),
    ("authorization: basic dXNlcjpwYXNz", ["dXNlcjpwYXNz"]),
    ("GET https://alice:p%40ssword@example.test/data?api_key=key123&at=2026-09-17",
     ["alice", "p%40ssword", "key123"]),
    ("https://bob@example.test/path?access_token=at123&refresh_token=rt456#view",
     ["bob", "at123", "rt456"]),
    ("https://example.test/data?%61pi_key=encoded-key&InputTokens=123&sig=signed-secret",
     ["encoded-key", "signed-secret"]),
    ('password="spaces in secret" and client_secret=plain-secret', ["spaces in secret", "plain-secret"]),
    ("fragment: {'api_key': 'quoted-secret'}", ["quoted-secret"]),
    ("-----BEGIN PRIVATE KEY-----\nencoded-secret\n-----END PRIVATE KEY-----", ["encoded-secret"]),
])
def test_conventional_credential_string_patterns(tmp_path, text, secrets):
    path = tmp_path / "scoring-service.log"
    with RuntimeJournal(path) as journal:
        journal.event("evidence.assessed", text=text)
    output = read_events(path)[0]["details"]["text"]
    assert "[REDACTED]" in output
    assert all(secret not in output for secret in secrets)
    if "2026-09-17" in text:
        assert "2026-09-17" in output
    if "InputTokens=123" in text:
        assert "InputTokens=123" in output


def test_named_private_reasoning_redacted_but_rationales_preserved(tmp_path):
    path = tmp_path / "scoring-service.log"
    with RuntimeJournal(path) as journal:
        journal.event("judge.completed", hidden_reasoning="private-thought", scratchpad="private-work",
                      rationale="The claim quotes the observed error rate.", reasoning_tokens=17)
    details = read_events(path)[0]["details"]
    assert details == {
        "hidden_reasoning": "[REDACTED]", "scratchpad": "[REDACTED]",
        "rationale": "The claim quotes the observed error rate.", "reasoning_tokens": 17,
    }


def test_existing_log_and_directory_are_never_overwritten(tmp_path):
    path = tmp_path / "scoring-service.log"
    path.write_bytes(b"previous run\n")
    with pytest.raises(RuntimeLogError, match="already exists"):
        RuntimeJournal(path)
    assert path.read_bytes() == b"previous run\n"
    with pytest.raises(RuntimeLogError):
        RuntimeJournal(tmp_path)


def test_parent_directories_are_not_created(tmp_path):
    parent = tmp_path / "missing"
    with pytest.raises(RuntimeLogError) as caught:
        RuntimeJournal(parent / "scoring-service.log")
    assert isinstance(caught.value.__cause__, FileNotFoundError)
    assert not parent.exists()
    assert not isinstance(caught.value, OSError)


def test_parent_traversal_refused(tmp_path):
    with pytest.raises(RuntimeLogError, match="parent directories"):
        RuntimeJournal(tmp_path / "missing" / ".." / "scoring-service.log")
    assert not (tmp_path / "scoring-service.log").exists()


def make_link(link: Path, target: Path, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")


def test_symlink_destination_refused(tmp_path):
    target = tmp_path / "target.log"
    link = tmp_path / "scoring-service.log"
    make_link(link, target)
    with pytest.raises(RuntimeLogError, match="link or reparse"):
        RuntimeJournal(link)
    assert not target.exists()


def test_symlink_parent_refused(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    make_link(link, target, directory=True)
    with pytest.raises(RuntimeLogError, match="links or reparse"):
        RuntimeJournal(link / "scoring-service.log")
    assert not (target / "scoring-service.log").exists()


@pytest.mark.parametrize("target_kind", ["parent", "destination"])
def test_reparse_attributes_refused_without_following(tmp_path, monkeypatch, target_kind):
    path = tmp_path / "scoring-service.log"
    target = tmp_path if target_kind == "parent" else path
    original = Path.lstat

    def lstat(candidate):
        if candidate == target:
            return SimpleNamespace(st_mode=0o040755, st_file_attributes=0x400)
        return original(candidate)

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(RuntimeLogError, match="reparse"):
        RuntimeJournal(path)
    assert not path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only destination semantics")
@pytest.mark.parametrize("name", ["log:alternate", "NUL", "scoring-service.log.", "scoring-service.log "])
def test_windows_device_and_ambiguous_names_refused(tmp_path, name):
    with pytest.raises(RuntimeLogError, match="unsafe Windows"):
        RuntimeJournal(tmp_path / name)


@pytest.mark.parametrize("value", [
    float("nan"), float("inf"), -float("inf"), object(), {"not-a-list"}, (1, 2),
    b"bytes", Path("file"), datetime(2026, 9, 17), {1: "non-string key"}, "\ud800",
])
def test_unsupported_values_leave_no_partial_event_or_sequence_gap(tmp_path, value):
    path = tmp_path / "scoring-service.log"
    with RuntimeJournal(path) as journal:
        journal.event("before")
        previous = path.read_bytes()
        with pytest.raises(RuntimeLogError, match="finite, serializable UTF-8 JSON"):
            journal.event("invalid", value=value)
        assert path.read_bytes() == previous
        journal.event("after")
    assert [event["sequence"] for event in read_events(path)] == [1, 2]
    assert [event["event"] for event in read_events(path)] == ["before", "after"]


def test_invalid_values_are_not_hidden_by_credential_redaction(tmp_path):
    path = tmp_path / "scoring-service.log"
    cycle = []
    cycle.append(cycle)
    with RuntimeJournal(path) as journal:
        for value in (cycle, float("nan"), object(), "\ud800"):
            with pytest.raises(RuntimeLogError):
                journal.event("invalid", password=value)
        with pytest.raises(RuntimeLogError):
            journal.event("invalid", encoded='{"value":NaN,"password":"secret"}')
        assert path.read_bytes() == b""


def test_close_is_idempotent_and_original_body_exception_preserved(tmp_path):
    path = tmp_path / "scoring-service.log"
    original = ValueError("original evaluator failure")
    journal = RuntimeJournal(path)
    with pytest.raises(ValueError) as caught:
        with journal:
            journal.event("before.failure")
            raise original
    assert caught.value is original
    journal.close()
    journal.close()
    with pytest.raises(RuntimeLogError, match="closed"):
        journal.event("too.late")
    with pytest.raises(RuntimeLogError, match="closed"):
        journal.__enter__()


def test_open_error_is_explicit_and_chained(tmp_path, monkeypatch):
    import scoring_service.runtime_log as runtime_log

    original = PermissionError("blocked by permissions")

    def fail(path):
        raise original

    monkeypatch.setattr(runtime_log, "_open_windows" if os.name == "nt" else "_open_posix", fail)
    with pytest.raises(RuntimeLogError, match="exclusively open") as caught:
        RuntimeJournal(tmp_path / "scoring-service.log")
    assert caught.value.__cause__ is original
    assert not isinstance(caught.value, OSError)


class FailingStream:
    def __init__(self, stream, operation):
        self.stream = stream
        self.operation = operation
        self.failure = OSError("original I/O failure")
        self.close_calls = 0

    def tell(self):
        return self.stream.tell()

    def write(self, data):
        if self.operation == "write":
            self.stream.write(data[:7])
            raise self.failure
        if self.operation == "short":
            return self.stream.write(data[:7])
        return self.stream.write(data)

    def flush(self):
        if self.operation == "flush":
            raise self.failure
        self.stream.flush()

    def seek(self, position):
        return self.stream.seek(position)

    def truncate(self):
        return self.stream.truncate()

    def close(self):
        self.close_calls += 1
        self.stream.close()
        if self.operation == "close":
            raise self.failure


@pytest.mark.parametrize("operation", ["write", "short", "flush"])
def test_io_failures_explicit_no_further_events(tmp_path, operation):
    path = tmp_path / "scoring-service.log"
    with RuntimeJournal(path) as journal:
        journal.event("before")
        previous = path.read_bytes()
        wrapper = FailingStream(journal._stream, operation)
        journal._stream = wrapper
        with pytest.raises(RuntimeLogError, match="persist") as caught:
            journal.event("failed", long_value="Some detail")
        assert isinstance(caught.value.__cause__, OSError)
        if operation != "short":
            assert caught.value.__cause__ is wrapper.failure
        assert not isinstance(caught.value, OSError)
        assert path.read_bytes() == previous
        with pytest.raises(RuntimeLogError, match="unusable"):
            journal.event("cannot.continue")


def test_close_error_is_explicit_and_idempotent(tmp_path):
    journal = RuntimeJournal(tmp_path / "scoring-service.log")
    wrapper = FailingStream(journal._stream, "close")
    journal._stream = wrapper
    with pytest.raises(RuntimeLogError, match="close") as caught:
        journal.close()
    assert caught.value.__cause__ is wrapper.failure
    journal.close()
    assert wrapper.close_calls == 1


def test_close_failure_does_not_mask_body_exception(tmp_path):
    original = LookupError("original scoring failure")
    journal = RuntimeJournal(tmp_path / "scoring-service.log")
    journal._stream = FailingStream(journal._stream, "close")
    with pytest.raises(LookupError) as caught:
        with journal:
            raise original
    assert caught.value is original
    assert any("also failed to close" in note for note in original.__notes__)


def test_competing_journals_have_exactly_one_exclusive_winner(tmp_path):
    path = tmp_path / "scoring-service.log"
    barrier = threading.Barrier(3)

    def attempt(index):
        barrier.wait(timeout=10)
        try:
            with RuntimeJournal(path) as journal:
                journal.event("winner", index=index)
                return True
        except RuntimeLogError:
            return False

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(attempt, range(3)))
    assert results.count(True) == 1
    events = read_events(path)
    assert len(events) == 1
    assert events[0]["sequence"] == 1
    assert events[0]["details"]["index"] == results.index(True)


def test_redaction_does_not_inspect_environment_secrets(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Journal must not read environment secrets")

    monkeypatch.setattr(os, "getenv", forbidden)
    with RuntimeJournal(tmp_path / "scoring-service.log") as journal:
        journal.event("summary", rationale="Evidence is consistent.", InputTokens=12345)


@pytest.mark.parametrize(("name", "case_id"), [("", None), (" ", None), (123, None), ("valid", 123)])
def test_invalid_event_identity_is_explicit(tmp_path, name, case_id):
    path = tmp_path / "scoring-service.log"
    with RuntimeJournal(path) as journal:
        with pytest.raises(RuntimeLogError):
            journal.event(name, case_id)
        assert path.read_bytes() == b""


def test_stream_wrapping_error_closes_descriptor_and_preserves_cause(tmp_path, monkeypatch):
    import scoring_service.runtime_log as runtime_log

    descriptors = []
    original = OSError("could not wrap descriptor")

    def fail(descriptor, *args, **kwargs):
        descriptors.append(descriptor)
        raise original

    monkeypatch.setattr(runtime_log.os, "fdopen", fail)
    with pytest.raises(RuntimeLogError, match="exclusively open") as caught:
        RuntimeJournal(tmp_path / "scoring-service.log")
    assert caught.value.__cause__ is original
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_full_executor_payloads_preserve_semantic_judgments_and_arithmetic(tmp_path):
    path = tmp_path / "scoring-service.log"
    stamp = "2026-09-17T21:32:44.754+05:30"
    quote = '{ "ErrorCount":3, "observed_at":"2026-09-17T16:02:44Z" }'
    todo = TodoPlan(
        source_call_id="plan", created_at=stamp, raw="1. Inspect error telemetry",
        steps=[TodoStep(id="step-1", title="Inspect error telemetry")],
    )
    raw_output = json.dumps({
        "rows": [{"ErrorCount": 3, "observed_at": stamp, "region": "\u6771\u4eac"} for _ in range(128)],
        "InputTokens": 987654321, "OutputTokens": 123, "access_token": "embedded-output-secret",
    })
    tool_input = {"query": "Errors | count", "api_key": "embedded-input-secret"}
    call = ToolCall(
        id="call-1", name="Query", thread_id="thread-1", trace_id="trace-1",
        started_at=stamp, completed_at=stamp, input=tool_input, input_raw=json.dumps(tool_input),
        output_raw=raw_output, status="PAIRED",
    )
    evidence = [
        EvidenceItem(id="context", source_kind="incident_context", origin="incident:1", content="Investigate errors."),
        EvidenceItem(id="todo", source_kind="initial_plan", origin="plan", content=todo.raw),
        EvidenceItem(
            id="tool:call-1", source_kind="telemetry",
            origin="https://reader:origin-secret@metrics.example/data?sig=query-secret&api-version=2026-09-17",
            call_id=call.id, content=raw_output, completed_at=stamp, query="Errors | count",
        ),
    ]
    gate = GateVote(
        decision="PASS", rationale="The plan addresses the reported errors.",
        references=[EvidenceRef(evidence_id="todo", quote="Inspect error telemetry")],
    )
    claim = Claim(id="claim-1", quote="Three errors were observed.", step_id="step-1", claim_type="observation")
    claims = ClaimsDecision(
        claims=[claim],
        bindings=[StepBinding(
            step_id="step-1", call_ids=[call.id], disposition="EVALUATE", rationale="The call supplies telemetry.",
        )],
    )
    support = ClaimSupport(
        claim_id=claim.id, verdict="SUPPORTED", rationale="The source records three errors.",
        references=[EvidenceRef(evidence_id="tool:call-1", quote=quote)],
    )
    judgment = StepJudgment(
        faithfulness=1, rationale="The response faithfully reports the observation.",
        references=support.references, claim_support=[support], source_trust=0.5,
        trust_rationale="The configured telemetry policy caps trust at partial.", trust_policy_ids=["telemetry"],
    )
    document = DocumentEvidence(
        id="doc-1", url="https://docs.example/runbook?access_token=document-secret",
        step_id="step-1", status="AVAILABLE", content="A relevant runbook paragraph.",
        last_updated=None, historical_version_verified=True, metadata_provenance="Versioned snapshot",
        reason="Historical content is verified, but the last-updated date is missing.",
    )
    result = StepResult(
        id="step-1", title=todo.steps[0].title, disposition="EVALUATE", included=True,
        call_ids=[call.id], claim_ids=[claim.id], faithfulness=1, coverage=1,
        source_trust=0.5, freshness=0, freshness_reason=document.reason, score=0.8,
        votes={"gpt": 1, "claude": 0.5, "gemini": 1}, support=[support],
    )
    calculations = {
        "faithfulness": {"votes": result.votes, "operator": "median", "result": 1},
        "coverage": {"verdicts": [support.model_dump(mode="json")], "rule": "All supported -> 1", "result": 1},
        "source_trust": {"result": 0.5, "rationale": judgment.trust_rationale, "policy_ids": ["telemetry"]},
        "document_freshness": {"result": 0, "reason": document.reason, "cutoff": stamp},
        "weighted_terms": {"faithfulness": 35, "coverage": 35, "source_trust": 10.0, "freshness": 0},
        "step_score": 0.8,
    }
    gate_input = {
        "case_id": "case-1", "data_sha256": "a" * 64,
        "initial_plan": todo.model_dump(mode="json"),
        "evidence": [item.model_dump(mode="json") for item in evidence[:2]],
    }
    with RuntimeJournal(path) as journal:
        journal.event("run.started", run_id="run-1", weights={
            "faithfulness": 35, "coverage": 35, "source_trust": 20, "freshness": 10,
        })
        journal.event("case.started", "case-1", cutoff=stamp, synthetic=True)
        journal.event(
            "bundle.prepared", "case-1", selected_response=claim.quote,
            initial_todo=todo.model_dump(mode="json"), context_available_at=stamp, cutoff=stamp,
            calls=[call.model_dump(mode="json")],
            evidence=[item.model_dump(mode="json") for item in evidence], data_sha256="a" * 64,
        )
        bundle_event = read_events(path)[-1]
        assert bundle_event["event"] == "bundle.prepared"

        def panel(role):
            journal.event("judge.started", "case-1", role=role, stage="todo_gate",
                          step_id=None, evaluator_input=gate_input)
            record = JudgeRecord(
                role=role, stage="todo_gate", model=f"synthetic-{role}",
                request_sha256=f"request-{role}", prompt_sha256="b" * 64,
                mode="replay", output=gate.model_dump(mode="json"),
            )
            journal.event(
                "judge.returned", "case-1", role=role, stage="todo_gate", step_id=None,
                elapsed_ms=12.345, judgment=gate.model_dump(mode="json"),
                provenance=record.model_dump(mode="json"),
            )

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(panel, ["gpt", "claude", "gemini"]))
        journal.event("gate.decided", "case-1", decision="PASS",
                      votes={role: gate.model_dump(mode="json") for role in ("gpt", "claude", "gemini")})
        journal.event("claims.validated", "case-1", claims=claims.model_dump(mode="json"))
        journal.event("documents.prepared", "case-1", step_id="step-1", documents=[document.model_dump(mode="json")])
        journal.event("step.scored", "case-1", step=result.model_dump(mode="json"), calculations=calculations)
        journal.event("step.excluded", "case-1", step={"id": "housekeeping", "included": False},
                      rule="Housekeeping is not in the denominator.")
        journal.event("case.finished", "case-1", status="SCORED", score=0.8,
                      included_step_count=1, contributions=[{"step_id": "step-1", "step_score": 0.8, "contribution": 0.8}])
        journal.event("run.completed", statuses={"SCORED": 1}, case_scores={"case-1": 0.8})
        events = read_events(path)
    assert len(events) == 16
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    details = bundle_event["details"]
    assert details["cutoff"] == stamp
    assert details["context_available_at"] == stamp
    assert details["initial_todo"] == todo.model_dump(mode="json")
    assert details["calls"][0]["input"]["api_key"] == "[REDACTED]"
    assert json.loads(details["calls"][0]["input_raw"])["api_key"] == "[REDACTED]"
    decoded_output = json.loads(details["calls"][0]["output_raw"])
    assert decoded_output["access_token"] == "[REDACTED]"
    assert decoded_output["InputTokens"] == 987654321
    assert len(decoded_output["rows"]) == 128
    assert all(row["observed_at"] == stamp for row in decoded_output["rows"])
    assert details["evidence"][2]["origin"].endswith("api-version=2026-09-17")
    for role in ("gpt", "claude", "gemini"):
        role_events = [event for event in events if event["details"].get("role") == role]
        assert [event["event"] for event in role_events] == ["judge.started", "judge.returned"]
        assert role_events[0]["details"]["evaluator_input"] == gate_input
        assert role_events[1]["details"]["judgment"] == gate.model_dump(mode="json")
        assert role_events[1]["details"]["provenance"]["request_sha256"] == f"request-{role}"
    scored = next(event["details"] for event in events if event["event"] == "step.scored")
    assert scored["step"] == result.model_dump(mode="json")
    assert scored["calculations"] == calculations
    assert scored["step"]["support"][0]["references"][0]["quote"] == quote
    docs = next(event["details"]["documents"] for event in events if event["event"] == "documents.prepared")
    assert docs[0]["last_updated"] is None
    assert docs[0]["historical_version_verified"] is True
    assert docs[0]["reason"] == document.reason
    for secret in ("embedded-input-secret", "embedded-output-secret", "origin-secret", "query-secret", "document-secret"):
        assert secret not in path.read_text(encoding="utf-8")
    assert call.input == tool_input
    assert call.output_raw == raw_output


def test_deep_payload_validation_failure_does_not_change_existing_journal(tmp_path):
    path = tmp_path / "scoring-service.log"
    with RuntimeJournal(path) as journal:
        journal.event("bundle.prepared", "case-1", calls=[], evidence=[])
        before = path.read_bytes()
        for details in (
            {"evaluator_input": {"calls": [{"input": {"query": Path("not-json")}}]}},
            {"calculations": {"weighted_terms": {"faithfulness": float("inf")}}},
            {"provenance": {"output": {"references": [{"quote": "\ud800"}]}}},
        ):
            with pytest.raises(RuntimeLogError, match="serializable"):
                journal.event("judge.returned", "case-1", **details)
            assert path.read_bytes() == before
        journal.event("judge.failed", "case-1", role="gpt", stage="step", step_id="step-1",
                      error_type="JudgeError", error="Request failed at 2026-09-17T16:02:44Z; api_key=error-secret")
    events = read_events(path)
    assert [event["sequence"] for event in events] == [1, 2]
    assert "2026-09-17T16:02:44Z" in events[-1]["details"]["error"]
    assert "error-secret" not in path.read_text()
