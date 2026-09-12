from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from search_txt_md.config import DEFAULT_ROOT

SKIP_DIRNAMES = {".git", ".Trash", "__pycache__", ".Spotlight-V100"}
EXCLUDED_NAMES = {".DS_Store", ".DS Store"}


@dataclass(frozen=True)
class CrawlItem:
    abs_path: Path
    relpath: str
    ext: str
    size: int
    mtime_ns: int
    dataless: bool
    warning: str | None


def _guard_test_root(root: Path) -> None:
    if os.environ.get("TXTMD_TEST") != "1":
        return
    resolved = root.resolve()
    try:
        default = DEFAULT_ROOT.resolve()
    except OSError:
        default = DEFAULT_ROOT
    if resolved == default or "CloudStorage" in resolved.parts:
        raise RuntimeError(f"tests must not walk the real corpus: {resolved}")


def is_dataless_placeholder(st: os.stat_result) -> bool:
    return st.st_size > 0 and st.st_blocks == 0


def classify_name(name: str) -> tuple[str, str | None] | None:
    """Return (ext, warning) or None if the name is not indexable."""
    if name in EXCLUDED_NAMES or name.startswith("."):
        return None
    stripped = name.rstrip()
    lower = stripped.lower()
    warning = "trailing whitespace in filename" if name != stripped else None
    if lower.endswith(".txt"):
        return "txt", warning
    if lower.endswith(".md"):
        return "md", warning
    suffix = Path(name).suffix
    if suffix == "" and not name.startswith("."):
        if lower.endswith("txt"):
            w = "missing-dot extension" if warning is None else warning
            return "txt", w
        if lower.endswith("md"):
            w = "missing-dot extension" if warning is None else warning
            return "md", w
    return None


def filename_stem(name: str, ext: str) -> str:
    classified = name.rstrip()
    lower = classified.lower()
    dotted = f".{ext}"
    if lower.endswith(dotted):
        return classified[: -len(dotted)]
    if lower.endswith(ext):
        return classified[: -len(ext)]
    return classified


def iter_corpus(root: Path) -> Iterator[CrawlItem]:
    """Walk files. Yield every indexable name, including dataless placeholders.

    Never follow symlinks. Never open().
    """
    _guard_test_root(root)
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in SKIP_DIRNAMES
            and not d.startswith(".")
            and not os.path.islink(os.path.join(dirpath, d))
        ]
        for name in filenames:
            classified = classify_name(name)
            if classified is None:
                continue
            ext, warning = classified
            abs_path = Path(dirpath, name)
            if os.path.islink(abs_path):
                continue
            try:
                st = os.lstat(abs_path)
            except OSError:
                continue
            relpath = abs_path.relative_to(root).as_posix()
            yield CrawlItem(
                abs_path=abs_path,
                relpath=relpath,
                ext=ext,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                dataless=is_dataless_placeholder(st),
                warning=warning,
            )
