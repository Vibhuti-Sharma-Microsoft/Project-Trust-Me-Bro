import json
from unittest.mock import patch

from scoring_service.cli import main
from scoring_service.models import BatchResult
from scoring_service.runtime_log import RuntimeLogError


def test_evaluate_records_analysis_outside_the_scorecard(corpus, tmp_path):
    root, _, _ = corpus
    out = tmp_path / "out"
    result = main([
        "evaluate", "--manifest", str(root / "cases.json"), "--config", str(root / "evaluation.json"),
        "--judge-mode", "replay", "--out", str(out), "--run-id", "logged",
        "--case", "synthetic-supported", "--case", "synthetic-gate-failed", "--case", "synthetic-judge-error",
    ])
    assert result == 1
    folder = out / "logged"
    records = [json.loads(line) for line in (folder / "scoring-service.log").read_text(encoding="utf-8").splitlines()]
    assert records[0]["event"] == "run.started"
    assert records[-1]["event"] == "run.completed"
    assert [record["sequence"] for record in records] == list(range(1, len(records) + 1))
    supported = [record for record in records if record["case_id"] == "synthetic-supported"]
    assert len([record for record in supported if record["event"] == "judge.returned"]) == 7
    scored = next(record for record in supported if record["event"] == "step.scored")["details"]
    assert scored["calculations"]["faithfulness"]["operator"] == "median"
    assert scored["calculations"]["weighted_terms"] == {"faithfulness": 35, "coverage": 35, "source_trust": 20, "freshness": 10}
    assert scored["calculations"]["step_score"] == 100
    assert scored["calculations"]["coverage"]["verdicts"][0]["references"]
    failed_gate = [record for record in records if record["case_id"] == "synthetic-gate-failed"]
    assert not any(record["event"] in {"claims.validated", "documents.prepared", "step.scored"} for record in failed_gate)
    assert next(record for record in failed_gate if record["event"] == "case.finished")["details"]["score"] == 0
    failed_judge = [record for record in records if record["case_id"] == "synthetic-judge-error"]
    assert any(record["event"] == "judge.failed" and record["details"]["role"] == "gemini" for record in failed_judge)
    assert next(record for record in failed_judge if record["event"] == "case.finished")["details"]["score"] is None
    batch = BatchResult.model_validate_json((folder / "results.json").read_text())
    assert batch.runtime_log_file == "scoring-service.log"
    assert (folder / "index.html").exists()


def test_render_does_not_invent_a_historical_runtime_log(corpus, tmp_path):
    root, _, _ = corpus
    out = tmp_path / "out"
    assert main(["evaluate", "--manifest", str(root / "cases.json"), "--config", str(root / "evaluation.json"),
                 "--judge-mode", "replay", "--out", str(out), "--run-id", "original",
                 "--case", "synthetic-supported"]) == 0
    source = out / "original" / "results.json"
    batch = BatchResult.model_validate_json(source.read_text())
    batch.runtime_log_file = None
    old = tmp_path / "old-results.json"
    old.write_text(batch.model_dump_json(), encoding="utf-8")
    rendered = tmp_path / "rendered"
    assert main(["render", "--results", str(old), "--out", str(rendered)]) == 0
    assert not (rendered / "scoring-service.log").exists()


def test_log_open_failure_prevents_unlogged_evaluation(corpus, tmp_path):
    root, _, _ = corpus
    with patch("scoring_service.cli.RuntimeJournal", side_effect=RuntimeLogError("Cannot create log")), \
            patch("scoring_service.cli.evaluate_case") as evaluate:
        assert main(["evaluate", "--manifest", str(root / "cases.json"), "--config", str(root / "evaluation.json"),
                     "--out", str(tmp_path / "out"), "--run-id", "no-log"]) == 2
        evaluate.assert_not_called()


def test_import_failure_is_preserved_in_runtime_log(corpus, tmp_path):
    root, manifest, _ = corpus
    case = manifest.cases[0]
    (root / case.response_path).write_text("Different from the verified response", encoding="utf-8")
    out = tmp_path / "out"
    assert main(["evaluate", "--manifest", str(root / "cases.json"), "--config", str(root / "evaluation.json"),
                 "--case", case.id, "--out", str(out), "--run-id", "bad-import"]) == 1
    records = [json.loads(line) for line in (out / "bad-import" / "scoring-service.log").read_text().splitlines()]
    assert any(record["event"] == "import.failed" for record in records)
    assert not any(record["event"] == "judge.started" for record in records)
    finished = next(record for record in records if record["event"] == "case.finished")
    assert finished["details"]["status"] == "IMPORT_ERROR"
