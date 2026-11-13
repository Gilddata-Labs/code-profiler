"""Duplication: jscpd when installed (the buyer's exact command), a built in clone
detector otherwise. Also root commits and file hash overlap for duplicate groups."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from .util import log, run

JSCPD_IGNORE = ("**/vendor/**,**/vendors/**,**/third_party/**,**/node_modules/**,**/dist/**,"
                "**/build/**,**/generated/**,**/migrations/**,**/site-packages/**,**/venv/**,"
                "**/.venv/**,**/*.min.js,**/*.min.css,**/*.lock,**/package-lock.json,"
                "**/yarn.lock,**/pnpm-lock.yaml,**/go.sum,**/.git/**")
TOKEN_RE = re.compile(r"\w+|[^\s\w]")


def jscpd_available() -> bool:
    return bool(shutil.which("jscpd"))


def jscpd_command(target: str, cfg: dict) -> list[str]:
    return ["jscpd", target, "--min-tokens", str(cfg["dup_min_tokens"]), "--min-lines",
            str(cfg["dup_min_lines"]), "--reporters", "json", "--silent", "--ignore", JSCPD_IGNORE]


def run_jscpd(paths: list[Path], cfg: dict, timeout: int = 3600, big_files: bool = False) -> dict | None:
    """big_files lifts jscpd's default 1,000 line / 100 KB file limits. Off for the
    per repo run, so the number matches the buyer's plain `jscpd .`."""
    out = Path(tempfile.mkdtemp(prefix="gdp-jscpd-"))
    try:
        cmd = ["jscpd", *map(str, paths), "--min-tokens", str(cfg["dup_min_tokens"]),
               "--min-lines", str(cfg["dup_min_lines"]), "--reporters", "json", "--silent",
               "--ignore", JSCPD_IGNORE, "--output", str(out), "--absolute"]
        if big_files:
            cmd += ["--max-lines", "200000", "--max-size", "5mb"]
        run(cmd, timeout=timeout, check=False)
        rep = out / "jscpd-report.json"
        return json.loads(rep.read_text(encoding="utf-8")) if rep.exists() else None
    except Exception as e:  # noqa: BLE001
        log.warning("jscpd failed: %s", e)
        return None
    finally:
        shutil.rmtree(out, ignore_errors=True)


def _plain_jscpd(repo: Path, cfg: dict) -> dict | None:
    """Exactly the buyer's command, run inside the repo: jscpd . --min-tokens 50 --min-lines 5 --reporters json"""
    out = Path(tempfile.mkdtemp(prefix="gdp-jscpd-"))
    try:
        run(["jscpd", ".", "--min-tokens", str(cfg["dup_min_tokens"]), "--min-lines",
             str(cfg["dup_min_lines"]), "--reporters", "json", "--silent", "--output", str(out)],
            cwd=repo, timeout=3600, check=False)
        rep = out / "jscpd-report.json"
        return json.loads(rep.read_text(encoding="utf-8")) if rep.exists() else None
    except Exception as e:  # noqa: BLE001
        log.warning("jscpd failed: %s", e)
        return None
    finally:
        shutil.rmtree(out, ignore_errors=True)


def jscpd_repo(repo: Path, cfg: dict, has_non_authored: bool = True) -> dict | None:
    """The column uses the buyer's plain command so buyers reproduce it exactly. The PRD's
    variant (vendored and generated ignored) is kept in the evidence too."""
    plain = _plain_jscpd(repo, cfg)
    if plain is None:
        return None
    total = (plain.get("statistics") or {}).get("total") or {}
    out = {
        "tool": "jscpd",
        "command": "jscpd . --min-tokens {} --min-lines {} --reporters json".format(
            cfg["dup_min_tokens"], cfg["dup_min_lines"]),
        "statistics": {"total": {k: total.get(k) for k in
                                 ("lines", "duplicatedLines", "percentage", "clones", "sources")}},
        "ratio": round((total.get("percentage") or 0) / 100, 4),
        "clones_sample": [{"first": _rel(d["firstFile"], ""), "second": _rel(d["secondFile"], ""),
                           "lines": d.get("lines")} for d in plain.get("duplicates", [])[:2000]],
    }
    if has_non_authored:
        filt = run_jscpd([repo], cfg)
        if filt:
            ft = (filt.get("statistics") or {}).get("total") or {}
            out["excluding_vendored_generated"] = {
                "command": out["command"] + f" --ignore '{JSCPD_IGNORE}'",
                "percentage": ft.get("percentage"), "ratio": round((ft.get("percentage") or 0) / 100, 4)}
    else:
        out["excluding_vendored_generated"] = {"note": "no vendored or generated files; same as above",
                                               "ratio": out["ratio"]}
    return out


def _rel(f: dict, root: str) -> dict:
    name = f.get("name", "")
    if root and name.startswith(root):
        name = name[len(root):].lstrip("/\\")
    return {"file": name, "start": (f.get("start") or 0), "end": (f.get("end") or 0)}


# ------------------------------------------------------------ built in fallback
def _norm_lines(text: str) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        s = " ".join(line.split())
        if s:
            out.append((i, s))
    return out


def builtin_dup(repo: Path, files: list[dict], cfg: dict) -> dict:
    """Clone detection close to jscpd: windows of min_lines non blank lines with at
    least min_tokens tokens; a line is duplicated when a window covering it occurs
    more than once. Returns ratio plus a sampled fingerprint set for cross repo use."""
    min_lines, min_tokens = cfg["dup_min_lines"], cfg["dup_min_tokens"]
    windows: dict[int, list[tuple[str, int]]] = defaultdict(list)
    file_lines: dict[str, list[tuple[int, str]]] = {}
    total = 0
    for r in files:
        if r["class"] not in ("production", "test") or not r["code_loc"]:
            continue
        p = repo / r["path"]
        try:
            if p.stat().st_size > cfg["max_file_bytes"]:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = _norm_lines(text)
        file_lines[r["path"]] = lines
        total += len(lines)
        toks = [len(TOKEN_RE.findall(s)) for _, s in lines]
        for i in range(0, max(0, len(lines) - min_lines + 1)):
            if sum(toks[i:i + min_lines]) < min_tokens:
                continue
            h = hashlib.blake2b("\n".join(s for _, s in lines[i:i + min_lines]).encode(),
                                digest_size=8).digest()
            windows[int.from_bytes(h, "big")].append((r["path"], i))
    dup_idx: dict[str, set[int]] = defaultdict(set)
    clones = 0
    for h, locs in windows.items():
        if len(locs) > 1:
            clones += len(locs) - 1
            for path, i in locs:
                dup_idx[path].update(range(i, i + min_lines))
    dup_lines = sum(len(v) for v in dup_idx.values())
    sample = {h for h in windows if h % 4 == 0}
    return {
        "tool": "builtin (jscpd not installed)",
        "command": f"built in clone detector: {min_lines} line windows, >= {min_tokens} tokens",
        "statistics": {"total": {"lines": total, "duplicatedLines": dup_lines,
                                 "percentage": round(100 * dup_lines / total, 2) if total else 0,
                                 "clones": clones}},
        "ratio": round(dup_lines / total, 4) if total else 0.0,
        "_fingerprints": sample,
        "_window_count": len(sample),
    }


def file_hashes(repo: Path, files: list[dict]) -> dict[str, int]:
    """Hash -> code LOC of normalised production/test files with 5+ lines."""
    out: dict[str, int] = {}
    for r in files:
        if r["class"] not in ("production", "test") or r["code_loc"] < 5:
            continue
        try:
            text = (repo / r["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        norm = "\n".join(s for _, s in _norm_lines(text))
        out[hashlib.sha1(norm.encode()).hexdigest()[:16]] = r["code_loc"]
    return out


def cross_repo_files(hashes: dict[str, dict[str, int]]) -> dict[str, float]:
    """Share of a repo's production/test LOC in files that also exist, identical, in another repo."""
    owners: Counter = Counter()
    for h in hashes.values():
        owners.update(h.keys())
    out = {}
    for k, h in hashes.items():
        tot = sum(h.values())
        out[k] = round(sum(v for x, v in h.items() if owners[x] > 1) / tot, 4) if tot else 0.0
    return out


def stage_sources(repo: Path, files: list[dict], dest: Path, max_bytes: int) -> None:
    """Copy production and test files to a temp folder for the cross repo jscpd run.
    The folder stays on this machine and is deleted at the end of the run."""
    for r in files:
        if r["class"] not in ("production", "test"):
            continue
        src = repo / r["path"]
        try:
            if src.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        tgt = dest / r["path"]
        tgt.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(src, tgt)
        except OSError:
            pass


def cross_repo_jscpd(stage_root: Path, keys: list[str], code_loc: dict[str, int],
                     cfg: dict) -> dict[str, float] | None:
    """One jscpd run over all staged repos; per repo: its lines duplicated in another repo."""
    dirs = [stage_root / k for k in keys if (stage_root / k).exists()]
    if len(dirs) < 2:
        return {k: 0.0 for k in keys}
    rep = run_jscpd(dirs, cfg, timeout=7200, big_files=True)
    if rep is None:
        return None
    root = str(stage_root.resolve())
    covered: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))

    def repo_of(name: str) -> tuple[str, str]:
        rel = str(Path(name).resolve())[len(root):].lstrip("/\\")
        k, _, f = rel.partition("/")
        return k, f
    for d in rep.get("duplicates", []):
        (ka, fa), (kb, fb) = repo_of(d["firstFile"]["name"]), repo_of(d["secondFile"]["name"])
        if ka == kb:
            continue
        for k, f, side in ((ka, fa, d["firstFile"]), (kb, fb, d["secondFile"])):
            covered[k][f].update(range(side.get("start", 0), side.get("end", 0) + 1))
    return {k: round(min(1.0, sum(len(v) for v in covered[k].values()) / code_loc[k]), 4)
            if code_loc.get(k) else 0.0 for k in keys}


def cross_repo_builtin(fps: dict[str, set[int]]) -> dict[str, float]:
    counts: Counter = Counter()
    for s in fps.values():
        counts.update(s)
    out = {}
    for k, s in fps.items():
        shared = sum(1 for h in s if counts[h] > 1)
        out[k] = round(shared / len(s), 4) if s else 0.0
    return out


def overlap_groups(hashes: dict[str, set[str]], threshold: float) -> list[tuple[str, str, float]]:
    keys = sorted(hashes)
    pairs = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            A, B = set(hashes[a]), set(hashes[b])
            if not A or not B:
                continue
            ov = len(A & B) / min(len(A), len(B))
            if ov > threshold:
                pairs.append((a, b, round(ov, 2)))
    return pairs


def root_commits(repo: Path, ref: str) -> list[str]:
    out = run(["git", "rev-list", "--max-parents=0", ref], cwd=repo, check=False)
    return sorted(s[:7] for s in out.split())
