from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path(
    "/Users/mauriziocarioli/Library/CloudStorage/"
    "GoogleDrive-maurizio.carioli@gmail.com/My Drive/_Uploaded/_Podcasts"
)
DEFAULT_INDEX = Path.home() / "Library/Application Support/search-txt-md/index.sqlite"
SCHEMA_VERSION = 2


class SchemaError(RuntimeError):
    """Index schema is missing or the wrong version."""


@dataclass(frozen=True)
class Settings:
    root: Path
    index: Path

    @classmethod
    def resolve(cls, *, root: Path | None, index: Path | None) -> Settings:
        if root is None:
            env = os.environ.get("TXTMD_ROOT")
            root = Path(env) if env else DEFAULT_ROOT
        if index is None:
            env = os.environ.get("TXTMD_INDEX")
            index = Path(env) if env else DEFAULT_INDEX
        return cls(root=root.expanduser().resolve(), index=index.expanduser().resolve())

    def ensure_index_parent(self) -> None:
        parent = self.index.parent
        parent.mkdir(parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
