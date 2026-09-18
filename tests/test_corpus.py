import json

import pytest

from scoring_service.cli import main
from scoring_service.corpus import IncidentRoster, prepare_corpus
from scoring_service.imports import write_json
from scoring_service.models import CorpusManifest


def test_prepares_exact_roster_without_fabricating_evidence(tmp_path):
    ids = [str(9_100_000_000 + i) for i in range(10)]
    roster = tmp_path / "roster.json"
    write_json(roster, {"incident_ids": ids})
    path = prepare_corpus(roster, tmp_path / "pilot")
    plan = json.loads(path.read_text())
    assert plan["ready_for_evaluation"] is False
    assert plan["target_real_cases"] == 10
    assert [case["incident_id"] for case in plan["cases"]] == ids
    assert all(case["known_response"] is None for case in plan["cases"])
    assert not list(path.parent.rglob("response.html"))
    assert not list(path.parent.rglob("customEvents.json"))
    for query in (path.parent / "queries").glob("*.kql"):
        assert all(f'"{incident_id}"' in query.read_text() for incident_id in ids)
    with pytest.raises(ValueError):
        CorpusManifest.model_validate(plan)
    with pytest.raises(FileExistsError):
        prepare_corpus(roster, path.parent)


@pytest.mark.parametrize("ids", [[], ["1", "1"], ["1'; drop"], ["0"], ["-1"], [123], ["abc"]])
def test_roster_rejects_invalid_ids(ids):
    with pytest.raises(ValueError):
        IncidentRoster(incident_ids=ids)


def test_preserves_known_mapping_as_metadata_only(tmp_path):
    roster = tmp_path / "roster.json"
    mapping = {"incident_id": "9000000001", "message_id": "9000000002", "thread_id": "test-thread",
               "post_call_id": "test-call", "cutoff": "2026-01-01T00:00:00.0000001Z",
               "mapping_provenance": "Synthetic mapping fixture"}
    write_json(roster, {"incident_ids": ["9000000001"], "known_responses": [mapping]})
    assert main(["prepare-corpus", "--incidents-file", str(roster), "--directory", str(tmp_path / "pilot")]) == 0
    plan = json.loads((tmp_path / "pilot" / "collection-plan.json").read_text())
    assert plan["cases"][0]["known_response"] == mapping
    assert plan["cases"][0]["state"] == "AWAITING_EXPORTS"


def test_unknown_mapping_and_invalid_timestamp_are_rejected():
    mapping = {"incident_id": "2", "message_id": "3", "thread_id": "t", "post_call_id": "c",
               "cutoff": "2026-01-01T00:00:00Z", "mapping_provenance": "test"}
    with pytest.raises(ValueError):
        IncidentRoster(incident_ids=["1"], known_responses=[mapping])
    mapping["incident_id"] = "1"
    mapping["cutoff"] = "not a timestamp"
    with pytest.raises(ValueError):
        IncidentRoster(incident_ids=["1"], known_responses=[mapping])
