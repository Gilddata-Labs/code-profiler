"""Static analysis of a checked out tree: LOC, structure, tests, open source.

Reads files only to count and classify them. No source text leaves this module.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

from .util import log, pct, run

# extension -> (language, line comment, block start, block end)
LANGS: dict[str, tuple[str, str | None, str | None, str | None]] = {
    # Docstrings count as code, as scc counts them.
    ".py": ("Python", "#", None, None), ".pyi": ("Python", "#", None, None),
    ".js": ("JavaScript", "//", "/*", "*/"), ".mjs": ("JavaScript", "//", "/*", "*/"),
    ".cjs": ("JavaScript", "//", "/*", "*/"), ".jsx": ("JSX", "//", "/*", "*/"),
    ".ts": ("TypeScript", "//", "/*", "*/"), ".tsx": ("TSX", "//", "/*", "*/"),
    ".java": ("Java", "//", "/*", "*/"), ".kt": ("Kotlin", "//", "/*", "*/"),
    ".kts": ("Kotlin", "//", "/*", "*/"), ".scala": ("Scala", "//", "/*", "*/"),
    ".go": ("Go", "//", "/*", "*/"), ".rs": ("Rust", "//", "/*", "*/"),
    ".c": ("C", "//", "/*", "*/"), ".h": ("C Header", "//", "/*", "*/"),
    ".cc": ("C++", "//", "/*", "*/"), ".cpp": ("C++", "//", "/*", "*/"),
    ".cxx": ("C++", "//", "/*", "*/"), ".hpp": ("C++ Header", "//", "/*", "*/"),
    ".cs": ("C#", "//", "/*", "*/"), ".swift": ("Swift", "//", "/*", "*/"),
    ".m": ("Objective C", "//", "/*", "*/"), ".mm": ("Objective C++", "//", "/*", "*/"),
    ".php": ("PHP", "//", "/*", "*/"), ".rb": ("Ruby", "#", "=begin", "=end"),
    ".dart": ("Dart", "//", "/*", "*/"), ".lua": ("Lua", "--", "--[[", "]]"),
    ".r": ("R", "#", None, None), ".jl": ("Julia", "#", "#=", "=#"),
    ".sh": ("Shell", "#", None, None), ".bash": ("BASH", "#", None, None),
    ".zsh": ("Zsh", "#", None, None), ".ps1": ("PowerShell", "#", "<#", "#>"),
    ".sql": ("SQL", "--", "/*", "*/"), ".html": ("HTML", None, "<!--", "-->"),
    ".htm": ("HTML", None, "<!--", "-->"), ".css": ("CSS", None, "/*", "*/"),
    ".scss": ("Sass", "//", "/*", "*/"), ".less": ("LESS", "//", "/*", "*/"),
    ".vue": ("Vue", "//", "<!--", "-->"), ".svelte": ("Svelte", "//", "<!--", "-->"),
    ".ex": ("Elixir", "#", None, None), ".exs": ("Elixir", "#", None, None),
    ".erl": ("Erlang", "%", None, None), ".hs": ("Haskell", "--", "{-", "-}"),
    ".clj": ("Clojure", ";", None, None), ".fs": ("F#", "//", "(*", "*)"),
    ".pl": ("Perl", "#", None, None), ".groovy": ("Groovy", "//", "/*", "*/"),
    ".tf": ("Terraform", "#", "/*", "*/"), ".proto": ("Protocol Buffers", "//", "/*", "*/"),
    ".sol": ("Solidity", "//", "/*", "*/"), ".ipynb": ("Jupyter", None, None, None),
    ".md": ("Markdown", None, None, None), ".rst": ("ReStructuredText", None, None, None),
    ".yml": ("YAML", "#", None, None), ".yaml": ("YAML", "#", None, None),
    ".json": ("JSON", None, None, None), ".toml": ("TOML", "#", None, None),
    ".xml": ("XML", None, "<!--", "-->"), ".gradle": ("Gradle", "//", "/*", "*/"),
    ".txt": ("Plain Text", None, None, None), ".jsonl": ("JSONL", None, None, None),
    ".svg": ("SVG", None, "<!--", "-->"), ".service": ("Systemd", "#", None, None),
    ".ini": ("INI", ";", None, None), ".cfg": ("INI", "#", None, None),
    ".csv": ("CSV", None, None, None), ".bat": ("Batch", "REM", None, None),
    ".cmake": ("CMake", "#", None, None), ".hcl": ("HCL", "#", "/*", "*/"),
    ".graphql": ("GraphQL", "#", None, None), ".mdx": ("MDX", None, None, None),
    ".tex": ("TeX", "%", None, None), ".nix": ("Nix", "#", "/*", "*/"),
    ".zig": ("Zig", "//", None, None), ".ml": ("OCaml", None, "(*", "*)"),
}
NAMED = {"Dockerfile": ("Dockerfile", "#", None, None), "Makefile": ("Makefile", "#", None, None),
         "Jenkinsfile": ("Groovy", "//", "/*", "*/"), "Gemfile": ("Ruby", "#", None, None),
         "LICENSE": ("License", None, None, None), "LICENCE": ("License", None, None, None),
         ".dockerignore": ("Docker ignore", "#", None, None), ".gitignore": ("gitignore", "#", None, None),
         "CMakeLists.txt": ("CMake", "#", None, None)}

# Languages that are code (not docs or config): used for structure, tests, OSS share.
PROGRAMMING = {
    "Python", "JavaScript", "JSX", "TypeScript", "TSX", "Java", "Kotlin", "Scala", "Go",
    "Rust", "C", "C Header", "C++", "C++ Header", "C#", "Swift", "Objective C",
    "Objective C++", "PHP", "Ruby", "Dart", "Lua", "R", "Julia", "Shell", "BASH", "Zsh",
    "PowerShell", "SQL", "Vue", "Svelte", "Elixir", "Erlang", "Haskell", "Clojure", "F#",
    "Perl", "Groovy", "Solidity", "Jupyter", "Zig", "OCaml", "Nix", "HCL", "Batch", "CMake",
}

VENDOR_RE = re.compile(
    r"(^|/)(vendor|vendors|third[_-]?party|3rdparty|external|externals|deps|"
    r"node_modules|bower_components|site-packages|dist-packages|Pods|Carthage|"
    r"jspm_packages|\.yarn|libs?/(jquery|bootstrap|angular|react|lodash|d3))(/|$)",
    re.I,
)
MINIFIED_RE = re.compile(r"\.min\.(js|css)$|[.-]bundle\.js$", re.I)
LICENSE_HEADER_RE = re.compile(
    r"SPDX-License-Identifier|Licensed under the Apache License|Permission is hereby granted, "
    r"free of charge|GNU (Lesser |Affero )?General Public License|Mozilla Public License|"
    r"Redistribution and use in source and binary forms|@license\b|This file is part of",
    re.I,
)
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|testing|test_utils|e2e|integration_tests)(/|$)|"
    r"(^|/)test_[^/]+\.py$|_test\.(py|go|rb|exs?)$|\.(test|spec)\.[jt]sx?$|"
    r"(Test|Tests|Spec|IT)\.(java|kt|cs|scala|swift|php)$|_spec\.rb$|conftest\.py$",
    re.I,
)
CI_PATHS = (".github/workflows", ".gitlab-ci.yml", "Jenkinsfile", ".circleci", ".travis.yml",
            "azure-pipelines.yml", "bitbucket-pipelines.yml", ".drone.yml", ".buildkite",
            "cloudbuild.yaml", "appveyor.yml", ".woodpecker.yml", "buildspec.yml",
            ".teamcity", "codemagic.yaml", "bitrise.yml")
RUNNER_FILES = ("pytest.ini", "tox.ini", "conftest.py", "noxfile.py", ".rspec", "phpunit.xml",
                "phpunit.xml.dist", "karma.conf.js", "cypress.config.js", "cypress.config.ts",
                "playwright.config.ts", "playwright.config.js", ".mocharc.json", ".mocharc.yml",
                "jest.config.js", "jest.config.ts", "jest.config.mjs", "vitest.config.ts",
                "vitest.config.js", "ava.config.js", "codecept.conf.js")
LIB_MANIFESTS = ("setup.py", "pyproject.toml", "setup.cfg", "package.json", "Cargo.toml",
                 "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "composer.json",
                 "*.gemspec", "*.csproj", "Package.swift", "pubspec.yaml", "mix.exs")
MAX_FILE_BYTES = 2_000_000


def lang_of(path: str):
    name = path.rsplit("/", 1)[-1]
    if name in NAMED:
        return NAMED[name]
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    return LANGS.get(ext)


def tracked_files(repo: Path) -> list[str]:
    out = run(["git", "ls-files", "-z"], cwd=repo, check=False)
    files = [f for f in out.split("\0") if f]
    if not files:  # not a git checkout: walk the folder
        files = [str(p.relative_to(repo)) for p in repo.rglob("*")
                 if p.is_file() and ".git" not in p.parts]
    return files


def _read_text(path: Path) -> str | None:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", "replace")


def count_lines(text: str, spec) -> tuple[int, int, int]:
    """(code, comment, blank) for one file."""
    _, line_c, blk_s, blk_e = spec
    code = comment = blank = 0
    in_block = False
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            blank += 1
            continue
        if in_block:
            comment += 1
            if blk_e and blk_e in s:
                in_block = False
            continue
        if blk_s and s.startswith(blk_s):
            comment += 1
            rest = s[len(blk_s):]
            if not (blk_e and blk_e in rest):
                in_block = True
            continue
        if line_c and s.startswith(line_c):
            comment += 1
            continue
        code += 1
    return code, comment, blank


def run_scc(repo: Path) -> dict | None:
    if not shutil.which("scc"):
        return None
    try:
        out = run(["scc", "--format", "json", "--no-cocomo", "--by-file", "."], cwd=repo,
                  timeout=1800)
        raw = json.loads(out)
    except Exception as e:  # noqa: BLE001
        log.warning("scc failed, using the built in counter: %s", e)
        return None
    by_lang, per_file = {}, {}
    for entry in raw:
        by_lang[entry["Name"]] = entry["Code"]
        for f in entry.get("Files") or []:
            loc = f.get("Location", "").lstrip("./")
            per_file[loc] = (entry["Name"], f["Code"], f["Comment"], f["Blank"])
        entry["Files"] = []
    return {"tool": "scc", "loc": sum(by_lang.values()), "by_language": by_lang,
            "raw": raw, "per_file": per_file}


def builtin_loc(repo: Path, files: list[str]) -> dict:
    agg = defaultdict(lambda: {"Code": 0, "Comment": 0, "Blank": 0, "Lines": 0, "Count": 0})
    per_file = {}
    for f in files:
        spec = lang_of(f)
        if not spec:
            continue
        text = _read_text(repo / f)
        if text is None:
            continue
        if spec[0] == "Jupyter":
            text = _notebook_code(text)
        c, m, b = count_lines(text, spec)
        a = agg[spec[0]]
        a["Code"] += c; a["Comment"] += m; a["Blank"] += b
        a["Lines"] += c + m + b; a["Count"] += 1
        per_file[f] = (spec[0], c, m, b)
    raw = [{"Name": k, **v} for k, v in sorted(agg.items(), key=lambda kv: -kv[1]["Code"])]
    by_lang = {r["Name"]: r["Code"] for r in raw}
    return {"tool": "builtin", "loc": sum(by_lang.values()), "by_language": by_lang,
            "raw": raw, "per_file": per_file}


def _notebook_code(text: str) -> str:
    try:
        nb = json.loads(text)
    except ValueError:
        return ""
    return "\n".join("".join(c.get("source", [])) for c in nb.get("cells", [])
                     if c.get("cell_type") == "code")


# ------------------------------------------------------------- structure
_FUNC_PATTERNS = {
    "js": re.compile(r"^\s*(export\s+)?(default\s+)?(async\s+)?function\b|"
                     r"^\s*(export\s+)?(const|let|var)\s+\w+\s*=\s*(async\s*)?(\([^)]*\)|\w+)\s*=>|"
                     r"^\s*(public|private|protected|static|async|\s)*\w+\s*\([^)]*\)\s*\{\s*$"),
    "java": re.compile(r"^\s*(public|private|protected|static|final|abstract|synchronized|override|"
                       r"internal|virtual|async|\s)+[\w<>\[\],\s?]+\s+\w+\s*\([^;]*\)\s*(throws [\w, .]+)?\s*\{?\s*$"),
    "go": re.compile(r"^func\b"),
    "rust": re.compile(r"^\s*(pub(\([^)]*\))?\s+)?(async\s+)?(unsafe\s+)?fn\s+\w+"),
    "ruby": re.compile(r"^\s*def\s+"),
    "php": re.compile(r"^\s*(public|private|protected|static|\s)*function\s+\w+"),
    "kotlin": re.compile(r"^\s*(\w+\s+)*fun\s+"),
    "swift": re.compile(r"^\s*(\w+\s+)*func\s+"),
    "c": re.compile(r"^[A-Za-z_][\w\s\*&:<>,]*\s+\**\w+(::\w+)?\s*\([^;]*\)\s*(const)?\s*\{?\s*$"),
}
_CLASS_RE = re.compile(r"^\s*(export\s+)?(default\s+)?(public\s+|private\s+|internal\s+|abstract\s+|"
                       r"final\s+|sealed\s+|data\s+|open\s+|pub\s+)*(class|interface|struct|enum|trait|"
                       r"protocol|object|type\s+\w+\s+struct)\b")
_KIND = {"JavaScript": "js", "JSX": "js", "TypeScript": "js", "TSX": "js", "Vue": "js",
         "Svelte": "js", "Java": "java", "C#": "java", "Scala": "java", "Groovy": "java",
         "Dart": "java", "Go": "go", "Rust": "rust", "Ruby": "ruby", "PHP": "php",
         "Kotlin": "kotlin", "Swift": "swift", "C": "c", "C++": "c", "Objective C": "c",
         "Objective C++": "c"}
_SKIP_KEYWORDS = re.compile(r"^\s*(if|for|while|switch|catch|return|else|do|try|new|sizeof)\b")


def _python_structure(text: str) -> tuple[int, int, int, int] | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    f = c = documented = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            f += 1
            documented += bool(ast.get_docstring(node))
        elif isinstance(node, ast.ClassDef):
            c += 1
            documented += bool(ast.get_docstring(node))
    return f, c, documented, f + c


def _generic_structure(text: str, kind: str) -> tuple[int, int, int, int]:
    fre = _FUNC_PATTERNS[kind]
    lines = text.splitlines()
    f = c = documented = 0
    for i, line in enumerate(lines):
        is_class = bool(_CLASS_RE.match(line))
        is_func = not is_class and bool(fre.match(line)) and not _SKIP_KEYWORDS.match(line)
        if not (is_class or is_func):
            continue
        f += is_func
        c += is_class
        j = i - 1
        while j >= 0 and (not lines[j].strip() or lines[j].strip().startswith(("@", "#["))):
            j -= 1
        prev = lines[j].strip() if j >= 0 else ""
        if prev.endswith("*/") or prev.startswith(("///", "//", "#", "*")):
            documented += 1
    return f, c, documented, f + c


def analyze_tree(repo: Path, use_scc: bool = True) -> dict:
    files = tracked_files(repo)
    loc = (run_scc(repo) if use_scc else None) or builtin_loc(repo, files)
    per_file: dict = loc.pop("per_file")

    funcs = classes = documented = units = parse_gaps = 0
    comment_lines = code_lines = 0
    test_loc = source_loc = vendor_loc = oss_loc = prog_loc = 0
    source_files: list[str] = []
    test_files: list[str] = []
    oss_paths: set[str] = set()
    code_paths: set[str] = set()
    own_code_loc: dict[str, int] = {}
    for f in files:
        spec = lang_of(f)
        info = per_file.get(f)
        if not spec or not info or spec[0] not in PROGRAMMING:
            continue
        lang, code, comment, _ = info
        code_paths.add(f)
        prog_loc += code
        comment_lines += comment
        code_lines += code
        is_vendor = bool(VENDOR_RE.search(f) or MINIFIED_RE.search(f))
        text = _read_text(repo / f) if code else None
        is_oss = is_vendor or bool(text and LICENSE_HEADER_RE.search(text[:3000]))
        if is_vendor:
            vendor_loc += code
        if is_oss:
            oss_loc += code
            oss_paths.add(f)
            continue  # third party code is not judged for structure or tests
        own_code_loc[f] = code
        if TEST_PATH_RE.search(f):
            test_files.append(f)
            test_loc += code
            continue
        source_files.append(f)
        source_loc += code
        if text is None:
            continue
        if spec[0] == "Python":
            res = _python_structure(text)
            if res is None:
                parse_gaps += 1
                continue
        elif spec[0] in _KIND:
            res = _generic_structure(text, _KIND[spec[0]])
        else:
            continue
        funcs += res[0]; classes += res[1]; documented += res[2]; units += res[3]

    # A source file is "tested" when a test file name or body mentions its stem.
    test_blob_names = " ".join(Path(t).stem.lower() for t in test_files)
    test_bodies = []
    for t in test_files[:5000]:
        txt = _read_text(repo / t)
        if txt:
            test_bodies.append(txt.lower())
    test_blob = test_blob_names + "\n" + "\n".join(test_bodies)
    untested = 0
    countable = [s for s in source_files if Path(s).stem.lower() not in
                 {"__init__", "index", "main", "setup", "conftest", "manage", "app", "config",
                  "settings", "constants", "types", "urls", "wsgi", "asgi"}]
    for s in countable:
        stem = Path(s).stem.lower()
        if len(stem) < 3 or not re.search(rf"\b{re.escape(stem)}\b", test_blob):
            untested += 1

    names = set(files)
    top = {f.split("/", 1)[0] for f in files}
    has_ci = any(f.startswith(p) or f == p for f in files for p in CI_PATHS[:1]) or any(
        p in names or p in top for p in CI_PATHS)
    has_runner = any(Path(f).name in RUNNER_FILES for f in files) or _manifest_has_tests(repo, names) \
        or any(f.endswith("_test.go") for f in files)
    has_lib = any(Path(f).name in LIB_MANIFESTS or Path(f).suffix in (".gemspec", ".csproj")
                  for f in files if f.count("/") <= 1)

    return {
        "scc": loc,
        "total_files": len(files),
        "oss_paths": oss_paths,
        "code_paths": code_paths,
        "own_code_loc": own_code_loc,
        "structure": {
            "function_count": funcs,
            "class_count": classes,
            "docstring_coverage_pct": pct(documented, units),
            "comment_docstring_ratio": round(comment_lines / code_lines, 4) if code_lines else 0.0,
            "untested_files": untested,
            "untested_files_pct": pct(untested, len(countable)),
            "structure_parse_gaps": parse_gaps or "",
        },
        "tests": {
            "has_tests": bool(test_files),
            "has_test_runner": bool(has_runner),
            "has_ci_cd": bool(has_ci),
            "test_source_loc_pct": pct(test_loc, source_loc),
        },
        "paths": {
            "has_library_code": bool(has_lib),
            "open_source_loc_pct": pct(oss_loc, prog_loc),
            "library_modules_loc_pct": pct(vendor_loc, prog_loc),
        },
    }


def _manifest_has_tests(repo: Path, names: set[str]) -> bool:
    if "package.json" in names:
        try:
            pkg = json.loads((repo / "package.json").read_text(encoding="utf-8"))
            script = (pkg.get("scripts") or {}).get("test", "")
            if script and "no test specified" not in script:
                return True
        except (OSError, ValueError):
            pass
    for name, needle in (("pyproject.toml", "[tool.pytest"), ("setup.cfg", "[tool:pytest]"),
                         ("pom.xml", "surefire"), ("build.gradle", "test"),
                         ("Cargo.toml", "[dev-dependencies]"), ("Makefile", "test:")):
        if name in names:
            text = _read_text(repo / name) or ""
            if needle in text:
                return True
    return False


def language_breakdown(languages: dict[str, int]) -> tuple[str, str]:
    """('Python:93.6%; CSS:2.2%', 'Python') from a bytes or LOC per language map."""
    total = sum(languages.values())
    if not total:
        return "", ""
    items = sorted(languages.items(), key=lambda kv: -kv[1])
    text = "; ".join(f"{k}:{v / total * 100:.1f}%" for k, v in items if v / total >= 0.0005)
    return text, items[0][0]


def code_only(by_language: dict[str, int]) -> dict[str, int]:
    return {k: v for k, v in by_language.items() if k in PROGRAMMING or k in ("HTML", "CSS", "Sass", "LESS")}
