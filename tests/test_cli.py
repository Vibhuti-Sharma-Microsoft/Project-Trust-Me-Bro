from scoring_service.cli import main
from scoring_service.cli import parser
from scoring_service.models import BatchResult


def test_cli_evaluates_and_renders_without_network(corpus, tmp_path, capsys):
    root, _, _ = corpus
    out = tmp_path / "out"
    result = main(["evaluate", "--manifest", str(root / "cases.json"), "--config", str(root / "evaluation.json"),
                   "--judge-mode", "replay", "--out", str(out), "--run-id", "test",
                   "--case", "synthetic-supported"])
    assert result == 0
    output = capsys.readouterr().out
    assert output.count("Evaluation report:") == 1
    assert "Detailed runtime log:" not in output and "Results:" not in output
    batch = BatchResult.model_validate_json((out / "test" / "results.json").read_text())
    assert batch.results[0].score == 100
    assert batch.selected_real_cases == 0
    assert (out / "test" / "index.html").exists()
    assert main(["render", "--results", str(out / "test" / "results.json")]) == 0
    assert main(["evaluate", "--manifest", str(root / "cases.json"), "--judge-mode", "replay",
                 "--out", str(out), "--run-id", "test"]) == 2


def test_unknown_case_is_explicit_error(corpus):
    root, _, _ = corpus
    assert main(["evaluate", "--manifest", str(root / "cases.json"), "--case", "missing"]) == 2


def test_validation_does_not_need_judge_configuration(corpus):
    root, _, _ = corpus
    assert main(["validate", "--manifest", str(root / "cases.json")]) == 0


def test_cli_accepts_copilot_judge_mode():
    args = parser().parse_args(["evaluate", "--manifest", "cases.json", "--judge-mode", "copilot"])
    assert args.judge_mode == "copilot"


def test_cli_defaults_to_real_copilot_pilot():
    args = parser().parse_args(["evaluate"])
    assert args.manifest.as_posix() == "data/real-pilot/cases.json"
    assert args.config.as_posix() == "config/evaluation.local.json"
    assert args.judge_mode == "copilot"
