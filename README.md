# search-txt-md

Index and boolean-search local podcast `.txt` / `.md` files with SQLite FTS5 BM25.

Two commands:

- `txtmd-index` — walk a corpus and incrementally upsert into a local SQLite index
- `txtmd-search` — evaluate a boolean keyword expression against that index, ranked by relevance

The default corpus is the Google Drive podcast folder. The index is stored **off Drive** at `~/Library/Application Support/search-txt-md/index.sqlite`. Search never reads the Drive tree.

## Install (macOS, Homebrew Python)

Always create the venv from Homebrew Python. `conda activate` can put a different `python3` first on `PATH`.

```bash
# BREWBIN=/opt/homebrew/bin  (set in ~/.zshenv)
$BREWBIN/python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e ".[dev]"
```

Commands live in `.venv/bin/txtmd-index` and `.venv/bin/txtmd-search`. Or `source .venv/bin/activate`.

Requires Python 3.14+ (Homebrew `python@3.14`) with SQLite FTS5. Do not `pip install` into `/opt/homebrew`.

## Index

```bash
txtmd-index --verbose
txtmd-index --dry-run
txtmd-index --full          # rebuild from readable files
```

| Flag | Meaning |
| --- | --- |
| `--root PATH` | Corpus directory (`TXTMD_ROOT` or the default Podcasts path) |
| `--index PATH` | SQLite file (`TXTMD_INDEX` or `~/Library/Application Support/search-txt-md/index.sqlite`) |
| `--full` | Ignore fingerprints; rebuild FTS. Evicted Drive placeholders cannot be re-read, so their stored bodies are dropped until they hydrate. Prefer incremental day-to-day. |
| `--dry-run` | Compare only. Never creates a missing index. |
| `--verbose` | Prints `open: relpath` on stderr **before** `open()`, and `skip dataless (not opening):` for File Provider placeholders |

Expect about **3556** files walked on the real corpus. A full index of hydrated files should finish in under ~30 s on an M4 Pro; a no-op incremental re-run under ~2 s.

The index is a **full-text copy** of your notes (`chmod 0600`). Time Machine may back it up.

If Drive is remounted under a new path, pass `--root` and `--full`.

### File Provider hangs

If the indexer appears stuck, `--verbose` has already printed the path it is about to open. Placeholders (`st_size > 0` and `st_blocks == 0`) are not opened and are **not** pruned: previously indexed bodies stay searchable.

## Search

```bash
txtmd-search grusch
txtmd-search naive AND pasulka --json
txtmd-search 'uap -hoax'
txtmd-search uap -- -hoax
txtmd-search '"crash retrieval"' --under UFO
txtmd-search grusch --ext md --limit 10
```

`--under` is case-insensitive (`ufo` matches `UFO/`). Trailing slashes are stripped.

Missing index → exit 2 (run `txtmd-index`). Empty query → exit 1.

### Query language

Default operator is **AND**. `--or` makes juxtaposition OR. `NOT` is binary and tighter than OR.

| Typed | Meaning |
| --- | --- |
| `grusch elizondo` | `grusch AND elizondo` |
| `grusch OR elizondo` | either |
| `uap NOT hoax` | `uap` minus `hoax` |
| `uap -hoax` | same (must be quoted or after `--`) |
| `(grusch OR elizondo) pasulka` | group then AND |
| `"crash retrieval"` | phrase |
| `pasulka*` | prefix |
| `title:grusch` | title column |
| `folder:UFO` | folder column |
| `path:Grusch-hearing` | filename **stem** (not the `.txt` extension) |
| `a OR b NOT c` | `a OR (b NOT c)` |
| `(a OR b) NOT c` | NOT applies to the group |

`txtmd-search uap -hoax` is an argparse error (`-hoax` looks like a flag). Use:

```bash
txtmd-search 'uap -hoax'
txtmd-search uap -- -hoax
```

`txtmd-search` has no short flags (not even `-h`, because `-hoax` would otherwise be parsed as help). Use `--help`. `txtmd-index -h` still works.

`--explain` prints the AST, FTS5 string, and elapsed ms on stderr.

BM25 scores are **more negative = better**, and only comparable within one query.

Query `txt` does not match every `.txt` file; extensions are not indexed as tokens.

## Tests

```bash
.venv/bin/pytest
```

Tests use a tiny fixture corpus. They refuse to walk the real Google Drive folder.

## Uninstall

```bash
rm -rf ~/Library/Application\ Support/search-txt-md
rm -rf .venv
```

The corpus is never modified.
