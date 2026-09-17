from __future__ import annotations

import inspect
import sqlite3

import pytest

# query.py must not import sqlite3 (parser is pure).
import search_txt_md.query as query_mod
from search_txt_md.query import (
    And,
    Not,
    Or,
    QuerySyntaxError,
    TagNot,
    Term,
    compile_query,
    parse_query,
    partition,
)

assert "sqlite3" not in inspect.getsource(query_mod)


def fts5(text: str, *, default_op: str = "AND") -> str:
    return parse_query(text, default_op=default_op).to_fts5()


TRANSLATION = [
    ("naive", "AND", "naive"),
    ("naïve", "AND", "naïve"),
    ("grusch elizondo", "AND", "grusch AND elizondo"),
    ("grusch elizondo", "OR", "grusch OR elizondo"),
    ("grusch AND elizondo", "AND", "grusch AND elizondo"),
    ("grusch OR elizondo", "AND", "grusch OR elizondo"),
    ("(grusch OR elizondo) pasulka", "AND", "(grusch OR elizondo) AND pasulka"),
    ('"crash retrieval"', "AND", '"crash retrieval"'),
    ("pasulka*", "AND", "pasulka*"),
    ("uap NOT hoax", "AND", "uap NOT hoax"),
    ("uap -hoax", "AND", "uap NOT hoax"),
    ("uap AND NOT hoax", "AND", "uap NOT hoax"),
    ("grusch elizondo NOT hoax", "AND", "(grusch AND elizondo) NOT hoax"),
    ("grusch NOT hoax elizondo", "AND", "(grusch NOT hoax) AND elizondo"),
    ("grusch elizondo -hoax", "OR", "grusch OR (elizondo NOT hoax)"),
    ("grusch -hoax", "OR", "grusch NOT hoax"),
    ("a OR b NOT c", "AND", "a OR (b NOT c)"),
    ("a b NOT c", "OR", "a OR (b NOT c)"),
    ("(a OR b) NOT c", "AND", "(a OR b) NOT c"),
    ("uap AND NOT title:grusch", "AND", "uap NOT {title}: grusch"),
    ("uap NOT title:grusch", "AND", "uap NOT {title}: grusch"),
    ("title:grusch body:uap", "AND", "{title}: grusch AND {body}: uap"),
    ("Title:grusch", "AND", "{title}: grusch"),
    ("folder:UFO grusch", "AND", "{folder}: UFO AND grusch"),
    ("path:Grusch-hearing", "AND", '{stem}: "Grusch-hearing"'),
    ("O'Brien", "AND", '"O\'Brien"'),
    ("crash-retrieval", "AND", '"crash-retrieval"'),
    ('"AND"', "AND", '"AND"'),
]


@pytest.mark.parametrize("user,op,expected", TRANSLATION)
def test_translation_table(user: str, op: str, expected: str) -> None:
    assert fts5(user, default_op=op) == expected


ERRORS = [
    ("NOT hoax", "AND"),
    ("-hoax", "AND"),
    ("grusch OR NOT hoax", "AND"),
    ("-title:grusch", "AND"),
    ("uap -title:grusch", "AND"),
    ("uap AND -title:grusch", "AND"),
    ("title:(a OR b)", "AND"),
    ("NEAR(a b)", "AND"),
    ("body: foo) OR (title:bar", "AND"),
    ("", "AND"),
    ("   ", "AND"),
    ("; DROP TABLE documents", "AND"),
    ("^grusch", "AND"),
    ("{title}", "AND"),
    ('"unclosed', "AND"),
    ('tag:"a b"', "AND"),
    ("tag:foo*", "AND"),
]


@pytest.mark.parametrize("user,op", ERRORS)
def test_error_rows(user: str, op: str) -> None:
    with pytest.raises(QuerySyntaxError):
        parse_query(user, default_op=op)


def _mem_fts() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE VIRTUAL TABLE t USING fts5("
        "title, stem, folder, body, tokenize='unicode61 remove_diacritics 2')"
    )
    return conn


@pytest.mark.parametrize("user,op,expected", TRANSLATION)
def test_match_is_legal_fts5(user: str, op: str, expected: str) -> None:
    conn = _mem_fts()
    conn.execute(
        "INSERT INTO t(title, stem, folder, body) VALUES ('x','x','x','hello world')"
    )
    conn.execute("SELECT * FROM t WHERE t MATCH :fts5", {"fts5": expected})


def test_mixed_not_hits() -> None:
    conn = _mem_fts()
    conn.execute(
        "INSERT INTO t(rowid, title, stem, folder, body) VALUES (1,'g','g','g','grusch elizondo')"
    )
    conn.execute(
        "INSERT INTO t(rowid, title, stem, folder, body) VALUES (2,'h','h','h','grusch elizondo hoax')"
    )
    q = fts5("grusch elizondo NOT hoax")
    rows = {r[0] for r in conn.execute("SELECT rowid FROM t WHERE t MATCH :q", {"q": q})}
    assert 1 in rows
    assert 2 not in rows


def test_compile_tag_only() -> None:
    c = compile_query("tag:ufo")
    assert c.fts5 is None
    assert c.tags == Term("ufo", "word", "tag")
    with pytest.raises(QuerySyntaxError, match="no keyword"):
        parse_query("tag:ufo").to_fts5()


def test_compile_tag_and_keyword() -> None:
    c = compile_query("tag:ufo grusch")
    assert c.fts5 == "grusch"
    assert c.tags == Term("ufo", "word", "tag")


def test_compile_tag_or() -> None:
    c = compile_query("tag:ufo OR tag:physics")
    assert c.fts5 is None
    assert isinstance(c.tags, Or)


def test_mixed_or_is_error() -> None:
    with pytest.raises(QuerySyntaxError, match="cannot OR"):
        compile_query("tag:ufo OR grusch")
    with pytest.raises(QuerySyntaxError, match="cannot OR"):
        compile_query("grusch OR tag:ufo")


def test_partition_not_tag() -> None:
    ast = parse_query("grusch NOT tag:ufo").ast
    fts, tag = partition(ast)
    assert fts == Term("grusch", "word", None)
    assert tag == TagNot(Term("ufo", "word", "tag"))


def test_or_not_binding() -> None:
    conn = _mem_fts()
    conn.execute(
        "INSERT INTO t(rowid, title, stem, folder, body) VALUES (1,'a','a','a','apple only')"
    )
    conn.execute(
        "INSERT INTO t(rowid, title, stem, folder, body) VALUES (2,'b','b','b','banana carrot')"
    )
    q = fts5("apple OR banana NOT carrot")
    assert q == "apple OR (banana NOT carrot)"
    rows = {r[0] for r in conn.execute("SELECT rowid FROM t WHERE t MATCH :q", {"q": q})}
    assert 1 in rows
    assert 2 not in rows


def test_ast_shapes() -> None:
    ast = parse_query("grusch elizondo NOT hoax").ast
    assert isinstance(ast, Not)
    assert isinstance(ast.left, And)
    ast2 = parse_query("a OR b NOT c").ast
    assert isinstance(ast2, Or)
    assert isinstance(ast2.children[1], Not)
    ast3 = parse_query("uap -hoax").ast
    assert ast3 == Not(Term("uap", "word", None), Term("hoax", "word", None))
