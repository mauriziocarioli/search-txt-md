from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from search_txt_md.config import SCHEMA_VERSION, SchemaError, Settings
from search_txt_md.crawl import iter_corpus
from search_txt_md.extract import extract_document

_ERROR_CAP = 20
_CHANGE_CAP = 20
_COMMIT_EVERY = 200
_PROGRESS_EVERY = 200

_UPSERT_SQL = """
INSERT INTO documents (path, size, mtime_ns, ext, title, folder, stem, url, body, indexed_at)
VALUES (:path, :size, :mtime_ns, :ext, :title, :folder, :stem, :url, :body, :indexed_at)
ON CONFLICT(path) DO UPDATE SET
  size       = excluded.size,
  mtime_ns   = excluded.mtime_ns,
  ext        = excluded.ext,
  title      = excluded.title,
  folder     = excluded.folder,
  stem       = excluded.stem,
  url        = excluded.url,
  body       = excluded.body,
  indexed_at = excluded.indexed_at
"""


@dataclass
class IndexStats:
    walked: int = 0
    upserted: int = 0
    unchanged: int = 0
    pruned: int = 0
    dataless: int = 0
    dataless_kept: int = 0
    dataless_new: int = 0
    errors: int = 0
    elapsed_s: float = 0.0
    error_paths: list[tuple[str, str]] = field(default_factory=list)
    change_paths: list[str] = field(default_factory=list)
    txt: int = 0
    md: int = 0
    pruned_paths: list[str] = field(default_factory=list)


def schema_sql() -> str:
    return resources.files("search_txt_md").joinpath("schema.sql").read_text(encoding="utf-8")


def require_fts5(conn: sqlite3.Connection) -> None:
    opts = {row[0] for row in conn.execute("PRAGMA compile_options")}
    if "ENABLE_FTS5" not in opts:
        raise SchemaError(
            "sqlite3 was built without FTS5; recreate the venv with $BREWBIN/python3"
        )


def connect(index_path: Path, *, writable: bool) -> sqlite3.Connection:
    """Open the index. writable=False must not be used on a missing file."""
    if writable:
        created = not index_path.exists()
        conn = sqlite3.connect(index_path)
        if created:
            os.chmod(index_path, 0o600)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA mmap_size = 268435456")
        conn.execute("PRAGMA busy_timeout = 5000")
        require_fts5(conn)
        return conn
    conn = sqlite3.connect(index_path)
    conn.execute("PRAGMA busy_timeout = 5000")
    require_fts5(conn)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _insert_meta_if_missing(conn: sqlite3.Connection) -> None:
    now = datetime.now(UTC).isoformat()
    defaults = {
        "schema_version": str(SCHEMA_VERSION),
        "tokenizer": "unicode61 remove_diacritics 2",
        "created_at": now,
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO index_meta(key, value) VALUES (:key, :value)",
            {"key": key, "value": value},
        )


def _documents_ddl(sql: str) -> str:
    marker = "CREATE TABLE documents"
    i = sql.find(marker)
    if i < 0:
        raise RuntimeError("schema.sql is missing CREATE TABLE documents")
    return sql[i:]


def init_schema(conn: sqlite3.Connection) -> None:
    sql = schema_sql()
    tables = {
        n
        for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
        )
    }
    if "documents" not in tables:
        conn.executescript(sql)
        _insert_meta_if_missing(conn)
        conn.commit()
        return
    row = conn.execute(
        "SELECT value FROM index_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None or int(row[0]) != SCHEMA_VERSION:
        raise SchemaError(
            f"schema_version is {row[0] if row else 'missing'}; "
            "run txtmd-index --full or delete the index file"
        )


def rebuild_documents(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TRIGGER IF EXISTS documents_ai")
    conn.execute("DROP TRIGGER IF EXISTS documents_ad")
    conn.execute("DROP TRIGGER IF EXISTS documents_au")
    conn.execute("DROP TABLE IF EXISTS docs_fts")
    conn.execute("DROP TABLE IF EXISTS documents")
    conn.executescript(_documents_ddl(schema_sql()))
    conn.commit()


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM index_meta WHERE key = :key", {"key": key}
    ).fetchone()
    return row[0] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO index_meta(key, value) VALUES (:key, :value) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        {"key": key, "value": value},
    )


def assert_searchable(conn: sqlite3.Connection) -> None:
    tables = {n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "documents" not in tables or "index_meta" not in tables:
        raise SchemaError("index is not initialized; run txtmd-index")
    row = conn.execute(
        "SELECT value FROM index_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None or int(row[0]) != SCHEMA_VERSION:
        raise SchemaError("schema_version mismatch; run txtmd-index --full")


def _load_existing(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    return {
        path: (size, mtime_ns)
        for path, size, mtime_ns in conn.execute(
            "SELECT path, size, mtime_ns FROM documents"
        )
    }


def _record_error(stats: IndexStats, relpath: str, msg: str) -> None:
    stats.errors += 1
    if len(stats.error_paths) < _ERROR_CAP:
        stats.error_paths.append((relpath, msg))


def _record_change(stats: IndexStats, relpath: str) -> None:
    if len(stats.change_paths) < _CHANGE_CAP:
        stats.change_paths.append(relpath)


def incremental_index(
    conn: sqlite3.Connection | None,
    settings: Settings,
    *,
    full: bool,
    verbose: bool,
    dry_run: bool,
) -> IndexStats:
    t0 = time.perf_counter()
    stats = IndexStats()
    existing: dict[str, tuple[int, int]] = {}
    writable = conn is not None and not dry_run

    if conn is not None:
        existing = _load_existing(conn)
        stored_root = meta_get(conn, "corpus_root")
        if stored_root and Path(stored_root) != settings.root and not full:
            raise SchemaError(
                f"index corpus_root is {stored_root}; "
                f"got {settings.root}. Pass --full to reassign."
            )
        if writable and full:
            rebuild_documents(conn)
            existing = {}

    seen: set[str] = set()
    since_commit = 0
    if writable:
        conn.commit()
        conn.execute("BEGIN")

    try:
        for item in iter_corpus(settings.root):
            seen.add(item.relpath)
            stats.walked += 1
            if item.ext == "txt":
                stats.txt += 1
            else:
                stats.md += 1
            if item.warning and verbose:
                print(f"warn: {item.relpath}: {item.warning}", file=sys.stderr, flush=True)

            if item.dataless:
                stats.dataless += 1
                if item.relpath in existing:
                    stats.dataless_kept += 1
                else:
                    stats.dataless_new += 1
                if verbose:
                    print(
                        f"skip dataless (not opening): {item.relpath}",
                        file=sys.stderr,
                        flush=True,
                    )
                continue

            if not full and existing.get(item.relpath) == (item.size, item.mtime_ns):
                stats.unchanged += 1
                _maybe_progress(stats, verbose=False)
                continue

            if verbose:
                print(f"open: {item.relpath}", file=sys.stderr, flush=True)
            try:
                doc = extract_document(item.abs_path, item.relpath, item.ext)
            except Exception as exc:  # noqa: BLE001 — keep row, tally and continue
                _record_error(stats, item.relpath, f"{type(exc).__name__}: {exc}")
                continue

            stats.upserted += 1
            _record_change(stats, item.relpath)
            if writable:
                conn.execute(
                    _UPSERT_SQL,
                    {
                        "path": doc.relpath,
                        "size": item.size,
                        "mtime_ns": item.mtime_ns,
                        "ext": doc.ext,
                        "title": doc.title,
                        "folder": doc.folder,
                        "stem": doc.stem,
                        "url": doc.url,
                        "body": doc.body,
                        "indexed_at": int(time.time()),
                    },
                )
                since_commit += 1
                if full and since_commit >= _COMMIT_EVERY:
                    conn.execute("COMMIT")
                    conn.execute("BEGIN")
                    since_commit = 0
            _maybe_progress(stats, verbose=False)

        pruned_paths = sorted(existing.keys() - seen)
        stats.pruned = len(pruned_paths)
        stats.pruned_paths = pruned_paths[:_CHANGE_CAP]
        if writable:
            for path in pruned_paths:
                conn.execute("DELETE FROM documents WHERE path = :path", {"path": path})
            now = datetime.now(UTC).isoformat()
            meta_set(conn, "corpus_root", str(settings.root))
            meta_set(conn, "last_run_at", now)
            meta_set(
                conn,
                "last_run_stats",
                json.dumps(
                    {
                        "walked": stats.walked,
                        "upserted": stats.upserted,
                        "unchanged": stats.unchanged,
                        "pruned": stats.pruned,
                        "dataless": stats.dataless,
                        "errors": stats.errors,
                    }
                ),
            )
            conn.execute("COMMIT")
    except Exception:
        if writable:
            conn.execute("ROLLBACK")
        raise

    stats.elapsed_s = time.perf_counter() - t0
    return stats


def _maybe_progress(stats: IndexStats, *, verbose: bool) -> None:
    del verbose
    total = stats.upserted + stats.unchanged
    if total == 0 or total % _PROGRESS_EVERY != 0:
        return
    if not sys.stderr.isatty():
        return
    print(
        f"{stats.upserted + stats.unchanged}/{stats.walked}  {stats.elapsed_s:.1f}s",
        file=sys.stderr,
        flush=True,
    )


def format_index_summary(stats: IndexStats, settings: Settings) -> str:
    lines = [
        f"Root:    {settings.root}",
        f"Index:   {settings.index}",
        f"Walked {stats.walked} (txt={stats.txt} md={stats.md})",
        f"  unchanged: {stats.unchanged}",
        f"  upserted:  {stats.upserted}",
        f"  pruned:    {stats.pruned}",
        f"  dataless:  {stats.dataless} (kept {stats.dataless_kept}, never-indexed {stats.dataless_new})",
        f"  errors:    {stats.errors}",
    ]
    for path, msg in stats.error_paths:
        lines.append(f"    {path}: {msg}")
    lines.append(f"Elapsed: {stats.elapsed_s:.2f}s")
    return "\n".join(lines)
