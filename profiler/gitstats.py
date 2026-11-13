"""Clone a repo and read its history with git. Local only, never pushes."""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .util import AI_SIGNATURE_RE, is_bot, log, parse_date, run

REC, FLD = "\x1e", "\x1f"


def clone(url: str, dest: Path, auth_header: str | None, timeout: int,
          retries: int, extended_timeout: int) -> None:
    """Full clone (history is needed). Token goes in a header, never in the URL."""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    cfg = ["-c", f"http.extraHeader={auth_header}"] if auth_header else []
    attempts = [timeout] * (retries + 1) + ([extended_timeout] if extended_timeout else [])
    last = None
    for i, t in enumerate(attempts):
        try:
            run(["git", *cfg, "clone", "--quiet", url, str(dest)], timeout=t)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            shutil.rmtree(dest, ignore_errors=True)
            msg = str(e).lower()
            if "not found" in msg or "authentication" in msg or "403" in msg:
                break
            log.warning("clone attempt %d failed (%s), retrying", i + 1, type(e).__name__)
    raise RuntimeError(f"clone failed: {last}")


def measured_branch(repo: Path, default_branch: str | None) -> tuple[str, str]:
    """Return (branch name, ref to read) and check the branch out for LOC counting."""
    if default_branch:
        for ref in (f"refs/remotes/origin/{default_branch}", f"refs/heads/{default_branch}"):
            if run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo, check=False).strip():
                run(["git", "checkout", "--quiet", "--detach", ref], cwd=repo, check=False)
                return default_branch, ref
    name = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=False).strip()
    return name or "HEAD", "HEAD"


def read_commits(repo: Path, branch: str = "HEAD") -> list[dict]:
    """Every commit reachable from the branch, with per file line counts."""
    fmt = REC + FLD.join(["%H", "%P", "%aN", "%aE", "%aI", "%s", "%b", "%(trailers:only,unfold)"]) + FLD
    try:
        out = run(["git", "log", branch, "--no-renames", "--numstat", f"--format={fmt}"],
                  cwd=repo, timeout=1800)
    except RuntimeError as e:
        if "does not have any commits" in str(e) or "unknown revision" in str(e):
            return []
        raise
    commits = []
    for rec in out.split(REC)[1:]:
        parts = rec.split(FLD)
        if len(parts) < 9:
            continue
        sha, parents, name, email, date, subject, body, trailers, rest = parts[:9]
        files = []
        for line in rest.strip().splitlines():
            bits = line.split("\t")
            if len(bits) != 3:
                continue
            a, d, path = bits
            files.append({"path": path,
                          "additions": int(a) if a.isdigit() else 0,
                          "deletions": int(d) if d.isdigit() else 0})
        text = f"{subject}\n{body}\n{trailers}\n{name} {email}"
        commits.append({
            "sha": sha,
            "parents": parents.split(),
            "author_name": name,
            "author_email": email,
            "author_date": date,
            "subject": subject,
            "is_bot": is_bot(name, email),
            "ai_signature": bool(AI_SIGNATURE_RE.search(text)),
            "files": files,
        })
    return commits


def git_stats(commits: list[dict]) -> dict:
    if not commits:
        return {"total_commits": 0, "first_commit": "", "last_commit": "", "span_days": 0,
                "recency_days": "", "human_authors": 0, "bot_authors": 0,
                "bot_commit_count": 0, "bot_commit_ratio": 0.0, "authors": [],
                "merge_commits": 0}
    authors: dict[tuple, dict] = {}
    for c in commits:
        key = (c["author_name"], c["author_email"].lower())
        a = authors.setdefault(key, {"name": key[0], "email": c["author_email"],
                                     "commits": 0, "is_bot": c["is_bot"]})
        a["commits"] += 1
    dated = [(parse_date(c["author_date"]), c["author_date"]) for c in commits]
    dated = [d for d in dated if d[0]]
    first = min(dated, key=lambda d: d[0])
    last = max(dated, key=lambda d: d[0])
    bot_commits = sum(1 for c in commits if c["is_bot"])
    # Count people, not email addresses: merge identities that share a name.
    humans = {a["name"].strip().lower() for a in authors.values() if not a["is_bot"]}
    bots = {a["name"].strip().lower() for a in authors.values() if a["is_bot"]}
    return {
        "total_commits": len(commits),
        "first_commit": first[1],
        "last_commit": last[1],
        "span_days": (last[0] - first[0]).days,
        "recency_days": (datetime.now(timezone.utc) - last[0]).days,
        "human_authors": len(humans),
        "bot_authors": len(bots),
        "bot_commit_count": bot_commits,
        "bot_commit_ratio": round(bot_commits / len(commits), 4),
        "authors": sorted(authors.values(), key=lambda a: -a["commits"]),
        "merge_commits": sum(1 for c in commits if len(c["parents"]) > 1),
    }


def first_parent_merges(repo: Path, branch: str) -> list[str]:
    out = run(["git", "log", branch, "--first-parent", "--merges", "--format=%H"],
              cwd=repo, check=False)
    return out.split()


_PR_NUM_RE = re.compile(r"(?:#|!)(\d+)\b")


def pr_signals(repo: Path, commits: list[dict], prs: list[dict], branch: str,
               oss_paths: set[str], code_paths: set[str] | None = None,
               time_budget_s: int = 600) -> list[dict] | None:
    """Per PR: changed lines, lines in open source files, lines from AI signed commits.

    With API PRs, a PR's commits are found from its merge commit. Without them
    (local clones) each first parent merge on the branch is treated as a PR.
    Returns None when the time budget runs out.
    """
    import time
    t0 = time.time()
    by_sha = {c["sha"]: c for c in commits}

    def lines(c: dict) -> tuple[int, int, int]:
        tot = oss = 0
        for f in c["files"]:
            n = f["additions"] + f["deletions"]
            tot += n
            if f["path"] in oss_paths:
                oss += n
        return tot, oss, (tot if c["ai_signature"] else 0)

    def find(sha: str) -> dict | None:
        c = by_sha.get(sha)
        if c is None and sha and len(sha) < 40:  # Bitbucket gives 12 character hashes
            c = next((v for k, v in by_sha.items() if k.startswith(sha)), None)
        return c

    def pr_commits(sha: str) -> list[dict]:
        c = find(sha)
        if not c:
            return []
        if len(c["parents"]) > 1:
            out = run(["git", "rev-list", f"{c['parents'][0]}..{c['sha']}", "--no-merges"],
                      cwd=repo, check=False).split()
            return [by_sha[s] for s in out if s in by_sha]
        return [c]

    units: list[tuple[str, str, bool]] = []
    if prs:
        for pr in prs:
            body_ai = bool(AI_SIGNATURE_RE.search(f"{pr.get('title','')}\n{pr.get('bodyText','')}"))
            units.append((str(pr["number"]), pr.get("merge_commit", ""), body_ai))
    else:
        for sha in first_parent_merges(repo, branch):
            subj = by_sha.get(sha, {}).get("subject", "")
            m = _PR_NUM_RE.search(subj)
            units.append((m.group(1) if m else sha[:12], sha, False))

    signals = []
    for number, sha, body_ai in units:
        if time.time() - t0 > time_budget_s:
            log.warning("PR signal time budget hit after %d PRs", len(signals))
            return None
        cs = pr_commits(sha) if sha else []
        tot = oss = ai = 0
        for c in cs:
            t, o, a = lines(c)
            tot, oss, ai = tot + t, oss + o, ai + a
        signed = body_ai or any(c["ai_signature"] for c in cs)
        if body_ai:
            ai = tot
        full = (find(sha) or {}).get("sha", sha) if sha else sha
        signals.append({"number": number, "commit": full, "changed_loc": tot,
                        "oss_loc": oss, "ai_loc": ai, "ai_total_loc": tot,
                        "ai_signed": signed})
    return signals


def signal_cell(repo_value, signals: list[dict] | None, key: str) -> str:
    """'<repo value>,<PRs affected>[,>50%:<n>]', or just the repo value."""
    rv = "-" if repo_value is None or repo_value == "" else repo_value
    if signals is None:
        return str(rv)
    affected = [s for s in signals if s[key] > 0 or (key == "ai_loc" and s.get("ai_signed"))]
    over = [s for s in affected if s["changed_loc"] and s[key] / s["changed_loc"] > 0.5]
    cell = f"{rv},{len(affected)}"
    if over:
        cell += f",>50%:{len(over)}"
    return cell
