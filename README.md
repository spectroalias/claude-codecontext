# codecontext

- [ ] Tested results added to description

Generate a lean code map of any repo so Claude can load it as session context instead of re-reading raw files each time.

## Usage

```bash
python3 codecontext.py                        # map current directory
python3 codecontext.py /path/to/repo          # map a specific repo
python3 codecontext.py /path/to/repo -o map.md  # custom output path
python3 codecontext.py . --max-depth 3        # shallower directory tree
```

Output is written to `.claude/codecontext.md` inside the target repo by default.

## Wiring it up with Claude

Add this to the repo's `CLAUDE.md`:

```markdown
At the start of every session, read `.claude/codecontext.md` for the full code map.
```

Regenerate the map whenever the codebase changes significantly:

```bash
python3 /path/to/codecontext.py .
```

## Output format

The generated file has three sections:

**Directory tree** — visual file tree, depth-limited, with all build artifacts and dependency folders stripped out.

**Config summaries** — one-liner per config file (`package.json`, `pyproject.toml`, `Cargo.toml`, `Dockerfile`, etc.) with name, version, and key dependencies.

**Symbol index** — per source file: all top-level symbols grouped by kind, with line numbers and imports.

```
### `src/auth/login.py`
  fn: `authenticate` L12, `hash_password` L28 | class: `LoginForm` L60
  imports: os · hashlib · flask
```

## How it works

The script has six stages.

### 1. Configuration

Four constant sets control what gets processed:

- `IGNORE_DIRS` — directory names to never descend into (build artifacts, caches, dependency folders across all ecosystems). Checked by exact name.
- `IGNORE_FILE_PATTERNS` — glob patterns for files to skip (`*.min.js`, `*.png`, lock files, binaries, etc.).
- `SOURCE_EXTENSIONS` — extensions worth extracting symbols from (`.py`, `.ts`, `.go`, `.rs`, and more).
- `CONFIG_NAMES` — specific filenames that get a special summary instead of symbol extraction.

Four numeric limits cap output size so the map stays compact even on enormous repos:
`MAX_FILE_BYTES`, `SYMBOL_SCAN_LINES`, `MAX_SYMBOLS_PER_FILE`, `MAX_IMPORTS_PER_FILE`.

### 2. Symbol extraction

Two regex lists drive the core extraction:

**`_SYM`** — one pattern per language construct, each capturing the symbol name in group 1:

```python
(re.compile(r"^(?:async\s+)?def\s+(\w+)\s*\("), "fn"),                          # Python
(re.compile(r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\("), "fn"),             # Go
(re.compile(r"^(?:pub(?:\(\w+\))?\s+)?(?:async\s+)?fn\s+(\w+)"), "fn"),        # Rust
```

**`_IMP`** — one pattern per import style:

```python
re.compile(r"^from\s+(\S+)\s+import"),                                          # Python
re.compile(r"""^import\s+.*?from\s+['"]([^'"]+)['"]"""),                        # JS/TS
re.compile(r"^use\s+([\w:]+)"),                                                 # Rust
```

`_extract(path, text)` processes each file line by line:

1. Skips blank lines and lines starting with comment or decorator characters (`#`, `//`, `@`, etc.).
2. Checks indentation — only processes lines with ≤4 spaces of indent to filter out nested closures and inner functions.
3. Tries each `_SYM` pattern in order; first match wins. Deduplicates by name.
4. Tries each `_IMP` pattern; shortens the result to the first meaningful segment (`from pathlib import Path` → `pathlib`).

### 3. .gitignore support

`_load_gitignore` reads the repo's `.gitignore` and returns all non-comment patterns. `_gitignore_match` checks each file against those patterns twice — once against the full relative path and once against just the filename — matching `.gitignore`'s own semantics.

### 4. Directory tree

`_build_tree` recurses through the directory and renders the tree using box-drawing characters:

```
my-repo/
├── src/
│   ├── auth.py
│   └── models/
│       └── user.py
└── tests/
    └── test_auth.py
```

Entries are sorted with directories first. Each entry gets `├── ` or `└── ` depending on whether more siblings follow. When `depth > max_depth` it prints `...` and stops recursing.

### 5. Config file summarizer

`_summarize_config` handles each config type specifically:

- `package.json` → parses JSON, extracts `name`, `version`, `description`, first 8 `dependencies` and `devDependencies`
- `pyproject.toml` → regex scan for `name =`, `version =`, `description =`
- `Cargo.toml` / `go.mod` → reads the first 5 lines for `module`/`name`/`version`
- `requirements.txt` → strips version pins to get bare package names
- `Dockerfile` → finds all `FROM` lines (base images)

Returns a compact one-liner or `None` if nothing useful was found.

### 6. File walker and output assembly

`_collect_files` uses `os.walk` and prunes ignored directories by mutating `dirnames` in-place:

```python
dirnames[:] = [d for d in sorted(dirnames) if d not in ignore_dirs ...]
```

This tells `os.walk` not to descend into those directories at all. Each surviving file is size-checked and bucketed into `source_files` or `config_files`.

`generate` assembles the final markdown in order: header → directory tree → config summaries → symbol index. `_format_symbols` groups the flat symbol list by kind:

```
fn: `auth` L12, `logout` L28 | class: `User` L45 | type: `Role` L60
```

### Data flow

```
main()
  └─ generate(root, output, max_depth)
       ├─ _load_gitignore()           → gi_patterns
       ├─ _collect_files()            → source_files, config_files
       ├─ _build_tree()               → directory tree string
       ├─ _summarize_config()         → one-liner per config file
       └─ _extract() per source file  → symbols + imports
            ├─ _SYM regex list        → symbol name + kind + line
            └─ _IMP regex list        → import module names
```

The whole thing is stateless and single-pass — no caching, no AST, just regex over raw text. It's fast and has zero dependencies, but it can miss things that require a real parser (e.g. dynamically assigned exports in JS).
