from __future__ import annotations

from importlib import resources
from pathlib import Path

from search_txt_md.config import SCHEMA_VERSION, Settings
from search_txt_md.index import (
    connect,
    incremental_index,
    init_schema,
    meta_get,
    require_fts5,
)
from search_txt_md.tags import apply_tags, create_tags, show_tags


def _settings(corpus: Path, index_path: Path) -> Settings:
    return Settings(root=corpus, index=index_path)


def _index(corpus: Path, index_path: Path, **kwargs):
    settings = _settings(corpus, index_path)
    settings.ensure_index_parent()
    conn = connect(index_path, writable=True)
    try:
        init_schema(conn)
        stats = incremental_index(conn, settings, dry_run=False, **kwargs)
        return stats, conn
    except Exception:
        conn.close()
        raise


def test_schema_sql_shipped() -> None:
    path = resources.files("search_txt_md").joinpath("schema.sql")
    assert path.is_file()


def test_create_close_reopen_insert(tmp_path: Path) -> None:
    db = tmp_path / "i.sqlite"
    conn = connect(db, writable=True)
    init_schema(conn)
    conn.close()
    conn = connect(db, writable=True)
    init_schema(conn)
    conn.execute(
        "INSERT INTO documents(path,size,mtime_ns,ext,title,folder,stem,url,body,indexed_at) "
        "VALUES ('a.txt',1,1,'txt','t','','a',NULL,'naive naïve',1)"
    )
    conn.commit()
    opts = {row[0] for row in conn.execute("PRAGMA compile_options")}
    assert "ENABLE_FTS5" in opts
    require_fts5(conn)
    rows = conn.execute("SELECT title FROM docs_fts WHERE docs_fts MATCH 'naive'").fetchall()
    assert rows
    conn.execute("DELETE FROM documents WHERE path = 'a.txt'")
    conn.commit()
    assert conn.execute("SELECT count(*) FROM docs_fts").fetchone()[0] == 0
    conn.close()


def test_first_index_counts(corpus: Path, index_path: Path) -> None:
    stats, conn = _index(corpus, index_path, full=False, verbose=False)
    try:
        assert stats.walked == 8
        assert stats.upserted == 8
        assert stats.errors == 0
        n = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
        assert n == 8
        distinct = conn.execute("SELECT count(DISTINCT path) FROM documents").fetchone()[0]
        assert n == distinct
        assert meta_get(conn, "schema_version") == str(SCHEMA_VERSION)
    finally:
        conn.close()


def test_second_run_unchanged(corpus: Path, index_path: Path) -> None:
    _stats, conn = _index(corpus, index_path, full=False, verbose=False)
    conn.close()
    stats, conn = _index(corpus, index_path, full=False, verbose=False)
    try:
        assert stats.upserted == 0
        assert stats.unchanged == 8
        assert stats.pruned == 0
    finally:
        conn.close()


def test_touch_one_upserts(corpus: Path, index_path: Path) -> None:
    _index(corpus, index_path, full=False, verbose=False)[1].close()
    target = corpus / "UFO" / "Grusch-hearing.txt"
    target.write_text(target.read_text(encoding="utf-8") + "\nextra\n", encoding="utf-8")
    stats, conn = _index(corpus, index_path, full=False, verbose=False)
    try:
        assert stats.upserted == 1
        assert stats.unchanged == 7
    finally:
        conn.close()


def test_delete_prunes(corpus: Path, index_path: Path) -> None:
    _index(corpus, index_path, full=False, verbose=False)[1].close()
    (corpus / "Physics" / "tokamak-seminar.txt").unlink()
    stats, conn = _index(corpus, index_path, full=False, verbose=False)
    try:
        assert stats.pruned == 1
        paths = [r[0] for r in conn.execute("SELECT path FROM documents")]
        assert "Physics/tokamak-seminar.txt" not in paths
    finally:
        conn.close()


def test_full_rebuild(corpus: Path, index_path: Path) -> None:
    _index(corpus, index_path, full=False, verbose=False)[1].close()
    stats, conn = _index(corpus, index_path, full=True, verbose=False)
    try:
        assert stats.upserted == 8
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 8
    finally:
        conn.close()


def test_full_rebuild_preserves_tags(corpus: Path, index_path: Path) -> None:
    _stats, conn = _index(corpus, index_path, full=False, verbose=False)
    create_tags(conn, ["ufo"])
    apply_tags(conn, "ufo", ["UFO/Grusch-hearing.txt"])
    conn.commit()
    conn.close()
    _stats, conn = _index(corpus, index_path, full=True, verbose=False)
    try:
        assert show_tags(conn, "UFO/Grusch-hearing.txt")[1] == ["ufo"]
        assert conn.execute("SELECT count(*) FROM tags").fetchone()[0] == 1
    finally:
        conn.close()


def test_prune_drops_tag_assignments(corpus: Path, index_path: Path) -> None:
    _stats, conn = _index(corpus, index_path, full=False, verbose=False)
    create_tags(conn, ["plasma"])
    apply_tags(conn, "plasma", ["Physics/tokamak-seminar.txt"])
    conn.commit()
    conn.close()
    (corpus / "Physics" / "tokamak-seminar.txt").unlink()
    _stats, conn = _index(corpus, index_path, full=False, verbose=False)
    try:
        n = conn.execute("SELECT count(*) FROM document_tags").fetchone()[0]
        assert n == 0
        assert conn.execute("SELECT count(*) FROM tags").fetchone()[0] == 1
    finally:
        conn.close()


def test_migrate_v1_to_v2(corpus: Path, index_path: Path) -> None:
    _stats, conn = _index(corpus, index_path, full=False, verbose=False)
    conn.execute("DROP TABLE document_tags")
    conn.execute("DROP TABLE tags")
    conn.execute(
        "UPDATE index_meta SET value = '1' WHERE key = 'schema_version'"
    )
    conn.commit()
    conn.close()
    conn = connect(index_path, writable=True)
    try:
        init_schema(conn)
        assert meta_get(conn, "schema_version") == "2"
        tables = {
            n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "tags" in tables
        assert "document_tags" in tables
        n = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
        assert n == 8
    finally:
        conn.close()


def test_dataless_keeps_row(corpus: Path, index_path: Path, monkeypatch) -> None:
    stats, conn = _index(corpus, index_path, full=False, verbose=False)
    conn.close()
    from search_txt_md.crawl import CrawlItem
    from search_txt_md.crawl import iter_corpus as real_iter

    def _dataless_wrap(root: Path):
        for item in real_iter(root):
            yield CrawlItem(
                abs_path=item.abs_path,
                relpath=item.relpath,
                ext=item.ext,
                size=item.size,
                mtime_ns=item.mtime_ns,
                dataless=True,
                warning=item.warning,
            )

    monkeypatch.setattr("search_txt_md.index.iter_corpus", _dataless_wrap)
    stats, conn = _index(corpus, index_path, full=False, verbose=True)
    try:
        assert stats.dataless == 8
        assert stats.dataless_kept == 8
        assert stats.upserted == 0
        body = conn.execute(
            "SELECT body FROM documents WHERE path = 'UFO/Grusch-hearing.txt'"
        ).fetchone()[0]
        assert "David Grusch" in body
    finally:
        conn.close()


def test_dry_run_missing_does_not_create(corpus: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "missing" / "index.sqlite"
    settings = Settings(root=corpus, index=index_path)
    stats = incremental_index(
        None, settings, full=False, verbose=False, dry_run=True
    )
    assert not index_path.exists()
    assert stats.upserted == 8


def test_verbose_prints_open_before(corpus: Path, index_path: Path, capsys) -> None:
    _stats, conn = _index(corpus, index_path, full=False, verbose=True)
    conn.close()
    err = capsys.readouterr().err
    assert "open: UFO/Grusch-hearing.txt" in err
