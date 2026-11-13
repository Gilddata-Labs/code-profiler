"""Extra metrics for the GildScore report card: code vintage, team, PR process."""
from __future__ import annotations

import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .util import log, parse_date, pct, run


def code_vintage(repo: Path, files: list[str], per_file_loc: dict, cutoff: datetime,
                 budget_s: int = 900) -> dict:
    """How much of today's code was last written before vs after the cutoff (git blame).

    Files in a fixed random order until the time budget runs out; the rest is
    estimated from the blamed share (blame_coverage_pct says how much was read).
    """
    t0 = time.time()
    cut = int(cutoff.timestamp())
    by_year: Counter = Counter()
    pre_lines = post_lines = 0
    blamed_loc = 0
    total_loc = sum(per_file_loc.get(f, 0) for f in files)
    import random
    order = sorted(files)
    random.Random(42).shuffle(order)   # unbiased, repeatable sample when the budget runs out
    for f in order:
        if time.time() - t0 > budget_s:
            log.info("blame time budget reached; the rest is estimated")
            break
        loc = per_file_loc.get(f, 0)
        if not loc:
            continue
        out = run(["git", "blame", "--line-porcelain", "-w", "HEAD", "--", f], cwd=repo,
                  timeout=300, check=False)
        stamps = [int(l.split()[1]) for l in out.splitlines() if l.startswith("author-time ")]
        if not stamps:
            continue
        pre = sum(1 for s in stamps if s < cut)
        share_pre = pre / len(stamps)
        pre_lines += round(loc * share_pre)
        post_lines += loc - round(loc * share_pre)
        for s in stamps:
            by_year[datetime.fromtimestamp(s, timezone.utc).year] += loc / len(stamps)
        blamed_loc += loc
    coverage = pct(blamed_loc, total_loc)
    if blamed_loc and blamed_loc < total_loc:
        scale = total_loc / blamed_loc
        pre_lines, post_lines = round(pre_lines * scale), round(post_lines * scale)
        by_year = Counter({y: v * scale for y, v in by_year.items()})
    return {
        "code_loc": total_loc,
        "loc_pre_cutoff": pre_lines,
        "loc_post_cutoff": post_lines,
        "pct_loc_pre_cutoff": pct(pre_lines, pre_lines + post_lines),
        "loc_by_year": "; ".join(f"{y}:{round(v)}" for y, v in sorted(by_year.items())),
        "blame_coverage_pct": coverage,
    }


def history_metrics(commits: list[dict], cutoff: datetime) -> dict:
    """Team, activity and bulk import signals from the commit list."""
    if not commits:
        return {}
    human = [c for c in commits if not c["is_bot"]]
    now = datetime.now(timezone.utc)
    recent_authors = set()
    months = set()
    added_pre = added_post = 0
    per_author: Counter = Counter()
    adds_per_commit = []
    for c in commits:
        d = parse_date(c["author_date"])
        added = sum(f["additions"] for f in c["files"])
        if len(c["parents"]) <= 1:
            adds_per_commit.append(added)
        if not d:
            continue
        months.add((d.year, d.month))
        if d < cutoff:
            added_pre += added
        else:
            added_post += added
        if not c["is_bot"]:
            key = (c["author_email"] or c["author_name"]).lower()
            per_author[key] += 1
            if now - d <= timedelta(days=90):
                recent_authors.add(key)
    total_h = sum(per_author.values())
    bus, acc = 0, 0
    for _, n in per_author.most_common():
        bus += 1
        acc += n
        if acc >= total_h / 2:
            break
    total_added = sum(adds_per_commit)
    return {
        "active_authors_90d": len(recent_authors),
        "bus_factor": bus if total_h else 0,
        "top_author_commit_pct": pct(per_author.most_common(1)[0][1], total_h) if total_h else "",
        "active_months": len(months),
        "commits_per_active_month": round(len(commits) / len(months), 1) if months else "",
        "lines_added_pre_cutoff_pct": pct(added_pre, added_pre + added_post),
        "largest_commit_loc_pct": pct(max(adds_per_commit), total_added) if total_added else "",
        "human_commit_pct": pct(len(human), len(commits)),
    }


def pr_process(prs: list[dict], signals: list[dict] | None, loc: int,
               merges_fallback: int | str) -> dict:
    """Ratios over merged PRs: size, review, linked issues, time to merge."""
    n = len(prs)
    units = n or (merges_fallback if isinstance(merges_fallback, int) else 0)
    sizes = ([p.get("additions", 0) + p.get("deletions", 0) for p in prs] if prs
             else [s["changed_loc"] for s in (signals or [])])
    out = {
        "loc_per_pr": round(loc / units, 1) if units else "",
        "median_pr_loc": int(statistics.median(sizes)) if sizes else "",
        "commits_per_pr": "",
        "pr_review_pct": "",
        "pr_linked_issue_pct": "",
        "median_hours_to_merge": "",
        "ai_pr_pct": "",
    }
    if signals:
        out["ai_pr_pct"] = pct(sum(1 for s in signals if s.get("ai_signed") or s["ai_loc"] > 0),
                               len(signals))
    if not n:
        return out
    commits = [(p.get("commits") or {}).get("totalCount") for p in prs]
    commits = [c for c in commits if isinstance(c, int)]
    if commits:
        out["commits_per_pr"] = round(sum(commits) / len(commits), 1)
    reviewed = sum(1 for p in prs if (p.get("reviews") or {}).get("totalCount")
                   or (p.get("reviewThreads") or {}).get("totalCount"))
    linked = sum(1 for p in prs if (p.get("closingIssuesReferences") or {}).get("nodes"))
    hours = []
    for p in prs:
        a, b = parse_date(p.get("createdAt")), parse_date(p.get("mergedAt"))
        if a and b and b >= a:
            hours.append((b - a).total_seconds() / 3600)
    out.update({
        "pr_review_pct": pct(reviewed, n),
        "pr_linked_issue_pct": pct(linked, n),
        "median_hours_to_merge": round(statistics.median(hours), 1) if hours else "",
    })
    return out
