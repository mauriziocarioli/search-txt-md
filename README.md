# search-txt-md

Index and boolean-search local podcast `.txt` / `.md` files with SQLite FTS5 BM25. Tags live in the index, not in the corpus.

Three commands:

- `txtmd-index` — walk a corpus and incrementally upsert into a local SQLite index
- `txtmd-search` — evaluate a boolean keyword expression against that index, ranked by relevance
- `txtmd-tag` — create tags and apply them to indexed documents

The default corpus is the Google Drive podcast folder. The index is stored **off Drive** at `~/Library/Application Support/search-txt-md/index.sqlite`. Search never reads the Drive tree.

## Install (macOS, Homebrew Python)

Always create the venv from Homebrew Python. `conda activate` can put a different `python3` first on `PATH`.

```bash
# BREWBIN=/opt/homebrew/bin  (set in ~/.zshenv)
$BREWBIN/python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e ".[dev]"
```

Commands live in `.venv/bin/txtmd-index`, `.venv/bin/txtmd-search`, and `.venv/bin/txtmd-tag`. Or `source .venv/bin/activate`.

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
| `--full` | Ignore fingerprints; rebuild FTS. Evicted Drive placeholders cannot be re-read, so their stored bodies are dropped until they hydrate. Tag assignments are keyed by path and survive for files that are still indexed. Prefer incremental day-to-day. |
| `--dry-run` | Compare only. Never creates a missing index. |
| `--verbose` | Prints `open: relpath` on stderr **before** `open()`, and `skip dataless (not opening):` for File Provider placeholders |

Expect about **3556** files walked on the real corpus. A full index of hydrated files should finish in under ~30 s on an M4 Pro; a no-op incremental re-run under ~2 s.

The index is a **full-text copy** of your notes (`chmod 0600`). Time Machine may back it up.

If Drive is remounted under a new path, pass `--root` and `--full`.

A v1 index is migrated in place on the next `txtmd-index` (or `txtmd-tag`) run. No `--full` needed.

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
txtmd-search --tag ufo
txtmd-search tag:ufo grusch
txtmd-search 'tag:ufo OR tag:physics'
```

`--under` is case-insensitive (`ufo` matches `UFO/`). Trailing slashes are stripped.

Missing index → exit 2 (run `txtmd-index`). Empty query and no `--tag` → exit 1. Tag-only search (`--tag ufo` or `tag:ufo`) is allowed; BM25 scores are `0` and hits are sorted by path.

Unknown `--tag` name → exit 1. Unknown `tag:` in the query language → zero hits (same as `folder:nope`).

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
| `tag:ufo` | documents with that tag (not FTS) |
| `tag:ufo grusch` | tagged **and** keyword |
| `tag:ufo OR tag:physics` | either tag |
| `grusch NOT tag:ufo` | keyword minus that tag |
| `a OR b NOT c` | `a OR (b NOT c)` |
| `(a OR b) NOT c` | NOT applies to the group |

`tag:ufo OR grusch` is an error: a tag filter cannot be OR'd with a keyword. AND them, or OR tags with each other. `tag:` takes a word, not a phrase or `*`. Repeatable `--tag NAME` is ANDed with the query (and with other `--tag` flags).

Hits show a `Tags:` line (JSON: `"tags"`) when the document has tags.

`txtmd-search uap -hoax` is an argparse error (`-hoax` looks like a flag). Use:

```bash
txtmd-search 'uap -hoax'
txtmd-search uap -- -hoax
```

`txtmd-search` has no short flags (not even `-h`, because `-hoax` would otherwise be parsed as help). Use `--help`. `txtmd-index -h` still works.

`--explain` prints the AST, FTS5 string, tag predicate, and elapsed ms on stderr.

BM25 scores are **more negative = better**, and only comparable within one keyword query. Tag-only results are not BM25-ranked.

Query `txt` does not match every `.txt` file; extensions are not indexed as tokens.

## Tags

Tags are stored in the index. The corpus is never modified. Create a tag before applying it.

```bash
txtmd-tag create ufo physics
txtmd-tag apply ufo --under UFO
txtmd-tag apply physics Physics/tokamak-seminar.txt
txtmd-tag list
txtmd-tag show UFO/Grusch-hearing.txt
txtmd-tag remove ufo UFO/Elizondo-interview.txt
txtmd-tag delete physics
```

| Command | Meaning |
| --- | --- |
| `create NAME…` | Add catalog entries. Duplicate names print `exists` (case-insensitive). |
| `delete NAME…` | Drop tags and their assignments. Missing names print `missing` and exit 1. |
| `list` | Names with assignment counts. `--json` supported. |
| `apply NAME PATH…` | Tag indexed documents. Idempotent. `--under DIR` tags every indexed path in that subtree. |
| `remove NAME PATH…` | Untag documents; the catalog entry stays. `--under` works as in apply. |
| `show PATH` | Tags on one document. `--json` supported. |

Names: 1–64 characters, letters/digits/`-`/`_`, no leading `-`, unique ignoring case. Paths are `documents.path` (or absolute under the corpus root).

`--index` / `--root` match the other commands. `txtmd-tag -h` works (unlike `txtmd-search`).

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
