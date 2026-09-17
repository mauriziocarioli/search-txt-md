from __future__ import annotations

from pathlib import Path

import pytest

from search_txt_md.config import Settings
from search_txt_md.index import connect, incremental_index, init_schema, meta_get
from search_txt_md.tags import (
    TagError,
    apply_tags,
    create_tags,
    delete_tags,
    list_tags,
    remove_tags,
    show_tags,
    validate_tag_name,
)


def _build(corpus: Path, index_path: Path):
    settings = Settings(root=corpus, index=index_path)
    settings.ensure_index_parent()
    conn = connect(index_path, writable=True)
    init_schema(conn)
    incremental_index(conn, settings, full=False, verbose=False, dry_run=False)
    return conn, settings


def test_validate_tag_name() -> None:
    assert validate_tag_name("ufo") == "ufo"
    assert validate_tag_name("whistle-blower") == "whistle-blower"
    assert validate_tag_name("a_b") == "a_b"
    with pytest.raises(TagError):
        validate_tag_name("")
    with pytest.raises(TagError):
        validate_tag_name("-ufo")
    with pytest.raises(TagError):
        validate_tag_name("crash retrieval")
    with pytest.raises(TagError):
        validate_tag_name("a:b")
    with pytest.raises(TagError):
        validate_tag_name("x" * 65)


def test_create_list_delete(corpus: Path, index_path: Path) -> None:
    conn, _ = _build(corpus, index_path)
    try:
        assert create_tags(conn, ["ufo", "physics"]) == [
            ("ufo", "created"),
            ("physics", "created"),
        ]
        assert create_tags(conn, ["UFO"]) == [("ufo", "exists")]
        rows = list_tags(conn)
        assert rows == [("physics", 0), ("ufo", 0)]
        assert delete_tags(conn, ["physics"]) == [("physics", "deleted")]
        assert delete_tags(conn, ["nope"]) == [("nope", "missing")]
        names = [n for n, _c in list_tags(conn)]
        assert names == ["ufo"]
    finally:
        conn.close()


def test_apply_remove_show(corpus: Path, index_path: Path) -> None:
    conn, _ = _build(corpus, index_path)
    try:
        create_tags(conn, ["ufo"])
        path = "UFO/Grusch-hearing.txt"
        assert apply_tags(conn, "ufo", [path]) == [(path, "tagged")]
        assert apply_tags(conn, "UFO", [path]) == [(path, "already")]
        resolved, names = show_tags(conn, path)
        assert resolved == path
        assert names == ["ufo"]
        assert remove_tags(conn, "ufo", [path]) == [(path, "untagged")]
        assert remove_tags(conn, "ufo", [path]) == [(path, "absent")]
        assert show_tags(conn, path)[1] == []
    finally:
        conn.close()


def test_apply_missing_tag_and_path(corpus: Path, index_path: Path) -> None:
    conn, _ = _build(corpus, index_path)
    try:
        with pytest.raises(TagError, match="tag not found"):
            apply_tags(conn, "ufo", ["UFO/Grusch-hearing.txt"])
        create_tags(conn, ["ufo"])
        with pytest.raises(TagError, match="not in index"):
            apply_tags(conn, "ufo", ["nope.txt"])
    finally:
        conn.close()


def test_apply_under(corpus: Path, index_path: Path) -> None:
    conn, _ = _build(corpus, index_path)
    try:
        create_tags(conn, ["ufo"])
        changes = apply_tags(conn, "ufo", [], under="UFO")
        paths = {p for p, action in changes}
        actions = {action for _p, action in changes}
        assert actions == {"tagged"}
        assert "UFO/Grusch-hearing.txt" in paths
        assert "UFO/Grusch-hearing.md" in paths
        assert "UFO/Elizondo-interview.txt" in paths
        assert len(paths) == 3
        counts = dict(list_tags(conn))
        assert counts["ufo"] == 3
    finally:
        conn.close()


def test_delete_cascades_assignments(corpus: Path, index_path: Path) -> None:
    conn, _ = _build(corpus, index_path)
    try:
        create_tags(conn, ["ufo"])
        apply_tags(conn, "ufo", ["UFO/Grusch-hearing.txt"])
        delete_tags(conn, ["ufo"])
        n = conn.execute("SELECT count(*) FROM document_tags").fetchone()[0]
        assert n == 0
    finally:
        conn.close()


def test_absolute_path(corpus: Path, index_path: Path) -> None:
    conn, settings = _build(corpus, index_path)
    try:
        create_tags(conn, ["ufo"])
        abs_path = str(settings.root / "UFO" / "Grusch-hearing.txt")
        changes = apply_tags(conn, "ufo", [abs_path], corpus_root=settings.root)
        assert changes == [("UFO/Grusch-hearing.txt", "tagged")]
    finally:
        conn.close()


def test_schema_has_tag_tables(corpus: Path, index_path: Path) -> None:
    conn, _ = _build(corpus, index_path)
    try:
        tables = {
            n
            for (n,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "tags" in tables
        assert "document_tags" in tables
        assert meta_get(conn, "schema_version") == "2"
    finally:
        conn.close()
