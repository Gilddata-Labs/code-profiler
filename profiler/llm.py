"""Optional LLM analysis. OFF by default.

When turned on, a bounded excerpt of each repo (README start, file list,
manifests and the start of a few source files) is sent to the chosen AI
provider. Nothing else is sent. The seller must opt in.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import http
from .code import PROGRAMMING, TEST_PATH_RE, VENDOR_RE, lang_of, tracked_files, _read_text

DEFAULT_MODELS = {"anthropic": "claude-haiku-5-5", "openai": "gpt-4o-mini"}
# Must match the catalogue template's Repo Type dropdown.
REPO_TYPES = ["Production backend", "Production frontend", "Mobile app", "Infra / DevOps",
              "Data / ETL / ML", "Library / SDK", "Scripts / tooling", "Training exercise",
              "Scratch / POC", "Config only", "Empty"]
MANIFESTS = ("package.json", "pyproject.toml", "setup.py", "requirements.txt", "go.mod",
             "Cargo.toml", "pom.xml", "build.gradle", "composer.json", "Gemfile", "Dockerfile")

PROMPT = """You are profiling a software repository for a code data marketplace.
From the excerpt below, return ONLY a JSON object with these keys:
"codebase_description": what this repo does in 1 to 2 lines (service, frontend, infra, ETL...), no names of people,
"industry_domain": the industry it serves, 1 to 4 words,
"repo_type": exactly one of: {types},
"ai_code_score": 0 to 100, how likely it is that most of this code was written by an AI coding tool,
"ai_code_reasons": at most 3 short reasons for that score (for example uniform verbose comments, boilerplate docstrings on trivial functions, placeholder TODOs, emoji in logs).

EXCERPT
{excerpt}
"""


def build_excerpt(repo: Path, max_chars: int = 40000, max_files: int = 6,
                  lines_per_file: int = 150) -> str:
    """PRD: only file paths, READMEs and snippets under 200 lines."""
    files = tracked_files(repo)
    parts: list[str] = []
    readme = next((f for f in files if f.count("/") == 0 and f.lower().startswith("readme")), None)
    if readme:
        parts.append(f"## {readme}\n{(_read_text(repo / readme) or '')[:3000]}")
    parts.append("## File list (first 300)\n" + "\n".join(files[:300]))
    for m in MANIFESTS:
        if m in files:
            parts.append(f"## {m}\n{(_read_text(repo / m) or '')[:1500]}")
    src = [f for f in files if (lang_of(f) or ("",))[0] in PROGRAMMING
           and not TEST_PATH_RE.search(f) and not VENDOR_RE.search(f)]
    src.sort(key=lambda f: -((repo / f).stat().st_size if (repo / f).exists() else 0))
    step = max(1, len(src) // max_files) if src else 1
    for f in src[::step][:max_files]:
        text = _read_text(repo / f) or ""
        parts.append(f"## {f}\n" + "\n".join(text.splitlines()[:lines_per_file]))
    excerpt = "\n\n".join(parts)
    return excerpt[:max_chars]


def analyze(repo: Path, provider: str, api_key: str, model: str | None = None) -> dict:
    """Returns the four summary fields plus 'chars_sent' and 'llm_analysis_error'."""
    model = model or DEFAULT_MODELS[provider]
    excerpt = build_excerpt(repo)
    prompt = PROMPT.format(types=", ".join(REPO_TYPES), excerpt=excerpt)
    out = {"codebase_description": "", "industry_domain": "", "repo_type": "",
           "ai_code_score": "", "ai_code_reasons": "", "chars_sent": len(prompt),
           "llm_model": f"{provider}:{model}", "llm_analysis_error": ""}
    try:
        text = _call(provider, api_key, model, prompt)
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
        out["codebase_description"] = str(data.get("codebase_description", ""))[:400]
        out["industry_domain"] = str(data.get("industry_domain", ""))[:60]
        rt = str(data.get("repo_type", "")).strip()
        match = {t.lower(): t for t in REPO_TYPES}.get(rt.lower(), "")
        out["repo_type"] = match
        score = data.get("ai_code_score", "")
        out["ai_code_score"] = max(0, min(100, int(score))) if str(score).strip().isdigit() else ""
        reasons = data.get("ai_code_reasons") or []
        out["ai_code_reasons"] = "; ".join(map(str, reasons))[:300] if isinstance(reasons, list) else str(reasons)[:300]
        if not data:
            out["llm_analysis_error"] = "no JSON in reply"
    except Exception as e:  # noqa: BLE001
        out["llm_analysis_error"] = str(e)[:300]
    return out


def _call(provider: str, key: str, model: str, prompt: str) -> str:
    s = http.Session()
    if provider == "anthropic":
        s.headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        r = s.request("POST", "https://api.anthropic.com/v1/messages", timeout=120, json={
            "model": model, "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}]})
        if r.status_code >= 400:
            raise RuntimeError(f"Anthropic API {r.status_code}: {r.text[:200]}")
        return "".join(b.get("text", "") for b in r.json().get("content", []))
    if provider == "openai":
        s.headers.update({"Authorization": f"Bearer {key}"})
        r = s.request("POST", "https://api.openai.com/v1/chat/completions", timeout=120, json={
            "model": model, "max_tokens": 600,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]})
        if r.status_code >= 400:
            raise RuntimeError(f"OpenAI API {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"]
    raise ValueError(f"unknown LLM provider {provider}")
