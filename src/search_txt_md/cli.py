from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from search_txt_md.config import SchemaError, Settings
from search_txt_md.index import (
    assert_searchable,
    connect,
    format_index_summary,
    incremental_index,
    init_schema,
    meta_get,
)
from search_txt_md.query import QuerySyntaxError, parse_query
from search_txt_md.search import format_human, format_json, timed_search

SEARCH_EPILOG = """\
Query language: AND/OR/NOT, parentheses, "phrases", prefix*, field filters
(title:/path:/folder:/body:). Default operator is AND. Use --or for juxtaposition.

Leading-dash terms are NOT argparse flags. Pass them as a single argument or after --:
  txtmd-search 'uap -hoax'
  txtmd-search uap -- -hoax
Do not write: txtmd-search uap -hoax
"""


def _index_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="txtmd-index",
        description="Index podcast .txt/.md files into a local SQLite FTS5 database.",
    )
    p.add_argument("--root", type=Path, default=None, help="Corpus directory")
    p.add_argument("--index", type=Path, default=None, help="SQLite index path")
    p.add_argument(
        "--full",
        action="store_true",
        help="Rebuild FTS from readable files (drops evicted stored bodies)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk and compare; never create or write the index",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print open:/skip dataless: on stderr before each open()",
    )
    return p


def _search_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="txtmd-search",
        description="Boolean search of the local FTS5 index, ranked by BM25.",
        epilog=SEARCH_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
        allow_abbrev=False,
    )
    p.add_argument(
        "--help",
        action="help",
        help="show this help message and exit",
    )
    p.add_argument("query", nargs="*", help="Query tokens (join with spaces)")
    p.add_argument("--index", type=Path, default=None, help="SQLite index path")
    p.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Corpus root used only to resolve abs_path in output",
    )
    p.add_argument("--limit", type=int, default=20, help="Max hits (1..10000)")
    p.add_argument("--json", action="store_true", help="JSON document on stdout")
    p.add_argument("--ext", choices=["txt", "md"], default=None, help="Restrict extension")
    p.add_argument("--under", default=None, help="Restrict to a relative subdirectory")
    p.add_argument("--or", dest="default_or", action="store_true", help="Juxtaposition is OR")
    p.add_argument("--no-snippet", action="store_true", help="Omit snippets")
    p.add_argument("--explain", action="store_true", help="Print AST/FTS5/elapsed to stderr")
    return p


def _dispatch_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="search_txt_md")
    sub = p.add_subparsers(dest="cmd", required=True)
    idx = sub.add_parser("index", add_help=False, parents=[_index_parser()], help="Build index")
    idx.set_defaults(_handler="index")
    sch = sub.add_parser("search", add_help=False, parents=[_search_parser()], help="Search index")
    sch.set_defaults(_handler="search")
    return p


def index_main(argv: list[str] | None = None) -> int:
    args = _index_parser().parse_args(argv)
    settings = Settings.resolve(root=args.root, index=args.index)
    if not settings.root.is_dir():
        print(f"corpus root missing: {settings.root}", file=sys.stderr)
        return 3
    dry_run = bool(args.dry_run)
    conn = None
    try:
        if dry_run:
            if settings.index.is_file():
                conn = connect(settings.index, writable=False)
            stats = incremental_index(
                conn, settings, full=bool(args.full), verbose=bool(args.verbose), dry_run=True
            )
        else:
            try:
                settings.ensure_index_parent()
                conn = connect(settings.index, writable=True)
                init_schema(conn)
            except OSError as exc:
                print(f"cannot create/open index: {exc}", file=sys.stderr)
                return 3
            stats = incremental_index(
                conn, settings, full=bool(args.full), verbose=bool(args.verbose), dry_run=False
            )
    except SchemaError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SystemExit:
        raise
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    finally:
        if conn is not None:
            conn.close()
    print(format_index_summary(stats, settings))
    if dry_run:
        would = stats.change_paths + [f"- {p}" for p in stats.pruned_paths]
        if would:
            print("Would change (first 20):")
            for path in would[:20]:
                print(f"  {path}")
    return 0


def search_main(argv: list[str] | None = None) -> int:
    parser = _search_parser()
    args = parser.parse_args(argv)
    query_text = " ".join(args.query).strip()
    if not query_text:
        parser.print_help()
        return 1
    if args.limit < 1 or args.limit > 10000:
        print("--limit must be 1..10000", file=sys.stderr)
        return 1
    settings = Settings.resolve(root=args.root, index=args.index)
    if not settings.index.is_file():
        print(f"index not found: {settings.index}; run txtmd-index", file=sys.stderr)
        return 2
    try:
        parsed = parse_query(query_text, default_op="OR" if args.default_or else "AND")
        fts5 = parsed.to_fts5()
    except QuerySyntaxError as exc:
        print(f"query error: {exc}", file=sys.stderr)
        return 1
    try:
        conn = connect(settings.index, writable=False)
    except SchemaError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (sqlite3.Error, OSError) as exc:
        print(f"cannot open index: {exc}", file=sys.stderr)
        return 3
    try:
        try:
            assert_searchable(conn)
        except SchemaError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        stored_root = meta_get(conn, "corpus_root")
        if args.root is not None:
            corpus_root = settings.root
        elif stored_root:
            corpus_root = Path(stored_root)
        else:
            corpus_root = None
        try:
            hits, elapsed_ms = timed_search(
                conn,
                fts5,
                limit=args.limit,
                ext=args.ext,
                under=args.under,
                corpus_root=corpus_root,
                snippets=not args.no_snippet,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except sqlite3.Error as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.explain:
            print(f"default_op={parsed.default_op}", file=sys.stderr)
            print(f"ast={parsed.ast!r}", file=sys.stderr)
            print(f"fts5={fts5}", file=sys.stderr)
            print(f"elapsed_ms={elapsed_ms:.2f}", file=sys.stderr)
            print(f"hits={len(hits)}", file=sys.stderr)
        if args.json:
            print(format_json(hits, query=query_text, fts5=fts5, limit=args.limit))
        else:
            print(format_human(hits))
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] in {"-h", "--help"}:
        _dispatch_parser().parse_args(["-h"] if not args else args)
        return 0
    cmd, rest = args[0], args[1:]
    if cmd == "index":
        return index_main(rest)
    if cmd == "search":
        return search_main(rest)
    print(f"unknown command {cmd!r}; use index or search", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
