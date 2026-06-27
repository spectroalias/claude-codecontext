#!/usr/bin/env python3
"""
codecontext — generate a lean code map for Claude to use as session context.

Usage:
    python codecontext.py                      # maps current directory
    python codecontext.py /path/to/repo        # maps given directory
    python codecontext.py /path/to/repo --output map.md
    python codecontext.py --max-depth 4        # limit directory tree depth
"""

import os
import re
import sys
import json
import fnmatch
import argparse
from pathlib import Path
from datetime import datetime


# ── ignore config ─────────────────────────────────────────────────────────────

IGNORE_DIRS = {
    # Version control
    ".git", ".svn", ".hg", ".bzr",

    # JavaScript / TypeScript / Node
    "node_modules", "bower_components",
    ".next", ".nuxt", ".svelte-kit", ".solid", ".remix", ".astro",
    ".turbo", ".parcel-cache", ".yarn",

    # Python
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".pytype", ".pyre",
    "venv", ".venv", "env", ".env",
    ".tox", ".nox",
    "htmlcov", "site-packages",
    "eggs", ".eggs",

    # Rust
    ".cargo",

    # Java / Kotlin / Scala / Android
    ".gradle", ".mvn",
    "gen",

    # C / C++
    "CMakeFiles", "cmake-build-debug", "cmake-build-release",

    # Ruby
    ".bundle",

    # Swift / Xcode / CocoaPods / Carthage
    "Pods", "Carthage", "DerivedData",

    # Elixir / Erlang
    "_build", "deps",

    # Haskell
    ".stack-work", "dist-newstyle",

    # Dart / Flutter
    ".dart_tool",

    # R
    ".Rproj.user",

    # .NET
    "obj", "packages", ".nuget",

    # Infrastructure / cloud
    ".terraform", "cdk.out", ".serverless", ".pulumi",

    # Build output (shared across ecosystems)
    "dist", "build", "out", "target", "output", ".output", "bin",

    # Coverage / test artifacts
    "coverage", ".nyc_output",

    # IDE / editor
    ".idea", ".vscode", ".eclipse", ".settings",

    # Generic cache
    ".cache", ".build",

    # Shared dependency dirs
    "vendor",

    # Temporary / logs
    "tmp", "temp", "logs",

    # macOS archive artifacts
    "__MACOSX",
}

IGNORE_FILE_PATTERNS = [
    "*.min.js", "*.min.css", "*.map",
    "*.pyc", "*.pyo", "*.pyd",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.webp",
    "*.pdf", "*.zip", "*.tar.gz", "*.tar.bz2", "*.whl", "*.egg",
    "*.ttf", "*.woff", "*.woff2", "*.eot",
    "*.mp4", "*.mp3", "*.wav", "*.avi",
    "*.sqlite", "*.db", "*.sqlite3",
]

SOURCE_EXTENSIONS = {
    ".py", ".pyi",
    ".js", ".mjs", ".cjs", ".jsx",
    ".ts", ".tsx", ".mts", ".cts",
    ".go",
    ".rs",
    ".java", ".kt", ".scala",
    ".swift",
    ".c", ".cpp", ".cc", ".h", ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".ex", ".exs",
    ".sh", ".bash", ".zsh", ".fish",
    ".lua",
    ".r", ".R",
    ".zig",
}

CONFIG_NAMES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "go.mod", "build.gradle", "pom.xml",
    "requirements.txt", "requirements-dev.txt",
    "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
}

MAX_FILE_BYTES = 512 * 1024  # skip files over 512 KB
SYMBOL_SCAN_LINES = 600      # only scan first N lines per file
MAX_SYMBOLS_PER_FILE = 35
MAX_IMPORTS_PER_FILE = 12


# ── symbol extraction ─────────────────────────────────────────────────────────

# (compiled_regex, kind) — match against the stripped line
_SYM = [
    # Python
    (re.compile(r"^(?:async\s+)?def\s+(\w+)\s*\("), "fn"),
    (re.compile(r"^class\s+(\w+)"), "class"),
    # JS / TS — functions
    (re.compile(r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?function[*]?\s+(\w+)"), "fn"),
    # JS / TS — arrow / assigned functions
    (re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::\s*\S+\s*)?=\s*(?:async\s+)?\("), "fn"),
    (re.compile(r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?function"), "fn"),
    # JS / TS — classes / types / interfaces / enums
    (re.compile(r"^(?:export\s+(?:abstract\s+)?)?class\s+(\w+)"), "class"),
    (re.compile(r"^(?:export\s+)?(?:interface|type)\s+(\w+)"), "type"),
    (re.compile(r"^(?:export\s+)?enum\s+(\w+)"), "enum"),
    # Go
    (re.compile(r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\("), "fn"),
    (re.compile(r"^type\s+(\w+)\s+(?:struct|interface)"), "type"),
    # Rust
    (re.compile(r"^(?:pub(?:\(\w+\))?\s+)?(?:async\s+)?fn\s+(\w+)"), "fn"),
    (re.compile(r"^(?:pub(?:\(\w+\))?\s+)?struct\s+(\w+)"), "struct"),
    (re.compile(r"^(?:pub(?:\(\w+\))?\s+)?trait\s+(\w+)"), "trait"),
    (re.compile(r"^(?:pub(?:\(\w+\))?\s+)?enum\s+(\w+)"), "enum"),
    (re.compile(r"^(?:pub(?:\(\w+\))?\s+)?impl(?:\s+\w+\s+for)?\s+(\w+)"), "impl"),
    # Java / C# / Kotlin / Swift (methods inside classes)
    (re.compile(
        r"^(?:(?:public|private|protected|internal|override|static|abstract|virtual|async|sealed|final|open)\s+)+"
        r"(?:(?:fun|void|int|str|string|bool|boolean|object|var|val|def)\s+)?(\w+)\s*\("
    ), "fn"),
    # Generic fallback for common definition keywords
    (re.compile(r"^(?:fn|sub|procedure|method|func)\s+(\w+)"), "fn"),
    (re.compile(r"^(?:def)\s+(\w+)"), "fn"),
]

_IMP = [
    # Python
    re.compile(r"^from\s+(\S+)\s+import"),
    re.compile(r"^import\s+([\w, ]+)"),
    # JS / TS
    re.compile(r"""^import\s+.*?from\s+['"]([^'"]+)['"]"""),
    re.compile(r"""^(?:const|let|var)\s+\w+\s*=\s*require\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    # Go (inside import block — bare quoted string)
    re.compile(r"""^\s+"([^"]+)"\s*$"""),
    re.compile(r"""^import\s+"([^"]+)"""),
    # Rust
    re.compile(r"^use\s+([\w:]+)"),
    # C / C++
    re.compile(r"""^#include\s+[<"]([^>"]+)[>"]"""),
]


def _extract(path: Path, text: str) -> tuple[list[tuple[str, str, int]], list[str]]:
    """Return (symbols, imports) for a source file."""
    symbols: list[tuple[str, str, int]] = []
    imports: list[str] = []
    seen_syms: set[str] = set()
    seen_imps: set[str] = set()

    lines = text.splitlines()
    for lineno, raw in enumerate(lines[:SYMBOL_SCAN_LINES], start=1):
        stripped = raw.strip()
        if not stripped:
            continue

        # Skip pure comment / decorator lines
        if stripped.startswith(("#", "//", "/*", "*", "@", '"', "'")):
            continue

        # Symbol extraction — only match at low indentation (top-level or one level in)
        indent = len(raw) - len(raw.lstrip())
        if indent <= 4 and len(symbols) < MAX_SYMBOLS_PER_FILE:
            for pat, kind in _SYM:
                m = pat.match(stripped)
                if m:
                    name = m.group(1)
                    if name and name not in seen_syms:
                        seen_syms.add(name)
                        symbols.append((name, kind, lineno))
                    break

        # Import extraction
        if len(imports) < MAX_IMPORTS_PER_FILE:
            for pat in _IMP:
                m = pat.match(stripped)
                if m and m.group(1):
                    raw_imp = m.group(1).strip()
                    # Take the first meaningful segment
                    short = re.split(r"[/. ,;]", raw_imp)[0].strip()
                    if short and short not in seen_imps and short not in {"", "self", "super"}:
                        seen_imps.add(short)
                        imports.append(short)
                    break

    return symbols, imports


# ── .gitignore parsing ────────────────────────────────────────────────────────

def _load_gitignore(root: Path) -> list[str]:
    gi = root / ".gitignore"
    if not gi.is_file():
        return []
    patterns = []
    for line in gi.read_text(errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def _gitignore_match(rel: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat):
            return True
        if fnmatch.fnmatch(os.path.basename(rel), pat):
            return True
    return False


# ── directory tree ────────────────────────────────────────────────────────────

def _build_tree(root: Path, ignore_dirs: set[str], gi_patterns: list[str], max_depth: int) -> str:
    lines: list[str] = []

    def walk(path: Path, prefix: str, depth: int) -> None:
        if depth > max_depth:
            lines.append(f"{prefix}  ...")
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except PermissionError:
            return

        visible = []
        for e in entries:
            rel = str(e.relative_to(root))
            if e.is_dir():
                if e.name in ignore_dirs:
                    continue
                if _gitignore_match(rel, gi_patterns):
                    continue
                visible.append(e)
            else:
                if any(fnmatch.fnmatch(e.name, pat) for pat in IGNORE_FILE_PATTERNS):
                    continue
                if _gitignore_match(rel, gi_patterns):
                    continue
                visible.append(e)

        for i, e in enumerate(visible):
            connector = "└── " if i == len(visible) - 1 else "├── "
            lines.append(f"{prefix}{connector}{e.name}{'/' if e.is_dir() else ''}")
            if e.is_dir():
                extension = "    " if i == len(visible) - 1 else "│   "
                walk(e, prefix + extension, depth + 1)

    lines.append(f"{root.name}/")
    walk(root, "", 1)
    return "\n".join(lines)


# ── config file summary ───────────────────────────────────────────────────────

def _summarize_config(path: Path) -> str | None:
    name = path.name
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None

    if name == "package.json":
        try:
            data = json.loads(text)
            parts = []
            for key in ("name", "version", "description"):
                if key in data:
                    parts.append(f"{key}: {data[key]}")
            for key in ("dependencies", "devDependencies"):
                if key in data:
                    deps = list(data[key].keys())[:8]
                    label = "deps" if key == "dependencies" else "devDeps"
                    parts.append(f"{label}: {' · '.join(deps)}")
            return "  " + " | ".join(parts) if parts else None
        except json.JSONDecodeError:
            return None

    if name == "pyproject.toml":
        lines = []
        for line in text.splitlines():
            m = re.match(r'^(name|version|description)\s*=\s*"([^"]+)"', line)
            if m:
                lines.append(f"{m.group(1)}: {m.group(2)}")
        return "  " + " | ".join(lines) if lines else None

    if name in ("Cargo.toml", "go.mod"):
        lines = []
        for line in text.splitlines()[:5]:
            m = re.match(r'^(?:module|name|version)\s*[=\s]\s*"?([^\s"]+)"?', line)
            if m:
                lines.append(line.strip())
        return "  " + " | ".join(lines) if lines else None

    if name == "requirements.txt":
        pkgs = [l.split("==")[0].split(">=")[0].strip() for l in text.splitlines()
                if l.strip() and not l.startswith("#")][:12]
        return f"  packages: {' · '.join(pkgs)}" if pkgs else None

    if name in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml"):
        froms = re.findall(r"^FROM\s+(\S+)", text, re.MULTILINE)
        if froms:
            return f"  FROM: {' · '.join(froms[:4])}"

    return None


# ── file walker ───────────────────────────────────────────────────────────────

def _collect_files(
    root: Path,
    ignore_dirs: set[str],
    gi_patterns: list[str],
) -> tuple[list[Path], list[Path]]:
    """Return (source_files, config_files), both relative to root."""
    source_files: list[Path] = []
    config_files: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        rel_dir = dp.relative_to(root)

        # Prune ignored directories in-place
        dirnames[:] = [
            d for d in sorted(dirnames)
            if d not in ignore_dirs
            and not _gitignore_match(str(rel_dir / d), gi_patterns)
        ]

        for fname in sorted(filenames):
            if any(fnmatch.fnmatch(fname, pat) for pat in IGNORE_FILE_PATTERNS):
                continue
            fpath = dp / fname
            rel = fpath.relative_to(root)
            if _gitignore_match(str(rel), gi_patterns):
                continue
            try:
                size = fpath.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                continue

            if fpath.suffix.lower() in SOURCE_EXTENSIONS:
                source_files.append(fpath)
            elif fname in CONFIG_NAMES:
                config_files.append(fpath)

    return source_files, config_files


# ── output builder ────────────────────────────────────────────────────────────

def _format_symbols(symbols: list[tuple[str, str, int]]) -> str:
    grouped: dict[str, list[str]] = {}
    for name, kind, lineno in symbols:
        grouped.setdefault(kind, []).append(f"`{name}` L{lineno}")
    return "  " + " | ".join(f"{k}: {', '.join(v)}" for k, v in grouped.items())


def generate(root: Path, output: Path, max_depth: int) -> None:
    gi_patterns = _load_gitignore(root)
    source_files, config_files = _collect_files(root, IGNORE_DIRS, gi_patterns)

    sections: list[str] = []

    # ── header ──
    sections.append(
        f"# Code Context Map\n"
        f"> Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | "
        f"Root: `{root}` | "
        f"Source files: {len(source_files)}\n"
    )

    # ── directory tree ──
    tree = _build_tree(root, IGNORE_DIRS, gi_patterns, max_depth)
    sections.append(f"## Directory Tree\n\n```\n{tree}\n```\n")

    # ── config summaries ──
    if config_files:
        cfg_lines: list[str] = []
        for cf in config_files:
            summary = _summarize_config(cf)
            if summary:
                rel = cf.relative_to(root)
                cfg_lines.append(f"### `{rel}`\n{summary}")
        if cfg_lines:
            sections.append("## Config Files\n\n" + "\n\n".join(cfg_lines) + "\n")

    # ── symbol index ──
    sym_lines: list[str] = []
    for sf in source_files:
        try:
            text = sf.read_text(errors="ignore")
        except OSError:
            continue
        symbols, imports = _extract(sf, text)
        if not symbols and not imports:
            continue
        rel = sf.relative_to(root)
        parts: list[str] = []
        if symbols:
            parts.append(_format_symbols(symbols))
        if imports:
            parts.append(f"  imports: {' · '.join(imports)}")
        sym_lines.append(f"### `{rel}`\n" + "\n".join(parts))

    if sym_lines:
        sections.append("## Symbol Index\n\n" + "\n\n".join(sym_lines) + "\n")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(sections), encoding="utf-8")
    print(f"✓ Wrote {output} ({output.stat().st_size // 1024} KB, {len(source_files)} source files)")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a lean code map for Claude to use as session context."
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Root directory of the repo to map (default: current directory)",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output path (default: <target>/.claude/codecontext.md)",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=5,
        help="Max depth for directory tree (default: 5)",
    )
    args = parser.parse_args()

    root = Path(args.target).resolve()
    if not root.is_dir():
        sys.exit(f"Error: '{root}' is not a directory.")

    output = Path(args.output).resolve() if args.output else root / ".claude" / "codecontext.md"
    generate(root, output, args.max_depth)


if __name__ == "__main__":
    main()
