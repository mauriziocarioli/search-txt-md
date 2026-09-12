from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from search_txt_md.config import DEFAULT_ROOT
from search_txt_md.crawl import (
    classify_name,
    filename_stem,
    is_dataless_placeholder,
    iter_corpus,
)


def test_classify_trailing_space() -> None:
    assert classify_name("foo.txt ") == ("txt", "trailing whitespace in filename")


def test_classify_missing_dot() -> None:
    assert classify_name("20250906txt") == ("txt", "missing-dot extension")


def test_classify_skips_ds_store() -> None:
    assert classify_name(".DS_Store") is None
    assert classify_name(".DS Store") is None
    assert classify_name("skip-me.pdf") is None


def test_filename_stem_trailing_and_missing() -> None:
    assert filename_stem("foo.txt ", "txt") == "foo"
    assert filename_stem("20250906txt", "txt") == "20250906"
    assert filename_stem("Grusch-hearing.md", "md") == "Grusch-hearing"


def test_iter_corpus_synthesized(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("a", encoding="utf-8")
    (tmp_path / ".DS_Store").write_bytes(b"x")
    (tmp_path / "trailing.txt ").write_text("t", encoding="utf-8")
    (tmp_path / "20250906txt").write_text("m", encoding="utf-8")
    (tmp_path / "skip-me.pdf").write_text("p", encoding="utf-8")
    items = {i.relpath: i for i in iter_corpus(tmp_path)}
    assert "ok.txt" in items
    assert "trailing.txt " in items
    assert items["trailing.txt "].ext == "txt"
    assert "20250906txt" in items
    assert items["20250906txt"].ext == "txt"
    assert ".DS_Store" not in items
    assert "skip-me.pdf" not in items


def test_dataless_does_not_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "cloud.txt"
    target.write_text("secret", encoding="utf-8")
    opened: list[str] = []
    real_open = Path.read_bytes

    def wrap(self: Path, *a, **k):
        opened.append(str(self))
        return real_open(self, *a, **k)

    monkeypatch.setattr(
        "search_txt_md.crawl.is_dataless_placeholder", lambda st: True
    )
    items = list(iter_corpus(tmp_path))
    assert items and items[0].dataless is True
    monkeypatch.setattr(Path, "read_bytes", wrap)
    # crawl itself must not open
    assert opened == []


def test_is_dataless_heuristic() -> None:
    st = SimpleNamespace(st_size=10, st_blocks=0)
    assert is_dataless_placeholder(st) is True
    st2 = SimpleNamespace(st_size=10, st_blocks=8)
    assert is_dataless_placeholder(st2) is False


def test_guard_refuses_default_root() -> None:
    os.environ["TXTMD_TEST"] = "1"
    with pytest.raises(RuntimeError, match="must not walk"):
        list(iter_corpus(DEFAULT_ROOT))
