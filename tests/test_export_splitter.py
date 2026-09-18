import csv

import pytest

from scoring_service.export_splitter import split_exports


def write_export(path, rows, timestamp_field="timestamp"):
    fields = ["IncidentId", "SourceTable", timestamp_field, "operation_Id", "customDimensions"]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_split_exports_merges_slices_and_removes_routing_columns(tmp_path):
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    write_export(first, [
        {"IncidentId": "123", "SourceTable": "customEvents", "timestamp": "2026-01-01T00:00:00Z",
         "operation_Id": "a", "customDimensions": "{}"},
        {"IncidentId": "123", "SourceTable": "dependencies", "timestamp": "2026-01-01T00:01:00Z",
         "operation_Id": "a", "customDimensions": "{}"},
    ])
    write_export(second, [
        {"IncidentId": "123", "SourceTable": "customEvents", "timestamp": "2026-01-02T00:00:00Z",
         "operation_Id": "b", "customDimensions": "{}"},
    ])
    out = tmp_path / "out"
    assert split_exports([first, second], "123", out) == {"customEvents": 2, "dependencies": 1}
    rows = list(csv.DictReader((out / "customEvents.csv").open(encoding="utf-8")))
    assert [row["operation_Id"] for row in rows] == ["a", "b"]
    assert "IncidentId" not in rows[0]
    assert "SourceTable" not in rows[0]


def test_split_exports_rejects_wrong_incident_and_overwrite(tmp_path):
    source = tmp_path / "export.csv"
    write_export(source, [
        {"IncidentId": "other", "SourceTable": "customEvents", "timestamp": "2026-01-01T00:00:00Z",
         "operation_Id": "a", "customDimensions": "{}"},
    ])
    with pytest.raises(ValueError, match="incident ID"):
        split_exports([source], "123", tmp_path / "out")

    write_export(source, [
        {"IncidentId": "123", "SourceTable": "customEvents", "timestamp": "2026-01-01T00:00:00Z",
         "operation_Id": "a", "customDimensions": "{}"},
    ])
    out = tmp_path / "out"
    split_exports([source], "123", out)
    with pytest.raises(FileExistsError):
        split_exports([source], "123", out)


def test_split_exports_normalizes_portal_utc_timestamp(tmp_path):
    source = tmp_path / "export.csv"
    write_export(source, [
        {"IncidentId": "123", "SourceTable": "customEvents",
         "timestamp [UTC]": "9/16/2026, 1:03:00.245 AM",
         "operation_Id": "a", "customDimensions": "{}"},
    ], timestamp_field="timestamp [UTC]")
    out = tmp_path / "out"
    split_exports([source], "123", out)
    row = next(csv.DictReader((out / "customEvents.csv").open(encoding="utf-8")))
    assert row["timestamp"] == "2026-09-16T01:03:00.245000Z"
