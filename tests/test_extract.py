from __future__ import annotations

from pathlib import Path

from search_txt_md.extract import extract_document, read_text


def test_title_prefers_Title(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("title: lower\nTitle: Upper\n\nbody\n", encoding="utf-8")
    doc = extract_document(p, "a.txt", "txt")
    assert doc.title == "Upper"


def test_title_lowercase_fallback(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("title: only lower\ntitleUrl: https://ex.com\n\nbody\n", encoding="utf-8")
    doc = extract_document(p, "a.txt", "txt")
    assert doc.title == "only lower"
    assert doc.url == "https://ex.com"


def test_markdown_h1(tmp_path: Path) -> None:
    p = tmp_path / "n.md"
    p.write_text("# Heading One\n\nparagraph\n", encoding="utf-8")
    doc = extract_document(p, "n.md", "md")
    assert doc.title == "Heading One"


def test_fallback_stem(tmp_path: Path) -> None:
    p = tmp_path / "no-header.txt"
    p.write_text("just body text\n", encoding="utf-8")
    doc = extract_document(p, "no-header.txt", "txt")
    assert doc.title == "no-header"


def test_utf8_sig_strips_bom(tmp_path: Path) -> None:
    p = tmp_path / "bom.txt"
    p.write_bytes(b"\xef\xbb\xbfhello")
    text, label = read_text(p)
    assert text == "hello"
    assert label == "utf-8-sig"
    assert "\ufeff" not in text


def test_folder_and_stem(tmp_path: Path) -> None:
    p = tmp_path / "UFO" / "talk.txt"
    p.parent.mkdir()
    p.write_text("Title: Talk\n\nbody\n", encoding="utf-8")
    doc = extract_document(p, "UFO/talk.txt", "txt")
    assert doc.folder == "UFO"
    assert doc.stem == "talk"
    assert doc.ext == "txt"
