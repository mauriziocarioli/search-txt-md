from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

os.environ["TXTMD_TEST"] = "1"
os.environ.pop("TXTMD_ROOT", None)
os.environ.pop("TXTMD_INDEX", None)

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "corpus"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    dest = tmp_path / "corpus"
    shutil.copytree(FIXTURE_CORPUS, dest)
    return dest


@pytest.fixture
def index_path(tmp_path: Path) -> Path:
    return tmp_path / "index.sqlite"
