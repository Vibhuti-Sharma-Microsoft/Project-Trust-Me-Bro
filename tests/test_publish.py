import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("publish_check", Path(__file__).resolve().parents[1] / "tools" / "check_publish.py")
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


def _git(root, *args):
    process = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)
    assert process.returncode == 0, process.stderr


@pytest.fixture
def repository(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / ".gitignore").write_text("data/\nout/\n.env\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "incident-roster.local.json").write_text(
        json.dumps({"incident_ids": ["9100000001"]}), encoding="utf-8")
    (tmp_path / "example.py").write_text("print('synthetic example')\n", encoding="utf-8")
    return tmp_path


def test_ignored_private_data_is_not_publishable(repository):
    assert CHECKER.check_publish(repository) == []


def test_force_staged_private_path_is_rejected(repository):
    _git(repository, "add", "-f", "data/incident-roster.local.json")
    assert any("Private/generated path" in item for item in CHECKER.check_publish(repository))


def test_staged_sensitive_content_is_checked_even_if_worktree_is_clean(repository):
    (repository / "example.py").write_text("incident = '9100000001'\n", encoding="utf-8")
    _git(repository, "add", "example.py")
    (repository / "example.py").write_text("incident = 'placeholder'\n", encoding="utf-8")
    assert any("index: example.py" in item for item in CHECKER.check_publish(repository))


def test_roster_id_in_untracked_source_is_rejected(repository):
    (repository / "example.py").write_text("incident = '9100000001'\n", encoding="utf-8")
    assert any("working tree: example.py" in item for item in CHECKER.check_publish(repository))
