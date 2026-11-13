"""Git history and pull request columns, using the PRD definitions."""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone
from pathlib import Path

from .platforms import pr_tier
from .util import AI_SIGNATURE_RE, log, parse_date, run

GENERIC_LOCAL = {"admin", "dev", "info", "root", "user", "git", "noreply", "no-reply", "ci",
                 "build", "test", "developer", "support", "hello", "contact", "me", "mail"}
PR_MERGE_RE = re.compile(r"^(Merge pull request #(\d+)|Merged in |Merge branch '.+' into |"
                         r"Merge remote-tracking branch)", re.I)
PR_SQUASH_RE = re.compile(r"\(#(\d+)\)\s*$|^Squashed commit|See merge request !(\d+)", re.I)


def is_bot_author(name: str, email: str, cfg: dict) -> bool:
    text = f"{name} {email}".lower()
    return any(re.search(p, text, re.I) for p in cfg["bot_patterns"])


def default_branch(repo: Path, api_default: str | None) -> str:
    if api_default:
        return api_default
    out = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo, check=False).strip()
    if out:
        return out.split("/", 1)[-1]
    return run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=False).strip()


def ref_counts(repo: Path) -> tuple[int, int]:
    branches = [b.strip() for b in run(["git", "branch", "-r"], cwd=repo, check=False).splitlines()
                if b.strip() and "->" not in b]
    if not branches:   # local folder without remotes
        branches = run(["git", "branch"], cwd=repo, check=False).splitlines()
    tags = run(["git", "tag"], cwd=repo, check=False).split()
    return len(branches), len(tags)


def non_merge_count(repo: Path, ref: str) -> int:
    """the buyer's command: git log --oneline --no-merges | grep -v -i revert | wc -l"""
    out = run(["git", "log", "--oneline", "--no-merges", ref], cwd=repo, timeout=1800, check=False)
    return sum(1 for line in out.splitlines() if "revert" not in line.lower())


def authors(commits: list[dict], cfg: dict) -> dict:
    """Merge aliases: same email, same email local part, same normalised name, config map."""
    parent: dict[str, str] = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    alias = {k.lower(): v.lower() for k, v in (cfg.get("author_aliases") or {}).items()}
    ident = {}
    for c in commits:
        name, email = c["author_name"].strip(), c["author_email"].strip().lower()
        key = f"e:{email}" if email else f"n:{name.lower()}"
        ident[key] = (name, email)
        find(key)
        nname = re.sub(r"[^a-z0-9]", "", name.lower())
        if len(nname) >= 3:
            union(key, f"n:{nname}")
        local = email.split("@")[0].split("+")[-1] if "@" in email else ""
        if len(local) >= 3 and local not in GENERIC_LOCAL:
            union(key, f"l:{local}")
        for a in (name.lower(), email):
            if a in alias:
                union(key, f"a:{alias[a]}")
    groups: dict[str, dict] = {}
    for c in commits:
        email = c["author_email"].strip().lower()
        key = f"e:{email}" if email else f"n:{c['author_name'].strip().lower()}"
        root = find(key)
        g = groups.setdefault(root, {"names": set(), "commits": 0, "bot": False})
        g["names"].add(c["author_name"])
        g["commits"] += 1
        g["bot"] = g["bot"] or is_bot_author(c["author_name"], email, cfg)
    humans = {k: g for k, g in groups.items() if not g["bot"]}
    bots = {k: g for k, g in groups.items() if g["bot"]}
    bot_commits = sum(g["commits"] for g in bots.values())
    import hashlib
    return {
        "contributors_raw": len(ident),
        "contributors_merged": len(humans),
        "bot_authors": len(bots),
        "bot_author_names": sorted({n for g in bots.values() for n in g["names"]})[:50],
        "bot_commit_ratio": round(bot_commits / len(commits), 4) if commits else 0,
        "author_keys": sorted(hashlib.sha1(k.encode()).hexdigest()[:12] for k in humans),
    }


def _iso_utc(d) -> str:
    """Catalogue format, as in the reference output: 2018-03-12T10:22:17 (UTC)."""
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") if d else ""


def history(repo: Path, ref: str, commits: list[dict], api_default: str | None, cfg: dict,
            refs_repo: Path | None = None) -> dict:
    snapshot = run(["git", "rev-parse", ref], cwd=repo, check=False).strip()
    branches, tags = ref_counts(refs_repo or repo)
    db = default_branch(repo, api_default)
    dated = [(parse_date(c["author_date"]), c["author_date"]) for c in commits]
    dated = [d for d in dated if d[0]]
    first = min(dated, key=lambda d: d[0]) if dated else (None, "")
    last = max(dated, key=lambda d: d[0]) if dated else (None, "")
    roots = sorted(s[:7] for s in run(["git", "rev-list", "--max-parents=0", ref], cwd=repo,
                                      check=False).split())
    a = authors(commits, cfg)
    total = int(run(["git", "rev-list", "--count", ref], cwd=repo, check=False).strip() or 0)
    return {
        "snapshot_sha": snapshot,
        "default_branch": db,
        "branches": branches,
        "tags": tags,
        "total_commits": total,
        "non_merge_commits": non_merge_count(repo, ref),
        "first_commit": _iso_utc(first[0]),
        "last_commit": _iso_utc(last[0]),
        "first_commit_local": first[1],
        "last_commit_local": last[1],
        "span_days": (last[0] - first[0]).days if first[0] and last[0] else "",
        "recency_days": (datetime.now(timezone.utc) - last[0]).days if last[0] else "",
        "root_commit": roots[0] if roots else "",
        "root_commits": roots,
        **a,
        "commands": {
            "total_commits": f"git rev-list --count {ref}",
            "non_merge_commits": f"git log --oneline --no-merges {ref} | grep -v -i revert | wc -l",
            "root_commit": f"git rev-list --max-parents=0 {ref}",
        },
    }


def _excluded(path: str, cfg: dict) -> bool:
    from .classify import GENERATED_NAME_RE, _in_dirs
    name = path.rsplit("/", 1)[-1]
    return (name in set(cfg["lock_files"]) or bool(GENERATED_NAME_RE.search(path))
            or _in_dirs(path, cfg["generated_dirs"]) or _in_dirs(path, cfg["vendored_dirs"]))


def _diff_stats(repo: Path, sha: str, parent: str | None, cfg: dict) -> tuple[int, int] | None:
    args = ["git", "diff", "--numstat", parent, sha] if parent else ["git", "show", "--numstat",
                                                                       "--format=", sha]
    out = run(args, cwd=repo, timeout=120, check=False)
    if not out and parent is None:
        return None
    loc = files = 0
    for line in out.splitlines():
        bits = line.split("\t")
        if len(bits) != 3:
            continue
        a, d, path = bits
        if _excluded(path, cfg):
            continue
        files += 1
        loc += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
    return loc, files


def pr_stats(repo: Path, ref: str, commits: list[dict], prs: list[dict] | None,
             total_prs, cfg: dict, budget_s: int = 900, merged_total=None) -> dict:
    """Merged PR sizes and tiers. API PRs when given, else merge and squash markers."""
    import time
    t0 = time.time()
    by_sha = {c["sha"]: c for c in commits}
    first_parent = run(["git", "log", "--first-parent", "--format=%H%x1f%P%x1f%s", ref],
                       cwd=repo, timeout=1800, check=False).splitlines()
    units = []
    if prs is not None:
        source = "Platform API"
        for p in prs:
            sha = p.get("merge_commit") or ""
            c = by_sha.get(sha) or next((v for k, v in by_sha.items() if sha and k.startswith(sha)), None)
            units.append({"number": p["number"], "sha": c["sha"] if c else "", "api": p,
                          "body_ai": bool(AI_SIGNATURE_RE.search(f"{p.get('title') or ''}\n{p.get('bodyText') or ''}"))})
    else:
        source = "Commit-message inferred"
        for line in first_parent:
            sha, parents, subj = (line.split("\x1f") + ["", ""])[:3]
            ps = parents.split()
            m = PR_MERGE_RE.search(subj) if len(ps) > 1 else None
            s = PR_SQUASH_RE.search(subj) if len(ps) <= 1 else None
            if m or s:
                num = (m.group(2) if m else (s.group(1) or s.group(2))) if (m or s) else None
                units.append({"number": num or sha[:12], "sha": sha, "api": None, "body_ai": False})
    sample, sizes, tiers = [], [], {"simple": 0, "standard": 0, "rich": 0}
    signals = []
    timed_out = False
    for u in units:
        if time.time() - t0 > budget_s:
            timed_out = True
            break
        loc_files = None
        sha = u["sha"]
        c = by_sha.get(sha) if sha else None
        if c:
            parent = c["parents"][0] if len(c["parents"]) > 1 else None
            loc_files = _diff_stats(repo, sha, parent, cfg)
        if loc_files is None and u["api"]:
            fs = [f for f in u["api"].get("file_stats", []) if f.get("path") and not _excluded(f["path"], cfg)]
            if fs and u["api"].get("files_complete", True):
                loc_files = (sum(f["additions"] + f["deletions"] for f in fs), len(fs))
            else:
                loc_files = ((u["api"].get("additions") or 0) + (u["api"].get("deletions") or 0),
                             u["api"].get("changedFiles") or 0)
        if loc_files is None:
            continue
        loc, files = loc_files
        t = pr_tier(loc, files, cfg["pr_bands"])
        tiers[t] += 1
        sizes.append(loc)
        if len(sample) < 50:
            sample.append({"number": u["number"], "changed_loc": loc, "files": files, "class": t})
        # PR commits, for AI and open source signals
        pr_commits = []
        if c:
            if len(c["parents"]) > 1:
                out = run(["git", "rev-list", f"{c['parents'][0]}..{c['sha']}", "--no-merges"],
                          cwd=repo, check=False).split()
                pr_commits = [by_sha[s] for s in out if s in by_sha]
            else:
                pr_commits = [c]
        signed = u["body_ai"] or any(x["ai_signature"] for x in pr_commits)
        signals.append({"number": str(u["number"]), "commit": sha, "changed_loc": loc,
                        "ai_signed": signed, "ai_loc": loc if signed else 0, "oss_loc": 0})
    n = sum(tiers.values())
    reviewed = (sum(1 for p in prs if (p.get("approvals") or 0) > 0) if prs is not None else "")
    return {
        "source": source if (n or prs is not None) else "Not available",
        "prs_total": total_prs if prs is not None else "",
        # With --max-prs only a sample is read; the platform's own merged total still goes in the catalogue.
        "prs_merged": (merged_total if (prs is not None and isinstance(merged_total, int)
                                        and merged_total > len(units)) else
                       (len(units) if units or prs is not None else 0)),
        "prs_sampled": len(units) if prs is not None else "",
        "prs_reviewed": reviewed,
        "avg_changed_loc": round(sum(sizes) / len(sizes)) if sizes else "",
        "median_changed_loc": int(statistics.median(sizes)) if sizes else "",
        "bands": {"simple": f"<={cfg['pr_bands']['simple_max_loc']} LOC and <={cfg['pr_bands']['simple_max_files']} files",
                  "rich": f">{cfg['pr_bands']['rich_min_loc']} LOC or >{cfg['pr_bands']['rich_min_files']} files"},
        "share": {k: round(v / n, 2) for k, v in tiers.items()} if n else {},
        "classified": n,
        "excluded_from_diff": "lock files, generated and vendored files",
        "time_budget_hit": timed_out,
        "sample": sample,
        "_signals": signals,
    }


def ai_signals(commits: list[dict], signals: list[dict], cfg: dict, llm_score=None,
               comment_ratio=None) -> tuple[str, dict]:
    """Evidence only, no verdict."""
    own = [c for c in commits if len(c["parents"]) <= 1]
    adds = sorted((sum(f["additions"] for f in c["files"]) for c in own), reverse=True)
    total = sum(adds)
    ev = {}
    parts = []
    if total and len(own) > 3:
        top3 = sum(adds[:3]) / total
        ev["top3_commit_share"] = round(top3, 3)
        if top3 > cfg["ai_bulk_commit_share"]:
            parts.append(f"{top3:.0%} of lines added in 3 commits")
    elif total and own:
        ev["top3_commit_share"] = 1.0
        parts.append(f"all lines added in {len(own)} commit{'s' if len(own) != 1 else ''}")
    n_ai = sum(1 for c in commits if c["ai_signature"])
    ev["ai_tool_commits"] = n_ai
    if n_ai:
        parts.append(f"AI tool signature in {n_ai} commit{'s' if n_ai != 1 else ''}")
    pr_ai = sum(1 for s in signals if s.get("ai_signed"))
    ev["ai_signed_prs"] = pr_ai
    if pr_ai:
        parts.append(f"{pr_ai} PR{'s' if pr_ai != 1 else ''} with an AI signature")
    if comment_ratio not in ("", None) and comment_ratio > 0.35:
        parts.append(f"high comment and docstring density ({comment_ratio:.2f})")
        ev["boilerplate_density"] = comment_ratio
    if llm_score not in ("", None):
        parts.append(f"LLM review score {llm_score}/100")
        ev["llm_score"] = llm_score
    return ("; ".join(parts) if parts else "None detected"), ev
