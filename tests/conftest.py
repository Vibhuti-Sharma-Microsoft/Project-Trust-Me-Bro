from pathlib import Path

import pytest

from sre_assurance.config import load_config
from sre_assurance.demo import create_demo
from sre_assurance.imports import load_manifest


@pytest.fixture
def corpus(tmp_path: Path):
    root = tmp_path / "corpus"
    manifest_path = create_demo(root)
    return root, load_manifest(manifest_path), load_config(root / "evaluation.json")
