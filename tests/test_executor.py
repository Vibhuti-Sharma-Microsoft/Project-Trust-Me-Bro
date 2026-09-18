import json
from unittest.mock import patch

import pytest

from scoring_service.executor import evaluate_case
from scoring_service.imports import write_json


def test_replay_batch_expected_outcomes(corpus, tmp_path):
    root, manifest, config = corpus
    results = [evaluate_case(case, root, config, tmp_path / "cache") for case in manifest.cases]
    expected = [
        ("SCORED", 100), ("SCORED", 65), ("SCORED", 30), ("GATE_FAILED", 0),
        ("UNSCORABLE", None), ("SCORED", 90), ("SCORED", 90), ("SCORED", 80),
        ("SCORED", 0), ("JUDGE_ERROR", None),
    ]
    assert [(result.status, result.score) for result in results] == expected
    assert set(results[-1].gate_votes) == {"gpt", "claude"}
    assert all(result.synthetic for result in results)


def test_gate_failure_and_missing_todo_never_fetch_docs_or_judge_steps(corpus, tmp_path):
    root, manifest, config = corpus
    with patch("scoring_service.executor.DocumentStore") as docs:
        for case in manifest.cases[3:5]:
            result = evaluate_case(case, root, config, tmp_path / "cache")
            assert not result.steps
            assert not result.claims
            assert all(record.stage == "todo_gate" for record in result.judges)
        docs.assert_not_called()


def test_unknown_citation_is_not_a_success(corpus, tmp_path):
    root, manifest, config = corpus
    case = manifest.cases[0]
    replay_path = root / case.replay_path
    replay = json.loads(replay_path.read_text())
    replay["step:step-1:gpt"]["output"]["references"][0]["evidence_id"] = "fabricated"
    write_json(replay_path, replay)
    result = evaluate_case(case, root, config, tmp_path / "cache")
    assert result.status == "JUDGE_ERROR"
    assert result.score is None


def test_unassigned_claims_are_zero_contributions(corpus, tmp_path):
    root, manifest, config = corpus
    case = manifest.cases[0]
    path = root / case.replay_path
    replay = json.loads(path.read_text())
    replay["claims:gpt"]["output"]["claims"].append({
        "id": "unassigned", "quote": "The service", "step_id": None, "claim_type": "conclusion", "material": True,
    })
    write_json(path, replay)
    result = evaluate_case(case, root, config, tmp_path / "cache")
    assert result.status == "SCORED"
    assert result.score == 50
    assert result.steps[-1].disposition == "UNASSIGNED"


def test_source_trust_cannot_exceed_reviewed_authority(corpus, tmp_path):
    root, manifest, config = corpus
    case = manifest.cases[7]
    path = root / case.replay_path
    replay = json.loads(path.read_text())
    replay["step:step-1:gpt"]["output"].update(source_trust=1, trust_policy_ids=["demo-kusto"])
    write_json(path, replay)
    result = evaluate_case(case, root, config, tmp_path / "cache")
    assert result.status == "JUDGE_ERROR"
    assert result.score is None


def test_replay_is_deterministic(corpus, tmp_path):
    root, manifest, config = corpus
    first = evaluate_case(manifest.cases[0], root, config, tmp_path / "cache")
    second = evaluate_case(manifest.cases[0], root, config, tmp_path / "cache")
    assert first.score == second.score
    assert first.steps == second.steps
    assert first.data_sha256 == second.data_sha256
    assert {record.request_sha256 for record in first.judges} == {record.request_sha256 for record in second.judges}
    assert first.judges == second.judges


def test_material_claims_cannot_hide_in_housekeeping(corpus, tmp_path):
    root, manifest, config = corpus
    case = manifest.cases[0]
    logs_path = root / case.log_files["customEvents"]
    events = json.loads(logs_path.read_text())
    todo = json.loads(events[0]["customDimensions"]["ToolInput"])
    todo["todos"].append({"id": 2, "title": "Post results", "status": "not-started"})
    events[0]["customDimensions"]["ToolInput"] = json.dumps(todo)
    write_json(logs_path, events)
    replay_path = root / case.replay_path
    replay = json.loads(replay_path.read_text())
    claims = replay["claims:gpt"]["output"]
    claims["claims"].append({"id": "hidden", "quote": "The service", "step_id": "step-2",
                            "claim_type": "conclusion", "material": True})
    claims["bindings"].append({"step_id": "step-2", "call_ids": [], "disposition": "HOUSEKEEPING",
                              "rationale": "Posting is not source evidence"})
    write_json(replay_path, replay)
    result = evaluate_case(case, root, config, tmp_path / "cache")
    assert result.status == "SCORED"
    assert result.score == 50
    assert "hidden" in result.steps[-1].claim_ids


def test_orphan_call_does_not_satisfy_required_producing_work(corpus, tmp_path):
    root, manifest, config = corpus
    case = manifest.cases[0]
    path = root / case.log_files["customEvents"]
    events = json.loads(path.read_text())
    events[3]["timestamp"] = "2026-09-16T01:03:00.2451909Z"
    write_json(path, events)
    result = evaluate_case(case, root, config, tmp_path / "cache")
    assert result.status == "SCORED"
    assert result.score == 0
    assert result.steps[0].disposition == "MISSING_REQUIRED"
    assert not any(record.stage == "step" for record in result.judges)


def test_future_document_body_cannot_be_cited(corpus, tmp_path):
    root, manifest, config = corpus
    case = manifest.cases[5]
    case.documents[0].last_updated = "2026-09-16T01:03:00.2451909Z"
    path = root / case.replay_path
    replay = json.loads(path.read_text())
    reference = {"evidence_id": "doc:region-doc", "quote": "synthetic service region is eastus"}
    for role in ("gpt", "claude", "gemini"):
        replay[f"step:step-1:{role}"]["output"]["references"] = [reference]
    write_json(path, replay)
    result = evaluate_case(case, root, config, tmp_path / "cache")
    assert result.status == "JUDGE_ERROR"
    assert result.score is None
    assert result.documents[0].status == "AFTER_CUTOFF"
    assert result.documents[0].content == ""


def test_each_referenced_document_requires_its_own_mapping(corpus, tmp_path):
    root, manifest, config = corpus
    case = manifest.cases[5]
    case.documents[0].last_updated = "2026-09-01T00:00:00Z"
    path = root / case.log_files["customEvents"]
    events = json.loads(path.read_text())
    extra = json.loads(json.dumps(events[2:4]))
    for row in extra:
        row["itemId"] += "-second"
        row["customDimensions"]["CallId"] = "second-doc"
    extra[0]["customDimensions"]["ToolInput"] = '{"url":"https://docs.example.test/other"}'
    events += extra
    case.expected_rows = {}
    write_json(path, events)
    replay_path = root / case.replay_path
    replay = json.loads(replay_path.read_text())
    replay["claims:gpt"]["output"]["bindings"][0]["call_ids"].append("second-doc")
    write_json(replay_path, replay)
    result = evaluate_case(case, root, config, tmp_path / "cache")
    assert result.status == "SCORED"
    assert len(result.documents) == 2
    assert result.documents[1].status == "UNRESOLVED"
    assert result.steps[0].freshness == 0
    assert result.score == 90


def test_oversized_model_integer_is_an_error_not_batch_crash(corpus, tmp_path):
    root, manifest, config = corpus
    case = manifest.cases[0]
    path = root / case.replay_path
    replay = json.loads(path.read_text())
    replay["step:step-1:gpt"]["output"]["faithfulness"] = 10**400
    write_json(path, replay)
    result = evaluate_case(case, root, config, tmp_path / "cache")
    assert result.status == "JUDGE_ERROR"
    assert result.score is None


def test_malformed_table_is_import_error_not_batch_crash(corpus, tmp_path):
    root, manifest, config = corpus
    case = manifest.cases[0]
    write_json(root / case.log_files["customEvents"], {"tables": [{"rows": []}]})
    result = evaluate_case(case, root, config, tmp_path / "cache")
    assert result.status == "IMPORT_ERROR"
