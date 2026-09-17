from __future__ import annotations

from dataclasses import dataclass

_FIELD_NAMES = {"title", "path", "folder", "body", "tag"}
_FIELD_TO_COL = {"title": "title", "path": "stem", "folder": "folder", "body": "body"}
_BAD_WORD_CHARS = frozenset("{}^;")
_OPERATORS = {"and": "AND", "or": "OR", "not": "NOT"}


class QuerySyntaxError(ValueError):
    """User query cannot be translated to legal FTS5 MATCH."""


@dataclass(frozen=True)
class Term:
    value: str
    kind: str  # "word" | "phrase" | "prefix"
    field: str | None  # None or "title"|"path"|"folder"|"body"|"tag"


@dataclass(frozen=True)
class And:
    children: tuple[Node, ...]


@dataclass(frozen=True)
class Or:
    children: tuple[Node, ...]


@dataclass(frozen=True)
class Not:
    left: Node
    right: Node


@dataclass(frozen=True)
class TagNot:
    """Unary NOT for tag predicates (NOT EXISTS). Produced by partition, not the parser."""

    child: Node


Node = Term | And | Or | Not | TagNot


@dataclass(frozen=True)
class ParsedQuery:
    ast: Node
    default_op: str

    def to_fts5(self) -> str:
        fts_ast, _tag_ast = partition(self.ast)
        if fts_ast is None:
            raise QuerySyntaxError("query has no keyword terms")
        return _emit(fts_ast)


@dataclass(frozen=True)
class _Tok:
    kind: str
    value: str
    pos: int


def _tokenize(text: str) -> list[_Tok]:
    tokens: list[_Tok] = []
    i = 0
    n = len(text)

    def skip_ws() -> int:
        nonlocal i
        while i < n and text[i].isspace():
            i += 1
        return i

    while True:
        start = skip_ws()
        if i >= n:
            tokens.append(_Tok("EOF", "", start))
            return tokens
        ch = text[i]
        if ch == "(":
            tokens.append(_Tok("LPAREN", ch, start))
            i += 1
            continue
        if ch == ")":
            tokens.append(_Tok("RPAREN", ch, start))
            i += 1
            continue
        if ch == ":":
            tokens.append(_Tok("COLON", ch, start))
            i += 1
            continue
        if ch == "*":
            tokens.append(_Tok("STAR", ch, start))
            i += 1
            continue
        if ch == '"':
            i += 1
            parts: list[str] = []
            closed = False
            while i < n:
                if text[i] == '"':
                    if i + 1 < n and text[i + 1] == '"':
                        parts.append('"')
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                parts.append(text[i])
                i += 1
            if not closed:
                raise QuerySyntaxError("unclosed quote")
            tokens.append(_Tok("PHRASE", "".join(parts), start))
            continue
        if ch == "-":
            tokens.append(_Tok("MINUS", "-", start))
            i += 1
            continue
        j = i
        while j < n and not text[j].isspace() and text[j] not in '()":*':
            j += 1
        if j == i:
            raise QuerySyntaxError(f"unexpected character {ch!r}")
        word = text[i:j]
        i = j
        low = word.lower()
        if low == "near":
            raise QuerySyntaxError("NEAR is not supported")
        if low in _OPERATORS:
            tokens.append(_Tok(_OPERATORS[low], word, start))
        else:
            tokens.append(_Tok("WORD", word, start))


def _and_merge(acc: Node, nxt: Node) -> And:
    if isinstance(acc, And):
        return And(acc.children + (nxt,))
    return And((acc, nxt))


def _or_merge(acc: Node, nxt: Node) -> Or:
    if isinstance(acc, Or):
        return Or(acc.children + (nxt,))
    return Or((acc, nxt))


class _Parser:
    def __init__(self, tokens: list[_Tok], default_op: str) -> None:
        self.tokens = tokens
        self.i = 0
        self.default_op = default_op

    def peek(self) -> _Tok:
        return self.tokens[self.i]

    def kind(self) -> str:
        return self.peek().kind

    def accept(self, *kinds: str) -> _Tok | None:
        if self.kind() in kinds:
            tok = self.peek()
            self.i += 1
            return tok
        return None

    def expect(self, kind: str) -> _Tok:
        tok = self.accept(kind)
        if tok is None:
            raise QuerySyntaxError(f"expected {kind}, got {self.kind()}")
        return tok

    def _starts_primary(self) -> bool:
        return self.kind() in {"LPAREN", "WORD", "PHRASE"}

    def _is_field_colon(self) -> bool:
        tok = self.peek()
        if tok.kind != "WORD" or tok.value.lower() not in _FIELD_NAMES:
            return False
        nxt = self.tokens[self.i + 1] if self.i + 1 < len(self.tokens) else None
        return nxt is not None and nxt.kind == "COLON"

    def parse_or_expr(self) -> Node:
        acc = self.parse_and_expr()
        while True:
            if self.accept("OR"):
                if self.kind() in {"NOT", "MINUS"}:
                    raise QuerySyntaxError(
                        "OR NOT is not valid FTS5; write '(a OR b) NOT c'"
                    )
                acc = _or_merge(acc, self.parse_and_expr())
            elif self.default_op == "OR" and self._starts_primary():
                acc = _or_merge(acc, self.parse_and_expr())
            else:
                return acc

    def parse_and_expr(self) -> Node:
        if self.kind() in {"NOT", "MINUS"}:
            raise QuerySyntaxError(
                "NOT requires a term to the left (FTS5 cannot search 'everything except X')"
            )
        acc = self.parse_primary()
        while True:
            if self.accept("AND"):
                if self.accept("NOT"):
                    acc = Not(acc, self.parse_primary())
                elif self.accept("MINUS"):
                    acc = Not(acc, self.parse_minus_term())
                else:
                    acc = _and_merge(acc, self.parse_primary())
            elif self.accept("NOT"):
                acc = Not(acc, self.parse_primary())
            elif self.accept("MINUS"):
                acc = Not(acc, self.parse_minus_term())
            elif self.default_op == "AND" and self._starts_primary():
                acc = _and_merge(acc, self.parse_primary())
            else:
                return acc

    def parse_primary(self) -> Node:
        if self.accept("LPAREN"):
            inner = self.parse_or_expr()
            self.expect("RPAREN")
            return inner
        return self.parse_field_term()

    def parse_field_term(self) -> Term:
        if self._is_field_colon():
            field = self.expect("WORD").value.lower()
            self.expect("COLON")
            term = self.parse_term()
            if field == "tag" and term.kind != "word":
                raise QuerySyntaxError("tag: requires a word, not a phrase or prefix")
            return Term(term.value, term.kind, field)
        return self.parse_term()

    def parse_minus_term(self) -> Term:
        if self._is_field_colon():
            raise QuerySyntaxError(
                "'-title:' is FTS5 column exclusion; write 'NOT title:grusch'"
            )
        return self.parse_term()

    def parse_term(self) -> Term:
        if self.kind() == "WORD":
            tok = self.expect("WORD")
            _reject_bad_word(tok.value)
            value = tok.value
            kind = "word"
        elif self.kind() == "PHRASE":
            tok = self.expect("PHRASE")
            value = tok.value
            kind = "phrase"
        else:
            raise QuerySyntaxError("expected a term")
        if self.accept("STAR"):
            kind = "prefix"
        return Term(value, kind, None)


def _reject_bad_word(value: str) -> None:
    if any(c in _BAD_WORD_CHARS for c in value):
        raise QuerySyntaxError(f"unexpected character in term {value!r}")


def parse_query(text: str, *, default_op: str = "AND") -> ParsedQuery:
    if default_op not in {"AND", "OR"}:
        raise ValueError(f"default_op must be AND or OR, not {default_op!r}")
    if not text or not text.strip():
        raise QuerySyntaxError("empty query")
    tokens = _tokenize(text)
    parser = _Parser(tokens, default_op)
    ast = parser.parse_or_expr()
    if parser.kind() != "EOF":
        raise QuerySyntaxError(f"unexpected token {parser.peek().kind}")
    return ParsedQuery(ast=ast, default_op=default_op)


def is_bareword(s: str) -> bool:
    if not s:
        return False
    for c in s:
        o = ord(c)
        if o > 127 or o == 0x1A:
            continue
        if ("A" <= c <= "Z") or ("a" <= c <= "z") or ("0" <= c <= "9") or c == "_":
            continue
        return False
    return True


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _emit_term(term: Term) -> str:
    if term.field == "tag":
        raise QuerySyntaxError("tag: cannot be emitted as FTS5")
    if term.kind != "phrase" and is_bareword(term.value):
        tok = term.value
    else:
        tok = _quote(term.value)
    if term.kind == "prefix":
        tok += "*"
    if term.field is None:
        return tok
    col = _FIELD_TO_COL[term.field]
    return "{" + col + "}: " + tok


def _atom(node: Node) -> str:
    if isinstance(node, Term):
        return _emit_term(node)
    return "(" + _emit(node) + ")"


def _emit(node: Node) -> str:
    if isinstance(node, Term):
        return _emit_term(node)
    if isinstance(node, And):
        return " AND ".join(_atom(c) for c in node.children)
    if isinstance(node, Or):
        return " OR ".join(_atom(c) for c in node.children)
    if isinstance(node, Not):
        return f"{_atom(node.left)} NOT {_atom(node.right)}"
    if isinstance(node, TagNot):
        raise TypeError("TagNot cannot be emitted as FTS5")
    raise TypeError(f"unknown node {type(node)!r}")


_MIXED_OR = "cannot OR a tag: filter with a keyword; AND them or OR tags with each other"
_MIXED_NOT = "NOT cannot mix a tag: filter with a keyword on the right"
_NOT_KEYWORD = "NOT requires a keyword term to the left when excluding a keyword"


def _and_nodes(nodes: list[Node]) -> Node | None:
    if not nodes:
        return None
    if len(nodes) == 1:
        return nodes[0]
    return And(tuple(nodes))


def _or_nodes(nodes: list[Node]) -> Node | None:
    if not nodes:
        return None
    if len(nodes) == 1:
        return nodes[0]
    return Or(tuple(nodes))


def partition(node: Node) -> tuple[Node | None, Node | None]:
    """Split an AST into (fts_ast, tag_ast). Either side may be None."""
    if isinstance(node, TagNot):
        fts, tag = partition(node.child)
        if fts is not None:
            raise QuerySyntaxError(_MIXED_NOT)
        if tag is None:
            return None, None
        return None, TagNot(tag)
    if isinstance(node, Term):
        if node.field == "tag":
            return None, node
        return node, None
    if isinstance(node, And):
        fts_parts: list[Node] = []
        tag_parts: list[Node] = []
        for child in node.children:
            fts, tag = partition(child)
            if fts is not None:
                fts_parts.append(fts)
            if tag is not None:
                tag_parts.append(tag)
        return _and_nodes(fts_parts), _and_nodes(tag_parts)
    if isinstance(node, Or):
        parts = [partition(child) for child in node.children]
        if any(f is not None and t is not None for f, t in parts):
            raise QuerySyntaxError(_MIXED_OR)
        has_fts = any(f is not None for f, _t in parts)
        has_tag = any(t is not None for _f, t in parts)
        if has_fts and has_tag:
            raise QuerySyntaxError(_MIXED_OR)
        if has_fts:
            return _or_nodes([f for f, _t in parts if f is not None]), None
        return None, _or_nodes([t for _f, t in parts if t is not None])
    if isinstance(node, Not):
        lf, lt = partition(node.left)
        rf, rt = partition(node.right)
        if rf is not None and rt is not None:
            raise QuerySyntaxError(_MIXED_NOT)
        if rf is not None:
            if lf is None:
                raise QuerySyntaxError(_NOT_KEYWORD)
            return Not(lf, rf), lt
        if rt is None:
            return lf, lt
        if lt is None:
            return lf, TagNot(rt)
        return lf, Not(lt, rt)
    raise TypeError(f"unknown node {type(node)!r}")


@dataclass(frozen=True)
class CompiledQuery:
    ast: Node
    default_op: str
    fts5: str | None
    tags: Node | None

    @property
    def has_fts(self) -> bool:
        return self.fts5 is not None

    @property
    def has_tags(self) -> bool:
        return self.tags is not None


def compile_query(text: str, *, default_op: str = "AND") -> CompiledQuery:
    parsed = parse_query(text, default_op=default_op)
    fts_ast, tag_ast = partition(parsed.ast)
    fts5 = _emit(fts_ast) if fts_ast is not None else None
    return CompiledQuery(ast=parsed.ast, default_op=parsed.default_op, fts5=fts5, tags=tag_ast)


def and_tag_terms(tag_ast: Node | None, names: list[str]) -> Node | None:
    extra = [Term(n, "word", "tag") for n in names]
    if not extra:
        return tag_ast
    if tag_ast is None:
        return extra[0] if len(extra) == 1 else And(tuple(extra))
    return And((tag_ast, *extra))
