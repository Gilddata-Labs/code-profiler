"""Shared helpers: logging, JSON IO, subprocess, time."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("profiler")

BOT_RE = re.compile(
    r"(\[bot\]|dependabot|renovate|github-actions|gitlab-bot|semantic-release|"
    r"greenkeeper|snyk-bot|codecov|pre-commit-ci|mergify|allcontributors|"
    r"imgbot|whitesource|deepsource|copilot-swe-agent)",
    re.I,
)

# Commit or PR text that marks AI generated work.
AI_SIGNATURE_RE = re.compile(
    r"(co-authored-by:\s*(claude|copilot|cursor|codex|chatgpt|gemini|devin|aider|"
    r"github copilot|openai|anthropic|sweep|codegen|amazon q|windsurf|cline|jules)"
    r"|generated (with|by) (claude|copilot|cursor|chatgpt|codex|gemini|aider|devin|an? ai)"
    r"|🤖 generated|claude code|claude\.ai/code|cursor agent|devin\.ai|"
    r"aider:|\(aider\)|codex cloud|copilot-swe-agent|noreply@anthropic\.com)",
    re.I,
)


def is_bot(name: str, email: str = "") -> bool:
    return bool(BOT_RE.search(f"{name} {email}"))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_json(path: Path, data) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    path.write_text(text, encoding="utf-8")
    return path.stat().st_size


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def run(cmd: list[str], cwd: str | Path | None = None, timeout: int = 600,
        env: dict | None = None, check: bool = True) -> str:
    """Run a command and return stdout as text (bytes decoded leniently)."""
    full_env = os.environ.copy()
    full_env["GIT_TERMINAL_PROMPT"] = "0"
    if env:
        full_env.update(env)
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, env=full_env)
    out = proc.stdout.decode("utf-8", "replace")
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): {redact(err)[-800:]}")
    return out


def redact(text: str) -> str:
    """Strip tokens out of URLs and headers before anything is logged."""
    text = re.sub(r"(https?://)[^@/\s]+@", r"\1***@", text)
    text = re.sub(r"(Authorization: \w+ )\S+", r"\1***", text, flags=re.I)
    text = re.sub(r"(gh[pousr]_|glpat-|github_pat_)[A-Za-z0-9_\-]+", r"\1***", text)
    return text


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def safe_name(full_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "__", full_name.strip("/"))


def pct(part: float, whole: float, nd: int = 1) -> float:
    return round(100.0 * part / whole, nd) if whole else 0.0


def classify_error(exc: BaseException) -> str:
    msg = str(exc).lower()
    if "timed out" in msg or "timeout" in msg:
        return "timeout"
    if "rate limit" in msg or "429" in msg or "secondary rate" in msg:
        return "rate_limit"
    if "401" in msg or "403" in msg or "authentication" in msg or "permission" in msg:
        return "auth"
    if "404" in msg or "not found" in msg:
        return "not_found"
    if "could not resolve" in msg or "connection" in msg or "network" in msg:
        return "network"
    if "empty" in msg:
        return "empty_repo"
    return "other"


RETRYABLE = {"timeout", "rate_limit", "network"}
