from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_SEARCH_SQL = """
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
ORDER BY score ASC, d.path ASC
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


def search(
    conn: sqlite3.Connection,
    fts5: str,
    *,
    limit: int,
    ext: str | None,
    under: str | None,
    corpus_root: Path | None,
    snippets: bool = True,
) -> list[Hit]:
    under_val: str | None = None
    under_like: str | None = None
    if under is not None:
        under_val, under_like = normalize_under(under)
    ext_val = ext.lower() if ext else None
    rows = conn.execute(
        _SEARCH_SQL,
        {
            "fts5": fts5,
            "ext": ext_val,
            "under": under_val,
            "under_like": under_like,
            "limit": limit,
        },
    ).fetchall()
    hits: list[Hit] = []
    root = corpus_root
    for i, (path, title, ext_row, url, snippet, score) in enumerate(rows, start=1):
        text = snippet if snippets else ""
        if snippets and not (text or "").strip():
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
            }
            for h in hits
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def timed_search(*args, **kwargs) -> tuple[list[Hit], float]:
    t0 = time.perf_counter()
    hits = search(*args, **kwargs)
    return hits, (time.perf_counter() - t0) * 1000.0
