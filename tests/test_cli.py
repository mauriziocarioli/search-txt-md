from __future__ import annotations

from pathlib import Path

import pytest

from search_txt_md.cli import index_main, search_main, tag_main


def test_index_and_search(corpus: Path, index_path: Path, capsys) -> None:
    rc = index_main(["--root", str(corpus), "--index", str(index_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Walked 8" in out
    rc = search_main(
        ["--root", str(corpus), "--index", str(index_path), "grusch"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Grusch-hearing.txt" in out


def test_missing_index_exit_2(tmp_path: Path) -> None:
    rc = search_main(["--index", str(tmp_path / "nope.sqlite"), "grusch"])
    assert rc == 2


def test_empty_query_exit_1(index_path: Path) -> None:
    rc = search_main(["--index", str(index_path)])
    assert rc == 1


def test_minus_as_flag_fails(corpus: Path, index_path: Path) -> None:
    index_main(["--root", str(corpus), "--index", str(index_path)])
    with pytest.raises(SystemExit) as ei:
        search_main(["--index", str(index_path), "uap", "-hoax"])
    assert ei.value.code != 0


def test_minus_after_end_of_options(corpus: Path, index_path: Path, capsys) -> None:
    index_main(["--root", str(corpus), "--index", str(index_path)])
    rc = search_main(["--index", str(index_path), "uap", "--", "-hoax"])
    assert rc == 0
    capsys.readouterr()


def test_quoted_minus_query(corpus: Path, index_path: Path) -> None:
    index_main(["--root", str(corpus), "--index", str(index_path)])
    rc = search_main(["--index", str(index_path), "uap -hoax"])
    assert rc == 0


def test_tag_cli_and_search(corpus: Path, index_path: Path, capsys) -> None:
    assert index_main(["--root", str(corpus), "--index", str(index_path)]) == 0
    capsys.readouterr()
    rc = tag_main(
        ["--index", str(index_path), "--root", str(corpus), "create", "ufo"]
    )
    assert rc == 0
    assert "created ufo" in capsys.readouterr().out
    rc = tag_main(
        [
            "--index",
            str(index_path),
            "--root",
            str(corpus),
            "apply",
            "ufo",
            "--under",
            "UFO",
        ]
    )
    assert rc == 0
    rc = search_main(["--index", str(index_path), "--tag", "ufo", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Grusch-hearing.txt" in out
    assert '"tags"' in out
    rc = search_main(["--index", str(index_path), "tag:ufo", "grusch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Grusch-hearing" in out
    rc = search_main(["--index", str(index_path), "--tag", "nope"])
    assert rc == 1


def test_empty_query_with_tag(corpus: Path, index_path: Path, capsys) -> None:
    index_main(["--root", str(corpus), "--index", str(index_path)])
    tag_main(["--index", str(index_path), "create", "ufo"])
    tag_main(
        ["--index", str(index_path), "apply", "ufo", "UFO/Grusch-hearing.txt"]
    )
    capsys.readouterr()
    rc = search_main(["--index", str(index_path), "--tag", "ufo"])
    assert rc == 0
    assert "Grusch-hearing.txt" in capsys.readouterr().out


def test_json_flag(corpus: Path, index_path: Path, capsys) -> None:
    index_main(["--root", str(corpus), "--index", str(index_path)])
    rc = search_main(["--index", str(index_path), "--json", "grusch"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"hits"' in out
    assert "Grusch" in out
