"""Lists and thresholds. Override any key with --config file.json or file.yaml."""
from __future__ import annotations

import copy
import json
from pathlib import Path

DEFAULTS: dict = {
    # Category column: primary language on this list -> "Rare language"
    "rare_languages": ["Haskell", "Elixir", "Erlang", "OCaml", "COBOL", "Fortran", "Solidity",
                       "Zig", "Clojure", "F#", "Julia", "Nim", "Crystal", "Racket", "Scheme",
                       "Lisp", "Common Lisp", "Prolog", "Ada", "Pascal", "Delphi", "Elm",
                       "PureScript", "Idris", "Agda", "Coq", "Lean", "Smalltalk", "Tcl", "Apex",
                       "ABAP", "VHDL", "Verilog", "SystemVerilog", "Assembly", "Mojo", "Gleam"],
    # Data / Markup LOC column
    "data_languages": ["JSON", "JSONL", "CSV", "TSV", "XML", "SVG", "YAML", "Markdown", "MDX",
                       "Plain Text", "Text", "ReStructuredText", "Jupyter", "Properties File",
                       "Properties", "License", "gitignore", "Docker ignore", "INI"],
    # PR size bands (PRD): simple <= 50 lines AND <= 3 files; rich > 500 lines OR > 15 files
    "pr_bands": {"simple_max_loc": 50, "simple_max_files": 3,
                 "rich_min_loc": 500, "rich_min_files": 15},
    # Dependency / build output folders: not counted in Total Files or Size
    "excluded_dirs": ["node_modules", "vendor", "dist", "build", "target", ".gradle", ".venv",
                      "venv", "__pycache__", "bower_components", ".next", ".nuxt", "out",
                      ".tox", ".mypy_cache", ".pytest_cache", "Pods", "Carthage"],
    "vendored_dirs": ["vendor", "vendors", "third_party", "third-party", "thirdparty", "3rdparty",
                      "node_modules", "site-packages", "dist-packages", "venv", ".venv",
                      "bower_components", "Pods", "Carthage", "jspm_packages", "external",
                      "externals"],
    "generated_dirs": ["dist", "build", "generated", "gen", "migrations", "__generated__",
                       ".next", "out", "target"],
    "lock_files": ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
                   "poetry.lock", "Pipfile.lock", "Gemfile.lock", "Cargo.lock", "composer.lock",
                   "go.sum", "mix.lock", "pubspec.lock", "Podfile.lock", "gradle.lockfile",
                   "packages.lock.json", "flake.lock", "uv.lock", "bun.lockb"],
    "bot_patterns": [r"\[bot\]", "dependabot", "renovate", "github-actions", "gitlab-bot",
                     "semantic-release", "greenkeeper", "snyk", "codecov", "pre-commit-ci",
                     "mergify", "allcontributors", "imgbot", "whitesource", "deepsource",
                     "jenkins", "travis", "circleci", "buildkite", "teamcity", "bamboo",
                     "azure-pipelines", "copilot-swe-agent", r"\bci\b", r"\bbot\b", "automation",
                     "build-bot", "release-bot", "noreply@github.com"],
    # "alias name or email" -> "canonical name"; merged before counting contributors
    "author_aliases": {},
    "default_branch_names": ["main", "master", "develop", "trunk"],
    "max_file_bytes": 1_000_000,
    "dup_min_tokens": 50,
    "dup_min_lines": 5,
    "shared_file_overlap": 0.8,
    "shallow_commits": 5,
    "data_heavy_share": 0.5,
    "ai_bulk_commit_share": 0.5,
}


def load(path: str | None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if not path:
        return cfg
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml  # optional
            data = yaml.safe_load(text) or {}
        except ImportError:
            raise SystemExit("Reading a YAML config needs PyYAML (pip install pyyaml), "
                             "or pass the same settings as a .json file.")
    else:
        data = json.loads(text)
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    return cfg
