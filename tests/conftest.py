from pathlib import Path

import pytest

from scoring_service.config import load_config
from scoring_service.demo import create_demo
from scoring_service.imports import load_manifest


@pytest.fixture
def corpus(tmp_path: Path):
    root = tmp_path / "corpus"
    manifest_path = create_demo(root)
    return root, load_manifest(manifest_path), load_config(root / "evaluation.json")
