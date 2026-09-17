from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from search_txt_md.query import And, Node, Not, Or, TagNot, Term
from search_txt_md.tags import tags_for_paths

_FTS_SQL = """
SELECT
  d.path,
  d.title,
  d.ext,
  d.url,
  snippet(docs_fts, 3, '>>', '<<', '…', 16) AS snippet,
  bm25(docs_fts, 10.0, 2.0, 4.0, 1.0)      AS score
FROM docs_fts
JOIN documents d ON d.id = docs_fts.rowid
WHERE docs_fts MATCH :fts5
  AND (:ext    IS NULL OR d.ext = :ext)
  AND (
        :under IS NULL
        OR LOWER(d.path) = :under
        OR LOWER(d.path) LIKE :under_like ESCAPE '\\'
      )
  AND ({tag_sql})
ORDER BY score ASC, d.path ASC
LIMIT :limit
"""

_TAG_ONLY_SQL = """
SELECT
  d.path,
  d.title,
  d.ext,
  d.url,
  ''  AS snippet,
  0.0 AS score
FROM documents d
WHERE (:ext    IS NULL OR d.ext = :ext)
  AND (
        :under IS NULL
        OR LOWER(d.path) = :under
        OR LOWER(d.path) LIKE :under_like ESCAPE '\\'
      )
  AND ({tag_sql})
ORDER BY d.path ASC
LIMIT :limit
"""


@dataclass(frozen=True)
class Hit:
    rank: int
    score: float
    path: str
    abs_path: str | None
    title: str
    ext: str
    snippet: str
    url: str | None
    tags: tuple[str, ...] = field(default_factory=tuple)


def escape_like(s: str) -> str:
    """LIKE metacharacters for ESCAPE '\\'. Order is load-bearing: backslash first."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalize_under(rel: str) -> tuple[str, str]:
    """Validate --under; return (lowercased prefix, escape_like(prefix) + '/%')."""
    if rel is None:
        raise ValueError("invalid --under")
    raw = rel.strip()
    if not raw or raw == "." or "\x00" in raw:
        raise ValueError("invalid --under")
    posix = raw.replace("\\", "/")
    while posix.endswith("/"):
        posix = posix[:-1]
    if not posix or posix == ".":
        raise ValueError("invalid --under")
    if posix.startswith("/"):
        raise ValueError("invalid --under: absolute path")
    parts = posix.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid --under")
    under = posix.casefold()
    return under, escape_like(under) + "/%"


def compile_tag_sql(tag_ast: Node, *, prefix: str = "tagp") -> tuple[str, dict[str, str]]:
    params: dict[str, str] = {}
    counter = 0

    def rec(node: Node) -> str:
        nonlocal counter
        if isinstance(node, Term):
            if node.field != "tag":
                raise TypeError(f"non-tag term in tag predicate: {node!r}")
            key = f"{prefix}{counter}"
            counter += 1
            params[key] = node.value
            return (
                "EXISTS (SELECT 1 FROM document_tags dt "
                "JOIN tags t ON t.id = dt.tag_id "
                f"WHERE dt.path = d.path AND t.name = :{key} COLLATE NOCASE)"
            )
        if isinstance(node, TagNot):
            return "NOT (" + rec(node.child) + ")"
        if isinstance(node, And):
            return "(" + " AND ".join(rec(c) for c in node.children) + ")"
        if isinstance(node, Or):
            return "(" + " OR ".join(rec(c) for c in node.children) + ")"
        if isinstance(node, Not):
            return "(" + rec(node.left) + " AND NOT (" + rec(node.right) + "))"
        raise TypeError(f"unknown node {type(node)!r}")

    return rec(tag_ast), params


def search(
    conn: sqlite3.Connection,
    fts5: str | None,
    *,
    limit: int,
    ext: str | None,
    under: str | None,
    corpus_root: Path | None,
    snippets: bool = True,
    tag_pred: Node | None = None,
) -> list[Hit]:
    if fts5 is None and tag_pred is None:
        raise ValueError("search requires a keyword query or a tag filter")
    under_val: str | None = None
    under_like: str | None = None
    if under is not None:
        under_val, under_like = normalize_under(under)
    ext_val = ext.lower() if ext else None
    if tag_pred is None:
        tag_sql, tag_params = "1", {}
    else:
        tag_sql, tag_params = compile_tag_sql(tag_pred)
    params: dict[str, object] = {
        "ext": ext_val,
        "under": under_val,
        "under_like": under_like,
        "limit": limit,
        **tag_params,
    }
    if fts5 is None:
        sql = _TAG_ONLY_SQL.format(tag_sql=tag_sql)
    else:
        sql = _FTS_SQL.format(tag_sql=tag_sql)
        params["fts5"] = fts5
    rows = conn.execute(sql, params).fetchall()
    hits: list[Hit] = []
    root = corpus_root
    paths = [row[0] for row in rows]
    by_path = tags_for_paths(conn, paths)
    for i, (path, title, ext_row, url, snippet, score) in enumerate(rows, start=1):
        text = snippet if snippets else ""
        if snippets and fts5 is not None and not (text or "").strip():
            text = title
        abs_path = str(root / path) if root is not None else None
        hits.append(
            Hit(
                rank=i,
                score=float(score),
                path=path,
                abs_path=abs_path,
                title=title,
                ext=ext_row,
                snippet=text,
                url=url,
                tags=tuple(by_path.get(path, ())),
            )
        )
    return hits


def format_human(hits: list[Hit]) -> str:
    if not hits:
        return "No hits."
    blocks: list[str] = []
    for hit in hits:
        snippet_line = f"      {hit.snippet}" if hit.snippet else ""
        block = (
            f"{hit.rank:3d}.  {hit.score:8.3f}  {hit.path}\n"
            f"      Title: {hit.title}"
        )
        if hit.tags:
            block += "\n      Tags: " + ", ".join(hit.tags)
        if snippet_line:
            block += "\n" + snippet_line
        blocks.append(block)
    return "\n\n".join(blocks)


def format_json(
    hits: list[Hit],
    *,
    query: str,
    fts5: str,
    limit: int,
) -> str:
    payload = {
        "query": query,
        "fts5": fts5,
        "limit": limit,
        "hits": [
            {
                "rank": h.rank,
                "score": round(h.score, 3),
                "path": h.path,
                "abs_path": h.abs_path,
                "title": h.title,
                "ext": h.ext,
                "url": h.url,
                "snippet": h.snippet,
                "tags": list(h.tags),
            }
            for h in hits
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def timed_search(*args, **kwargs) -> tuple[list[Hit], float]:
    t0 = time.perf_counter()
    hits = search(*args, **kwargs)
    return hits, (time.perf_counter() - t0) * 1000.0
