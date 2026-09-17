from __future__ import annotations

import argparse
import json
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
from search_txt_md.query import (
    QuerySyntaxError,
    and_tag_terms,
    compile_query,
)
from search_txt_md.search import format_human, format_json, timed_search
from search_txt_md.tags import (
    TagError,
    apply_tags,
    create_tags,
    delete_tags,
    get_tag_id,
    list_tags,
    remove_tags,
    show_tags,
    validate_tag_name,
)

SEARCH_EPILOG = """\
Query language: AND/OR/NOT, parentheses, "phrases", prefix*, field filters
(title:/path:/folder:/body:/tag:). Default operator is AND. Use --or for juxtaposition.

tag: filters are SQL, not FTS5. AND them with keywords; OR tags with each other.
Cannot OR a tag: filter with a keyword.

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
    p.add_argument(
        "--tag",
        action="append",
        default=[],
        dest="tags",
        metavar="NAME",
        help="Require this tag (repeatable, AND)",
    )
    return p


def _tag_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="txtmd-tag",
        description="Create tags and apply them to indexed documents.",
    )
    p.add_argument("--index", type=Path, default=None, help="SQLite index path")
    p.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Corpus root used to resolve absolute paths",
    )
    sub = p.add_subparsers(dest="tag_cmd", required=True)

    cr = sub.add_parser("create", help="Create tag names")
    cr.add_argument("names", nargs="+", help="Tag names")

    de = sub.add_parser("delete", help="Delete tags and their assignments")
    de.add_argument("names", nargs="+", help="Tag names")

    ls = sub.add_parser("list", help="List tags and assignment counts")
    ls.add_argument("--json", action="store_true", help="JSON document on stdout")

    ap = sub.add_parser("apply", help="Apply a tag to indexed documents")
    ap.add_argument("name", help="Tag name")
    ap.add_argument("paths", nargs="*", help="Document paths (index-relative or absolute)")
    ap.add_argument("--under", default=None, help="Apply to every indexed path under DIR")

    rm = sub.add_parser("remove", help="Remove a tag from documents")
    rm.add_argument("name", help="Tag name")
    rm.add_argument("paths", nargs="*", help="Document paths")
    rm.add_argument("--under", default=None, help="Remove from every indexed path under DIR")

    sh = sub.add_parser("show", help="List tags on a document")
    sh.add_argument("path", help="Document path")
    sh.add_argument("--json", action="store_true", help="JSON document on stdout")
    return p


def _dispatch_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="search_txt_md")
    sub = p.add_subparsers(dest="cmd", required=True)
    idx = sub.add_parser("index", add_help=False, parents=[_index_parser()], help="Build index")
    idx.set_defaults(_handler="index")
    sch = sub.add_parser("search", add_help=False, parents=[_search_parser()], help="Search index")
    sch.set_defaults(_handler="search")
    tg = sub.add_parser("tag", add_help=False, parents=[_tag_parser()], help="Manage tags")
    tg.set_defaults(_handler="tag")
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
    tag_flags: list[str] = list(args.tags)
    if not query_text and not tag_flags:
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
        for name in tag_flags:
            validate_tag_name(name)
    except TagError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    compiled = None
    fts5: str | None = None
    tag_pred = None
    default_op = "OR" if args.default_or else "AND"
    if query_text:
        try:
            compiled = compile_query(query_text, default_op=default_op)
            fts5 = compiled.fts5
            tag_pred = compiled.tags
        except QuerySyntaxError as exc:
            print(f"query error: {exc}", file=sys.stderr)
            return 1
    try:
        tag_pred = and_tag_terms(tag_pred, tag_flags)
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
        for name in tag_flags:
            if get_tag_id(conn, name) is None:
                print(f"tag not found: {name} (txtmd-tag create {name})", file=sys.stderr)
                return 1
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
                tag_pred=tag_pred,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except sqlite3.Error as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.explain:
            if compiled is not None:
                print(f"default_op={compiled.default_op}", file=sys.stderr)
                print(f"ast={compiled.ast!r}", file=sys.stderr)
            else:
                print(f"default_op={default_op}", file=sys.stderr)
            print(f"fts5={fts5}", file=sys.stderr)
            print(f"tags={tag_pred!r}", file=sys.stderr)
            print(f"elapsed_ms={elapsed_ms:.2f}", file=sys.stderr)
            print(f"hits={len(hits)}", file=sys.stderr)
        if args.json:
            print(format_json(hits, query=query_text, fts5=fts5 or "", limit=args.limit))
        else:
            print(format_human(hits))
        return 0
    finally:
        conn.close()


def _open_writable_index(settings: Settings) -> tuple[sqlite3.Connection | None, int | None]:
    if not settings.index.is_file():
        print(f"index not found: {settings.index}; run txtmd-index", file=sys.stderr)
        return None, 2
    try:
        conn = connect(settings.index, writable=True)
        init_schema(conn)
        assert_searchable(conn)
        return conn, None
    except SchemaError as exc:
        print(str(exc), file=sys.stderr)
        return None, 2
    except (sqlite3.Error, OSError) as exc:
        print(f"cannot open index: {exc}", file=sys.stderr)
        return None, 3


def tag_main(argv: list[str] | None = None) -> int:
    parser = _tag_parser()
    args = parser.parse_args(argv)
    settings = Settings.resolve(root=args.root, index=args.index)
    conn, err = _open_writable_index(settings)
    if err is not None:
        return err
    assert conn is not None
    stored_root = meta_get(conn, "corpus_root")
    if args.root is not None:
        corpus_root = settings.root
    elif stored_root:
        corpus_root = Path(stored_root)
    else:
        corpus_root = None
    try:
        cmd = args.tag_cmd
        if cmd == "create":
            try:
                changes = create_tags(conn, args.names)
            except TagError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            conn.commit()
            for name, action in changes:
                print(f"{action} {name}")
            return 0
        if cmd == "delete":
            try:
                changes = delete_tags(conn, args.names)
            except TagError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            conn.commit()
            rc = 0
            for name, action in changes:
                print(f"{action} {name}")
                if action == "missing":
                    rc = 1
            return rc
        if cmd == "list":
            rows = list_tags(conn)
            if args.json:
                print(
                    json.dumps(
                        {"tags": [{"name": n, "count": c} for n, c in rows]},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif not rows:
                print("No tags.")
            else:
                width = max(len(n) for n, _c in rows)
                for name, count in rows:
                    print(f"{name:<{width}}  {count}")
            return 0
        if cmd in {"apply", "remove"}:
            if not args.paths and args.under is None:
                print("pass PATH or --under", file=sys.stderr)
                return 1
            fn = apply_tags if cmd == "apply" else remove_tags
            try:
                changes = fn(
                    conn,
                    args.name,
                    args.paths,
                    under=args.under,
                    corpus_root=corpus_root,
                )
            except TagError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            conn.commit()
            for path, action in changes:
                print(f"{action} {path}")
            return 0
        if cmd == "show":
            try:
                path, names = show_tags(conn, args.path, corpus_root=corpus_root)
            except TagError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            if args.json:
                print(
                    json.dumps(
                        {"path": path, "tags": names},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            elif not names:
                print("No tags.")
            else:
                print("\n".join(names))
            return 0
        print(f"unknown tag command {cmd!r}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(str(exc), file=sys.stderr)
        return 1
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
    if cmd == "tag":
        return tag_main(rest)
    print(f"unknown command {cmd!r}; use index, search, or tag", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
