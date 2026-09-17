import csv
import json
from pathlib import Path

import pytest

from sre_assurance.imports import ImportFailure, load_table, safe_path, write_json
from sre_assurance.models import StepJudgment
from sre_assurance.time_utils import timestamp_ns


@pytest.mark.parametrize("value", [True, "0.5", 0.75, -1, float("nan"), float("inf"), 10**400])
def test_tri_score_rejects_invalid(value):
    with pytest.raises(ValueError):
        StepJudgment(faithfulness=value, rationale="x")


@pytest.mark.parametrize("value", [0, 0.5, 1])
def test_tri_score_accepts_exact_values(value):
    assert StepJudgment(faithfulness=value, rationale="x").faithfulness == value


def test_nanosecond_cutoff_and_timezone():
    cutoff = timestamp_ns("2026-09-16T01:03:00.2451908Z")
    assert cutoff - timestamp_ns("2026-09-16T01:03:00.2451907Z") == 100
    assert timestamp_ns("2026-09-16T06:33:00.2451908+05:30") == cutoff
    with pytest.raises(ValueError):
        timestamp_ns("2026-09-16T01:03:00")


@pytest.mark.parametrize("relative", ["../outside.json", "..\\outside.json", "C:\\secrets.json", "/etc/passwd"])
def test_paths_cannot_escape(tmp_path, relative):
    with pytest.raises(ImportFailure):
        safe_path(tmp_path, relative)


def test_csv_bom_and_json_cell(tmp_path: Path):
    path = tmp_path / "events.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["timestamp", "itemId", "customDimensions"])
        writer.writeheader()
        writer.writerow({"timestamp": "2026-09-16T00:00:00Z", "itemId": "one", "customDimensions": '{"Message":"a,b\\nquoted"}'})
    rows = load_table(path, "customEvents")
    assert rows[0]["customDimensions"]["Message"] == "a,b\nquoted"


def test_query_json_shape_and_jsonl(tmp_path: Path):
    path = tmp_path / "events.json"
    write_json(path, {"tables": [{"name": "PrimaryResult", "columns": [{"name": "timestamp"}, {"name": "customDimensions"}],
                                  "rows": [["2026-09-16T00:00:00Z", {"EventType": "ToolStart"}]]}]})
    assert load_table(path, "customEvents")[0]["customDimensions"]["EventType"] == "ToolStart"
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({"timestamp": "2026-09-16T00:00:00Z"}) + "\n", encoding="utf-8")
    assert len(load_table(path, "traces")) == 1


def test_aggregate_inventory_is_not_raw_logs(tmp_path: Path):
    path = tmp_path / "inventory.json"
    write_json(path, [{"EventName": "AgentToolExecution", "DimensionKey": "CallId", "ExampleValue": "abc"}])
    with pytest.raises(ImportFailure, match="aggregate inventories"):
        load_table(path, "customEvents")


def test_malformed_dimension_is_explicit_error(tmp_path: Path):
    path = tmp_path / "events.json"
    write_json(path, [{"timestamp": "2026-09-16T00:00:00Z", "customDimensions": '{"broken":'}])
    with pytest.raises(ImportFailure, match="not valid JSON"):
        load_table(path, "customEvents")


@pytest.mark.parametrize("table", [
    {"rows": []}, {"columns": [], "rows": None}, {"columns": [1], "rows": []},
    {"columns": [{"name": "x"}, {"name": "x"}], "rows": []},
    {"columns": [{"name": "timestamp"}], "rows": ["not a row"]},
])
def test_malformed_query_envelopes_fail_explicitly(tmp_path, table):
    path = tmp_path / "bad.json"
    write_json(path, {"tables": [table]})
    with pytest.raises(ImportFailure):
        load_table(path, "customEvents")


def test_extra_csv_cells_fail_explicitly(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("timestamp,customDimensions\n2026-09-16T00:00:00Z,{},unexpected\n", encoding="utf-8")
    with pytest.raises(ImportFailure, match="column layout"):
        load_table(path, "customEvents")


def test_duplicate_real_incidents_are_rejected(corpus):
    from sre_assurance.models import CorpusManifest
    _, manifest, _ = corpus
    a = manifest.cases[0].model_copy(update={"synthetic": False})
    b = a.model_copy(update={"id": "second-case", "message_id": "other-message"})
    with pytest.raises(ValueError, match="one first diagnostic"):
        CorpusManifest(cases=[a, b])
