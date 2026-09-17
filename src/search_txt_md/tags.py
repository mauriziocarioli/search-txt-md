from __future__ import annotations

import time
from pathlib import Path

_MAX_NAME = 64


class TagError(ValueError):
    """User-facing tag catalog or assignment error."""


def validate_tag_name(name: str) -> str:
    if not name or not name.strip():
        raise TagError("tag name is empty")
    if name != name.strip():
        raise TagError(f"invalid tag name {name!r}: leading or trailing whitespace")
    if len(name) > _MAX_NAME:
        raise TagError(f"invalid tag name {name!r}: longer than {_MAX_NAME} characters")
    if name.startswith("-"):
        raise TagError(f"invalid tag name {name!r}: must not start with '-'")
    for ch in name:
        if ch.isalnum() or ch in "-_":
            continue
        raise TagError(
            f"invalid tag name {name!r}: use letters, digits, '-' or '_'"
        )
    return name


def _now() -> int:
    return int(time.time())


def get_tag_id(conn, name: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM tags WHERE name = :name COLLATE NOCASE",
        {"name": name},
    ).fetchone()
    return int(row[0]) if row else None


def get_tag_name(conn, name: str) -> str | None:
    """Canonical stored name, or None if missing."""
    row = conn.execute(
        "SELECT name FROM tags WHERE name = :name COLLATE NOCASE",
        {"name": name},
    ).fetchone()
    return row[0] if row else None


def require_tag_id(conn, name: str) -> int:
    tid = get_tag_id(conn, name)
    if tid is None:
        raise TagError(f"tag not found: {name} (txtmd-tag create {name})")
    return tid


def create_tags(conn, names: list[str]) -> list[tuple[str, str]]:
    """Return (stored_name, 'created'|'exists') per input name."""
    out: list[tuple[str, str]] = []
    for raw in names:
        name = validate_tag_name(raw)
        existing = get_tag_name(conn, name)
        if existing is not None:
            out.append((existing, "exists"))
            continue
        conn.execute(
            "INSERT INTO tags(name, created_at) VALUES (:name, :created_at)",
            {"name": name, "created_at": _now()},
        )
        out.append((name, "created"))
    return out


def delete_tags(conn, names: list[str]) -> list[tuple[str, str]]:
    """Return (name, 'deleted'|'missing') per input name."""
    out: list[tuple[str, str]] = []
    for raw in names:
        name = validate_tag_name(raw)
        stored = get_tag_name(conn, name)
        if stored is None:
            out.append((name, "missing"))
            continue
        conn.execute("DELETE FROM tags WHERE name = :name COLLATE NOCASE", {"name": name})
        out.append((stored, "deleted"))
    return out


def list_tags(conn) -> list[tuple[str, int]]:
    rows = conn.execute(
        """
        SELECT t.name, COUNT(dt.path) AS n
        FROM tags t
        LEFT JOIN document_tags dt ON dt.tag_id = t.id
        GROUP BY t.id
        ORDER BY t.name COLLATE NOCASE
        """
    ).fetchall()
    return [(name, int(n)) for name, n in rows]


def resolve_doc_path(conn, path: str, corpus_root: Path | None) -> str:
    raw = path.strip()
    if not raw or "\x00" in raw:
        raise TagError(f"not in index: {path}")
    row = conn.execute(
        "SELECT path FROM documents WHERE path = :path", {"path": raw}
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT path FROM documents WHERE path = :path COLLATE NOCASE",
        {"path": raw},
    ).fetchone()
    if row:
        return row[0]
    if corpus_root is not None:
        p = Path(raw).expanduser()
        if p.is_absolute():
            try:
                rel = p.resolve().relative_to(corpus_root.resolve()).as_posix()
            except ValueError as exc:
                raise TagError(f"not in corpus: {path}") from exc
            row = conn.execute(
                "SELECT path FROM documents WHERE path = :path", {"path": rel}
            ).fetchone()
            if row:
                return row[0]
    raise TagError(f"not in index: {path}")


def _paths_under(conn, under: str) -> list[str]:
    from search_txt_md.search import normalize_under

    under_val, under_like = normalize_under(under)
    rows = conn.execute(
        """
        SELECT path FROM documents
        WHERE LOWER(path) = :under
           OR LOWER(path) LIKE :under_like ESCAPE '\\'
        ORDER BY path
        """,
        {"under": under_val, "under_like": under_like},
    ).fetchall()
    return [r[0] for r in rows]


def _collect_paths(
    conn,
    paths: list[str],
    *,
    under: str | None,
    corpus_root: Path | None,
) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    if under is not None:
        for p in _paths_under(conn, under):
            if p not in seen:
                seen.add(p)
                found.append(p)
    for raw in paths:
        p = resolve_doc_path(conn, raw, corpus_root)
        if p not in seen:
            seen.add(p)
            found.append(p)
    if not found:
        raise TagError("no documents to tag; pass PATH or --under")
    return found


def apply_tags(
    conn,
    name: str,
    paths: list[str],
    *,
    under: str | None = None,
    corpus_root: Path | None = None,
) -> list[tuple[str, str]]:
    """Return (path, 'tagged'|'already') in assignment order."""
    tag_id = require_tag_id(conn, validate_tag_name(name))
    out: list[tuple[str, str]] = []
    for path in _collect_paths(conn, paths, under=under, corpus_root=corpus_root):
        exists = conn.execute(
            "SELECT 1 FROM document_tags WHERE path = :path AND tag_id = :tag_id",
            {"path": path, "tag_id": tag_id},
        ).fetchone()
        if exists:
            out.append((path, "already"))
            continue
        conn.execute(
            "INSERT INTO document_tags(path, tag_id) VALUES (:path, :tag_id)",
            {"path": path, "tag_id": tag_id},
        )
        out.append((path, "tagged"))
    return out


def remove_tags(
    conn,
    name: str,
    paths: list[str],
    *,
    under: str | None = None,
    corpus_root: Path | None = None,
) -> list[tuple[str, str]]:
    """Return (path, 'untagged'|'absent')."""
    tag_id = require_tag_id(conn, validate_tag_name(name))
    out: list[tuple[str, str]] = []
    for path in _collect_paths(conn, paths, under=under, corpus_root=corpus_root):
        cur = conn.execute(
            "DELETE FROM document_tags WHERE path = :path AND tag_id = :tag_id",
            {"path": path, "tag_id": tag_id},
        )
        out.append((path, "untagged" if cur.rowcount else "absent"))
    return out


def show_tags(conn, path: str, *, corpus_root: Path | None = None) -> tuple[str, list[str]]:
    resolved = resolve_doc_path(conn, path, corpus_root)
    rows = conn.execute(
        """
        SELECT t.name FROM document_tags dt
        JOIN tags t ON t.id = dt.tag_id
        WHERE dt.path = :path
        ORDER BY t.name COLLATE NOCASE
        """,
        {"path": resolved},
    ).fetchall()
    return resolved, [r[0] for r in rows]


def tags_for_paths(conn, paths: list[str]) -> dict[str, list[str]]:
    if not paths:
        return {}
    placeholders = ",".join("?" * len(paths))
    rows = conn.execute(
        f"""
        SELECT dt.path, t.name
        FROM document_tags dt
        JOIN tags t ON t.id = dt.tag_id
        WHERE dt.path IN ({placeholders})
        ORDER BY t.name COLLATE NOCASE
        """,
        paths,
    ).fetchall()
    out: dict[str, list[str]] = {p: [] for p in paths}
    for path, name in rows:
        out.setdefault(path, []).append(name)
    return out


def prune_orphan_tags(conn) -> int:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'document_tags'"
    ).fetchone()
    if row is None:
        return 0
    cur = conn.execute(
        "DELETE FROM document_tags WHERE path NOT IN (SELECT path FROM documents)"
    )
    return int(cur.rowcount)
