import json

import pytest

from scoring_service.bundle import build_bundle, payload_flags
from scoring_service.imports import ImportFailure, write_json


def _events(root, case):
    path = root / case.log_files["customEvents"]
    return path, json.loads(path.read_text(encoding="utf-8"))


def test_complete_bundle_identifies_initial_todo(corpus):
    root, manifest, _ = corpus
    bundle = build_bundle(manifest.cases[0], root)
    assert bundle.todo.source_call_id == "todo"
    assert bundle.todo.steps[0].id == "step-1"
    assert len(bundle.calls) == 3
    assert not any(item.call_id == bundle.case.post_call_id for item in bundle.evidence)


def test_missing_initial_todo_is_not_replaced_by_later_update(corpus):
    root, manifest, _ = corpus
    case = manifest.cases[0]
    path, events = _events(root, case)
    events[0]["customDimensions"]["ToolInput"] = '{"todos":'
    later = json.loads(json.dumps(events[:2]))
    for row in later:
        row["itemId"] += "-later"
        row["customDimensions"]["CallId"] = "todo-later"
        row["timestamp"] = row["timestamp"].replace("00:50:", "00:52:")
    later[0]["customDimensions"]["ToolInput"] = '{"todos":[{"id":1,"title":"Better plan","status":"not-started"}]}'
    events += later
    case.expected_rows = {}
    write_json(path, events)
    assert build_bundle(case, root).todo is None


def test_future_tool_result_is_excluded_at_submicrosecond_boundary(corpus):
    root, manifest, _ = corpus
    case = manifest.cases[0]
    path, events = _events(root, case)
    events[3]["timestamp"] = "2026-09-16T01:03:00.2451909Z"
    write_json(path, events)
    bundle = build_bundle(case, root)
    call = next(call for call in bundle.calls if call.id.startswith("demo-read"))
    assert call.status == "ORPHAN"
    assert call.output_raw == ""
    assert not next(item for item in bundle.evidence if item.call_id == call.id).eligible


def test_identical_records_deduplicate_but_conflicting_ids_fail(corpus):
    root, manifest, _ = corpus
    case = manifest.cases[0]
    case.expected_rows = {}
    path, events = _events(root, case)
    events.append(json.loads(json.dumps(events[0])))
    write_json(path, events)
    assert len(build_bundle(case, root).calls) == 3
    events[-1]["customDimensions"]["ToolName"] = "ConflictingTool"
    write_json(path, events)
    with pytest.raises(ImportFailure, match="Conflicting copies"):
        build_bundle(case, root)


def test_response_version_and_mapping_validation(corpus):
    root, manifest, _ = corpus
    case = manifest.cases[0]
    case.mapping_verified = False
    with pytest.raises(ImportFailure, match="not been verified"):
        build_bundle(case, root)
    case.mapping_verified = True
    (root / case.response_path).write_text("Changed response", encoding="utf-8")
    with pytest.raises(ImportFailure, match="hash"):
        build_bundle(case, root)


def test_linked_genai_payload_without_trace_is_diagnostic_not_independent(corpus):
    root, manifest, _ = corpus
    case = manifest.cases[0]
    dependency = {"timestamp": "2026-09-16T00:51:00Z", "itemId": "dep",
                  "operation_Id": "demo-trace-1", "customDimensions": {"_MS.GenAIContentId": "payload"}}
    content = {"timestamp": "2026-09-16T00:51:00Z", "itemId": "gen",
               "outputMessages": "Duplicate model answer", "customDimensions": {"_MS.GenAIContentId": "payload"}}
    write_json(root / "deps.json", [dependency])
    write_json(root / "gen.json", [content])
    case.log_files.update({"dependencies": "deps.json", "genAIContent": "gen.json"})
    bundle = build_bundle(case, root)
    item = next(item for item in bundle.evidence if "genAIContent" in item.id)
    assert item.content == "Duplicate model answer"
    assert item.eligible is False


def test_quality_indicators_are_per_payload_not_per_call():
    assert "REDACTED" in payload_flags('{"name":"<redacted:unscannable>","region":"eastus"}')
    assert "REDACTED" in payload_flags(r'{"name":"\u003Credacted-phone\u003E"}')
    assert "PREVIEW" in payload_flags('{"contentPreview":"short","contentLength":100}')
    assert "POSSIBLY_SHORTENED" in payload_flags('"..."')
    assert "SIZE_BOUNDARY_INDICATOR" in payload_flags("x" * 8192)
    assert payload_flags('"ZERO_ROWS_RETURNED"') == []


def test_replay_transport_path_is_not_part_of_evidence_identity(corpus):
    root, manifest, _ = corpus
    case = manifest.cases[0]
    first = build_bundle(case, root)
    case.replay_path = None
    live = build_bundle(case, root)
    assert live.data_sha256 == first.data_sha256
    case.replay_path = "another-recording.json"
    assert build_bundle(case, root).data_sha256 == first.data_sha256


def test_posting_body_must_match_selected_response(corpus):
    root, manifest, _ = corpus
    case = manifest.cases[0]
    path, events = _events(root, case)
    events[-2]["customDimensions"]["ToolInput"] = json.dumps(
        {"incidentId": int(case.incident_id), "discussionEntry": "The service is in westus."})
    write_json(path, events)
    with pytest.raises(ImportFailure, match="discussionEntry"):
        build_bundle(case, root)


def test_post_body_normalization_preserves_query_attachment():
    from scoring_service.bundle import _validate_post_body, response_text
    body = "## Findings\n\n| Field | Value |\n|---|---|\n| Region | eastus |"
    query = "T | where x > 1"
    rendered = "<pre>T | where x &gt; 1</pre><h2>Findings</h2><table><tr><th>Field</th><th>Value</th></tr><tr><td>Region</td><td>eastus</td></tr></table>"
    _validate_post_body({"discussionEntry": body, "kustoQuery": query}, response_text(rendered, True), [])
    with pytest.raises(ImportFailure, match="Kusto attachment"):
        _validate_post_body({"discussionEntry": "Findings", "kustoQuery": query}, "Findings", [])
