from __future__ import annotations

from dataclasses import dataclass

_FIELD_NAMES = {"title", "path", "folder", "body"}
_FIELD_TO_COL = {"title": "title", "path": "stem", "folder": "folder", "body": "body"}
_BAD_WORD_CHARS = frozenset("{}^;")
_OPERATORS = {"and": "AND", "or": "OR", "not": "NOT"}


class QuerySyntaxError(ValueError):
    """User query cannot be translated to legal FTS5 MATCH."""


@dataclass(frozen=True)
class Term:
    value: str
    kind: str  # "word" | "phrase" | "prefix"
    field: str | None  # None or "title"|"path"|"folder"|"body"


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


Node = Term | And | Or | Not


@dataclass(frozen=True)
class ParsedQuery:
    ast: Node
    default_op: str

    def to_fts5(self) -> str:
        return _emit(self.ast)


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
    raise TypeError(f"unknown node {type(node)!r}")
