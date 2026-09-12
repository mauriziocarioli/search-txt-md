from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from search_txt_md.crawl import filename_stem

MAX_FILE_BYTES = 8 * 1024 * 1024
_ATX = re.compile(r"^#{1,6}\s+(.*)")


@dataclass(frozen=True)
class Document:
    relpath: str
    ext: str
    title: str
    folder: str
    stem: str
    url: str | None
    body: str


def read_text(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("too large")
    try:
        text = data.decode("utf-8-sig")
        label = "utf-8-sig"
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
        label = "cp1252"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text, label


def _txt_title_url(text: str) -> tuple[str | None, str | None]:
    title_exact: str | None = None
    title_lower: str | None = None
    url_exact: str | None = None
    url_alt: str | None = None
    for line in text.splitlines()[:30]:
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        if not val:
            continue
        if key == "Title" and title_exact is None:
            title_exact = val
        elif key == "title" and title_lower is None:
            title_lower = val
        elif key == "URL" and url_exact is None:
            url_exact = val
        elif key == "titleUrl" and url_alt is None:
            url_alt = val
    return title_exact or title_lower, url_exact or url_alt


def _md_title(text: str) -> str | None:
    lines = text.splitlines()[:40]
    for line in lines:
        m = _ATX.match(line)
        if m:
            heading = m.group(1).strip()
            if heading:
                return heading
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("*", "-")):
            continue
        return stripped
    return None


def extract_document(path: Path, relpath: str, ext: str) -> Document:
    text, _encoding = read_text(path)
    name = Path(relpath).name
    stem = filename_stem(name, ext)
    parent = Path(relpath).parent.as_posix()
    folder = "" if parent == "." else parent
    url: str | None = None
    if ext == "txt":
        title, url = _txt_title_url(text)
    else:
        title = _md_title(text)
    if not title:
        title = stem
    return Document(
        relpath=relpath,
        ext=ext,
        title=title,
        folder=folder,
        stem=stem,
        url=url,
        body=text,
    )
