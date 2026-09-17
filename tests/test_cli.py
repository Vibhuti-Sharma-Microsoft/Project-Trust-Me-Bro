from sre_assurance.cli import main
from sre_assurance.models import BatchResult


def test_cli_evaluates_and_renders_without_network(corpus, tmp_path):
    root, _, _ = corpus
    out = tmp_path / "out"
    result = main(["evaluate", "--manifest", str(root / "cases.json"), "--config", str(root / "evaluation.json"),
                   "--out", str(out), "--run-id", "test", "--case", "synthetic-supported"])
    assert result == 0
    batch = BatchResult.model_validate_json((out / "test" / "results.json").read_text())
    assert batch.results[0].score == 100
    assert batch.selected_real_cases == 0
    assert (out / "test" / "index.html").exists()
    assert main(["render", "--results", str(out / "test" / "results.json")]) == 0
    assert main(["evaluate", "--manifest", str(root / "cases.json"), "--out", str(out), "--run-id", "test"]) == 2


def test_unknown_case_is_explicit_error(corpus):
    root, _, _ = corpus
    assert main(["evaluate", "--manifest", str(root / "cases.json"), "--case", "missing"]) == 2


def test_validation_does_not_need_judge_configuration(corpus):
    root, _, _ = corpus
    assert main(["validate", "--manifest", str(root / "cases.json")]) == 0
