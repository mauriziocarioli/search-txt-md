from __future__ import annotations

from pathlib import Path

from search_txt_md.config import Settings
from search_txt_md.index import connect, incremental_index, init_schema
from search_txt_md.query import parse_query
from search_txt_md.search import escape_like, normalize_under, search


def _build(corpus: Path, index_path: Path):
    settings = Settings(root=corpus, index=index_path)
    settings.ensure_index_parent()
    conn = connect(index_path, writable=True)
    init_schema(conn)
    incremental_index(conn, settings, full=False, verbose=False, dry_run=False)
    return conn, settings


def _hits(conn, q: str, **kwargs):
    fts5 = parse_query(q).to_fts5()
    return search(conn, fts5, limit=kwargs.pop("limit", 20), ext=kwargs.get("ext"),
                  under=kwargs.get("under"), corpus_root=kwargs.get("corpus_root"),
                  snippets=kwargs.get("snippets", True))


def test_grusch_hits_txt_and_md(corpus: Path, index_path: Path) -> None:
    conn, settings = _build(corpus, index_path)
    try:
        hits = _hits(conn, "grusch", corpus_root=settings.root)
        paths = {h.path for h in hits}
        assert "UFO/Grusch-hearing.txt" in paths
        assert "UFO/Grusch-hearing.md" in paths
    finally:
        conn.close()


def test_naive_diacritic(corpus: Path, index_path: Path) -> None:
    conn, _ = _build(corpus, index_path)
    try:
        hits = _hits(conn, "naive")
        paths = {h.path for h in hits}
        assert "UFO/Grusch-hearing.txt" in paths
        assert "UFO/Grusch-hearing.md" not in paths
    finally:
        conn.close()


def test_phrase(corpus: Path, index_path: Path) -> None:
    conn, _ = _build(corpus, index_path)
    try:
        hits = _hits(conn, '"crash retrieval"')
        assert any(h.path.endswith("Grusch-hearing.txt") for h in hits)
    finally:
        conn.close()


def test_and_or(corpus: Path, index_path: Path) -> None:
    conn, _ = _build(corpus, index_path)
    try:
        assert _hits(conn, "grusch AND elizondo") == []
        hits = _hits(conn, "grusch OR elizondo")
        assert len(hits) >= 3
    finally:
        conn.close()


def test_ext_md(corpus: Path, index_path: Path) -> None:
    conn, _ = _build(corpus, index_path)
    try:
        hits = _hits(conn, "grusch", ext="md")
        assert hits and all(h.ext == "md" for h in hits)
    finally:
        conn.close()


def test_under_case_and_slash(corpus: Path, index_path: Path) -> None:
    conn, _ = _build(corpus, index_path)
    try:
        for under in ("UFO", "ufo", "UFO/"):
            hits = _hits(conn, "grusch OR plasma", under=under)
            paths = {h.path for h in hits}
            assert all(p.startswith("UFO/") or p == "UFO" for p in paths)
            assert not any(p.startswith("Physics/") for p in paths)
    finally:
        conn.close()


def test_under_rejects() -> None:
    import pytest

    for bad in ("", ".", "..", "/abs", "foo/../bar"):
        with pytest.raises(ValueError):
            normalize_under(bad)


def test_escape_like_order() -> None:
    assert escape_like("a\\b") == "a\\\\b"
    assert escape_like("100%") == "100\\%"
    assert escape_like("foo_bar") == "foo\\_bar"
    assert escape_like("a\\%_") == "a\\\\\\%\\_"


def test_under_like_metachar(corpus: Path, index_path: Path) -> None:
    (corpus / "foo_bar").mkdir()
    (corpus / "foo_bar" / "note.txt").write_text("Title: n\n\nunique-under-foo\n", encoding="utf-8")
    (corpus / "100%").mkdir()
    (corpus / "100%" / "note.txt").write_text("Title: n\n\nunique-under-pct\n", encoding="utf-8")
    conn, _ = _build(corpus, index_path)
    try:
        hits = _hits(conn, "unique-under-foo", under="foo_bar")
        assert [h.path for h in hits] == ["foo_bar/note.txt"]
        hits = _hits(conn, "unique-under-pct", under="100%")
        assert [h.path for h in hits] == ["100%/note.txt"]
    finally:
        conn.close()


def test_unique_token_hostile_name(corpus: Path, index_path: Path) -> None:
    conn, _ = _build(corpus, index_path)
    try:
        hits = _hits(conn, "unique-token-xyz")
        assert any(h.path == "Quote #Name'.txt" for h in hits)
    finally:
        conn.close()


def test_title_ranks_above_body(corpus: Path, index_path: Path) -> None:
    conn, _ = _build(corpus, index_path)
    try:
        hits = _hits(conn, "ranktoken")
        paths = [h.path for h in hits]
        assert paths.index("Rank-title.txt") < paths.index("Rank-body.txt")
    finally:
        conn.close()


def test_txt_is_not_universal(corpus: Path, index_path: Path) -> None:
    conn, _ = _build(corpus, index_path)
    try:
        hits = _hits(conn, "txt")
        assert len(hits) < 8
    finally:
        conn.close()
