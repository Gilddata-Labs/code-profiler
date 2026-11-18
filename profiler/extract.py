"""Catalogue extractor (PRD v2): one command on a supplier's repos gives one zip
with every column that can be computed from code and git history, plus the raw
evidence behind each number. Supplier and GildData columns are left blank and
listed in qc/missing_fields.csv, never guessed."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import shutil
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import __version__
from . import catalogue as cat
from .classify import classify, non_authored_scc, write_file_classes, SCC_GENERATED_CMD
from .dup import (builtin_dup, cross_repo_builtin, cross_repo_files, cross_repo_jscpd, file_hashes,
                  jscpd_available, jscpd_repo, overlap_groups, run_jscpd, stage_sources)
from .gitfacts import ai_signals, history, pr_stats
from .gitstats import clone, measured_branch, read_commits
from .metrics import code_vintage, history_metrics
from .platforms import Bitbucket, GitHub, GitLab
from .scans import (build_hints, oss_hints, pii_scan, readme_description, repo_type,
                    secrets_scan)
from .structure import analyze as structure_analyze
from .structure import tree_sitter_available
from .util import (RETRYABLE, classify_error, log, now_iso, pct, read_json, redact, run, safe_name,
                   stamp, write_json)

# Raw metric columns (snake case) kept next to the catalogue for GildScore and compare.
RAW_COLUMNS = [
    "full_name", "platform", "org", "repo", "logical_loc", "physical_loc", "non_authored_loc",
    "data_markup_loc", "source_loc", "test_loc", "source_files", "test_files", "total_files",
    "size_kb", "primary_language", "language_breakdown", "category", "repo_type",
    "duplication_ratio", "cross_repo_duplication_ratio", "functions", "classes_types",
    "parser_coverage", "test_to_code_ratio", "untested_file_share", "comment_docstring_ratio",
    "has_tests", "has_test_runner", "has_ci_cd", "has_library_code", "default_branch",
    "branches", "tags", "total_commits", "non_merge_commits", "first_commit", "last_commit",
    "span_days", "recency_days", "contributors", "bot_authors", "bot_commit_ratio",
    "prs_total", "prs_merged", "prs_reviewed", "avg_loc_per_pr", "median_pr_loc",
    "pr_simple", "pr_standard", "pr_rich", "pr_data_source", "ai_pr_pct", "ai_signals_text",
    "llm_ai_score", "open_source_loc_pct", "secrets_result", "pii_result", "loc_pre_cutoff",
    "loc_post_cutoff", "pct_loc_pre_cutoff", "loc_by_year", "active_authors_90d",
    "bus_factor", "top_author_commit_pct", "active_months", "commits_per_active_month",
    "largest_commit_loc_pct", "code_loc", "blame_coverage_pct", "codebase_description", "industry_domain_llm", "cutoff_date",
    "snapshot_sha", "error", "error_class",
]


def _hash(value: str) -> str:
    return "h-" + hashlib.sha256(value.strip().lower().encode()).hexdigest()[:12]


class Target:
    def __init__(self, platform: str, full_name: str, org: str, meta: dict | None = None,
                 path: Path | None = None, url: str | None = None):
        self.platform, self.full_name, self.org = platform, full_name, org
        self.meta, self.path, self.url = meta or {}, path, url

    @property
    def key(self) -> str:
        return safe_name(self.full_name)


class Extractor:
    def __init__(self, args, out_root: Path):
        self.a = args
        self.cfg = args.cfg
        self.out_root = out_root
        self.lock = threading.Lock()
        self.gh = GitHub(args.github_token, args.github_host) if args.github_token else None
        self.gl = GitLab(args.gitlab_token, args.gitlab_host) if args.gitlab_token else None
        self.bb = (Bitbucket(args.bitbucket_token, args.bitbucket_email)
                   if args.bitbucket_token else None)
        self.stage_root: Path | None = None

    # ------------------------------------------------------------ targets
    def discover(self) -> list[Target]:
        a, targets = self.a, []
        for org in a.github_org:
            for r in self._need(self.gh, "GitHub").list_repos(org):
                if (a.include_forks or not r.get("fork")) and (a.include_archived or not r.get("archived")):
                    targets.append(Target("github", r["full_name"], org, r))
        for full in a.github_repo:
            targets.append(Target("github", full.strip("/"), full.split("/")[0]))
        for grp in a.gitlab_group:
            for r in self._need(self.gl, "GitLab").list_repos(grp):
                if a.include_forks or not r.get("forked_from_project"):
                    targets.append(Target("gitlab", r["path_with_namespace"], grp, r))
        for full in a.gitlab_project:
            targets.append(Target("gitlab", full.strip("/"), full.split("/")[0]))
        for ws in a.bitbucket_workspace:
            for r in self._need(self.bb, "Bitbucket").list_repos(ws):
                if a.include_forks or not r.get("parent"):
                    targets.append(Target("bitbucket", r["full_name"], ws))
        for full in a.bitbucket_repo:
            targets.append(Target("bitbucket", full.strip("/"), full.split("/")[0]))
        if a.repos_file:   # PRD: --repos repos.json, list of {platform, org, full_name, url}
            for r in json.loads(Path(a.repos_file).read_text(encoding="utf-8")):
                plat = (r.get("platform") or "github").lower()
                targets.append(Target(plat, r["full_name"], r.get("org") or r["full_name"].split("/")[0],
                                      url=r.get("url")))
        for folder in a.local_dir:
            root = Path(folder).expanduser().resolve()
            repos = [root] if (root / ".git").exists() else sorted(
                p for p in root.iterdir() if p.is_dir() and (p / ".git").exists())
            for p in repos:
                owner = safe_name(a.label) if a.label else root.name
                targets.append(Target("local", f"{owner}/{p.name}", owner, path=p))
        seen, out = set(), []
        for t in targets:
            if (t.platform, t.full_name.lower()) not in seen:
                seen.add((t.platform, t.full_name.lower()))
                out.append(t)
        return out[: a.limit] if a.limit else out

    @staticmethod
    def _need(client, name):
        if client is None:
            raise SystemExit(f"A {name} token is needed for that. See README.")
        return client

    def client(self, t: Target):
        return {"github": self.gh, "gitlab": self.gl, "bitbucket": self.bb}.get(t.platform)

    def api_on(self, t: Target) -> bool:
        return self.a.mode != "offline" and self.client(t) is not None

    # ------------------------------------------------------------ run
    def run(self, resume: Path | None = None) -> Path:
        if resume:
            job_dir = resume
            targets = [Target(**{k: v for k, v in d.items() if k != "meta"})
                       for d in read_json(job_dir / "targets.json", [])]
            for t in targets:
                t.path = Path(t.path) if t.path else None
        else:
            targets = self.discover()
            if not targets:
                raise SystemExit("No repositories found for the given inputs.")
            orgs = {t.org for t in targets}
            label = safe_name(self.a.label or (next(iter(orgs)) if len(orgs) == 1 else "multi"))
            self.run_id = stamp()
            job_dir = self.out_root / f"catalogue-{label}-{self.run_id}"
            job_dir.mkdir(parents=True)
            write_json(job_dir / "targets.json", [
                {"platform": t.platform, "full_name": t.full_name, "org": t.org,
                 "path": str(t.path) if t.path else None, "url": t.url} for t in targets])
        self.run_id = job_dir.name.rsplit("-", 1)[-1]
        self.job_dir = job_dir
        self.work = job_dir / ".work"          # never zipped
        self.work.mkdir(exist_ok=True)
        (self.work / "prs").mkdir(exist_ok=True)   # merged PR page checkpoints, never zipped
        self.stage_root = self.work / "stage" if jscpd_available() else None
        self._setup_logging(job_dir)
        self._diagnostics(len(targets))
        self.job = read_json(job_dir / "job.json") or {"created_at": now_iso(), "total": len(targets), "repos": {}}
        # A repo counts as done only if its facts file is still there: after a completed run the
        # work folder is deleted (unless --keep-work), so a later --resume must extract again
        # instead of writing EXTRACT FAILED rows for every repo.
        todo = [t for t in targets if self.job["repos"].get(t.full_name, {}).get("status") != "ok"
                or not (self.work / "facts" / f"{t.key}.json").exists()]
        if resume and len(todo) == len(targets) and self.job["repos"]:
            log.warning("Resume: the work folder of this run was already cleaned up, so all %d repos "
                        "are extracted again (use --keep-work to keep facts for later resumes)", len(todo))
        log.info("This pass will process %d of %d repos", len(todo), len(targets))
        self._pass(todo)
        retry = [t for t in todo if self.job["repos"][t.full_name].get("error_class") in RETRYABLE]
        if retry:
            log.info("Retrying %d repos that failed on timeout, network or rate limit", len(retry))
            self._pass(retry)
        self._finish(targets)
        zpath = zip_job(job_dir)
        if not self.a.keep_work:
            shutil.rmtree(self.work, ignore_errors=True)
        log.info("Done. Output: %s", zpath)
        return zpath

    def _pass(self, targets: list[Target]) -> None:
        with ThreadPoolExecutor(max_workers=max(1, self.a.workers)) as ex:
            futs = {ex.submit(self._one_safe, t): t for t in targets}
            for i, fut in enumerate(as_completed(futs), 1):
                fut.result()
                vals = self.job["repos"].values()
                log.info("Progress %d/%d ok=%d failed=%d", i, len(targets),
                         sum(v["status"] == "ok" for v in vals), sum(v["status"] == "failed" for v in vals))

    def _one_safe(self, t: Target) -> None:
        try:
            facts = self._one(t)
            status, err, cls = "ok", "", ""
            log.info("OK %s: logical_loc=%s commits=%s merged_prs=%s", t.full_name,
                     facts.get("logical_loc"), facts.get("total_commits"), facts.get("prs_merged"))
        except Exception as e:  # noqa: BLE001
            status, err, cls = "failed", redact(str(e))[:500], classify_error(e)
            log.error("FAILED %s [%s]: %s", t.full_name, cls, err)
            log.debug("Traceback for %s", t.full_name, exc_info=True)   # extract.log with -v
            parts = t.full_name.split("/")
            facts = {"full_name": t.full_name, "platform": t.platform, "org": parts[0],
                     "repo": parts[-1], "error": err, "error_class": cls}
        with self.lock:
            write_json(self.work / "facts" / f"{t.key}.json", facts)
            self.job["repos"][t.full_name] = {"status": status, "error_class": cls, "error": err,
                                              "updated_at": now_iso()}
            self._save_job()

    def _save_job(self):
        j = self.job
        j["updated_at"] = now_iso()
        vals = j["repos"].values()
        j["progress"] = {"ok": sum(v["status"] == "ok" for v in vals),
                         "failed": sum(v["status"] == "failed" for v in vals), "done": len(j["repos"])}
        write_json(self.job_dir / "job.json", j)

    # ------------------------------------------------------------ one repo
    def _one(self, t: Target) -> dict:
        a, cfg = self.a, self.cfg
        ev = self.job_dir / "evidence" / t.key
        client = self.client(t)
        api = self.api_on(t)
        written: dict[str, int] = {}

        def save(name: str, data):
            written[f"evidence/{t.key}/{name}"] = write_json(ev / name, data)

        meta, prs, total_prs, check = t.meta, None, "", {}
        if api:
            meta = client.repo(t.full_name)
            check = {"platform": t.platform, "host": client.host, "started_at": now_iso(),
                     "before": {"counts": client.counts(t.full_name, meta), "refs": client.refs(t.full_name)}}
            prs = client.merged_prs(t.full_name, limit=a.max_prs or None,
                                    **({"checkpoint": str(self.work / "prs" / f"{t.key}.jsonl")}
                                       if t.platform == "github" else {}))
            total_prs = client.total_prs(t.full_name)
            save("api_repo.json", _slim_meta(meta, t.platform))
            save("api_merged_prs.json", [_slim_pr(p, a.include_text) for p in prs])
        import time as _t
        tick = {"t": _t.time()}
        timings: dict[str, float] = {}

        def lap(name):
            now = _t.time()
            timings[name] = round(now - tick["t"], 1)
            tick["t"] = now
        tmp = Path(tempfile.mkdtemp(prefix="gdp-"))
        try:
            work = tmp / "repo"
            if t.path:
                clone(str(t.path), work, None, a.clone_timeout, 0, 0, name=t.full_name)
            else:
                url = t.url or (client.clone_url(t.full_name) if client else None)
                if not url:
                    raise RuntimeError(f"no clone URL and no token for {t.platform}")
                clone(url, work, client.clone_auth_header() if client else None,
                      a.clone_timeout, a.clone_retries, a.clone_extended_timeout,
                      name=t.full_name, size_kb=(meta or {}).get("size"))
                run(["git", "fetch", "--quiet", "--tags", "origin"], cwd=work, timeout=a.clone_timeout, check=False)
            lap("api_and_clone")
            branch, ref = measured_branch(work, (meta or {}).get("default_branch"))
            commits = read_commits(work, ref)
            hist = history(work, ref, commits, (meta or {}).get("default_branch"), cfg,
                           refs_repo=t.path)   # local: count the supplier's own branches and tags
            lap("git_history")
            cl = classify(work, cfg)
            rows, vol = cl["files"], cl["volume"]
            scc_ev = non_authored_scc(work, cfg, rows) if cl["scc_used"] else None
            if scc_ev:
                vol["logical_loc"] = scc_ev["default_run"]["total_code"]
                vol["physical_loc"] = scc_ev["default_run"]["total_lines"]
                vol["non_authored_loc"] = scc_ev["non_authored_loc"]
                # Data/Markup LOC on the same scope as scc's Logical LOC: only files scc counted
                # (scc skips lock files and a few config types such as .gitignore and .cfg).
                data_langs = set(cfg["data_languages"])
                vol["data_markup_loc"] = sum(r["code_loc"] for r in rows
                                             if r.get("scc_counted") and r["language"] in data_langs)
            lap("classify_and_volume")
            struct = structure_analyze(work, rows, cfg)
            lap("structure")
            dup = jscpd_repo(work, cfg, any(r["class"] in ("vendored", "generated") and r["code_loc"]
                                            for r in rows)) if jscpd_available() else None
            fps = None
            if dup is None:
                dup = builtin_dup(work, rows, cfg)
                fps = dup.pop("_fingerprints")
                dup.pop("_window_count", None)
            lap("duplication")
            fhash = file_hashes(work, rows)
            if self.stage_root is not None:
                stage_sources(work, rows, self.stage_root / t.key, cfg["max_file_bytes"])
            prst = pr_stats(work, ref, commits, prs, total_prs, cfg, a.pr_time_budget,
                            merged_total=((check.get("before") or {}).get("counts") or {}).get("merged_prs"))
            lap("pull_requests")
            paths = [r["path"] for r in rows]
            hints = build_hints(work, paths)
            remotes = run(["git", "remote", "-v"], cwd=t.path, check=False) if t.path else ""
            oss = oss_hints(work, rows, meta, remotes)
            secrets = secrets_scan(work, rows, a.include_history_scan, cfg["max_file_bytes"])
            pii = pii_scan(work, rows, cfg["max_file_bytes"],
                           len({c["author_email"].lower() for c in commits}))
            lap("secrets_pii_build_hints")
            from .code import analyze_tree   # has_tests / runner / ci / library flags
            flags = analyze_tree(work, use_scc=False)
            desc = readme_description(work, paths)
            rtype = repo_type(t.full_name.split("/")[-1], rows, vol, flags["paths"]["has_library_code"], work)
            llm = None
            if a.llm:
                from .llm import analyze as llm_analyze
                llm = llm_analyze(work, a.llm, a.llm_key, a.llm_model or None)
            vintage = {} if a.skip_blame else code_vintage(
                work, [r["path"] for r in rows if r["class"] in ("production", "test")],
                {r["path"]: r["code_loc"] for r in rows if r["class"] in ("production", "test")},
                a.cutoff_dt, a.blame_time_budget)
            lap("llm_and_flags")
            hm = history_metrics(commits, a.cutoff_dt)
            lap("code_vintage_blame")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        log.info("Timings %s: %s", t.full_name, ", ".join(f"{k} {v}s" for k, v in timings.items()))
        if fps is not None:
            write_json(self.work / "fp" / f"{t.key}.json", sorted(fps))
        write_json(self.work / "fh" / f"{t.key}.json", fhash)
        ai_text, ai_ev = ai_signals(commits, prst["_signals"], cfg,
                                    (llm or {}).get("ai_code_score"), struct.get("comment_docstring_ratio"))
        signals = prst.pop("_signals")

        # ---- evidence
        save("scc.json", scc_ev or {
            "tool": "builtin line counter (scc not installed); install scc for buyer parity",
            "default_run": {"command": "builtin equivalent of: scc --format json .",
                            "languageSummary": _lang_summary(rows),
                            "total_code": vol["logical_loc"], "total_lines": vol["physical_loc"]},
            "generated_run": {"command": "builtin equivalent of: " + SCC_GENERATED_CMD,
                              "total_code": vol["logical_loc"] - vol["non_authored_loc"]},
            "non_authored_loc": vol["non_authored_loc"],
            "non_authored_rule": "code LOC of vendored and generated files (lock files, migrations, "
                                 "*.pb.go, *.min.js, dist/build, committed virtualenvs)"})
        save("jscpd-report.json", dup)
        save("git_stats.json", {k: v for k, v in hist.items() if k not in ("author_keys",)} |
             ({} if a.include_text else {"bot_author_names": [_hash(n) if "[bot]" not in n else n
                                                               for n in hist["bot_author_names"]]}))
        save("pr_stats.json", prst | {"ai": ai_ev, "pr_signals": signals[:2000]})
        save("structure.json", struct | {"source_files": vol["source_files"], "test_files": vol["test_files"]})
        write_file_classes(rows, ev / "file_classes.csv")
        written[f"evidence/{t.key}/file_classes.csv"] = (ev / "file_classes.csv").stat().st_size
        save("secrets_pii.json", {"secrets": secrets, "pii": pii,
                                  "note": "Values are never stored, only type, path and count."})
        save("build_hints.json", hints | {"oss_hints": oss})
        if llm:
            save("llm.json", llm)
        if vintage:
            save("code_vintage.json", vintage)
        cpath = ev / "commits.jsonl"
        with cpath.open("w", encoding="utf-8") as fh:
            for c in commits:
                fh.write(json.dumps(_slim_commit(c, a.include_text)) + "\n")
        written[f"evidence/{t.key}/commits.jsonl"] = cpath.stat().st_size
        if api:
            meta_after = client.repo(t.full_name)
            check["after"] = {"counts": client.counts(t.full_name, meta_after), "refs": client.refs(t.full_name)}
            check["finished_at"] = now_iso()
            check["files"] = dict(written)
            save("for_check.json", check)

        src, tst = vol["source_loc"], vol["test_loc"]
        oss_text = "; ".join(f"{h['path']}: {h['reason']}" for h in oss)[:500] or "None found by scan"
        n_pr = prst["classified"]
        parts = t.full_name.split("/")
        facts = {
            "full_name": t.full_name, "platform": t.platform, "org": t.org if t.platform != "local" else parts[0],
            "repo": parts[-1],
            "codebase_description": ((llm or {}).get("codebase_description") or desc),
            "industry_domain_llm": (llm or {}).get("industry_domain", ""),
            "repo_type": ((llm or {}).get("repo_type") if (llm or {}).get("repo_type") in _REPO_TYPES else rtype),
            **{k: vol[k] for k in ("total_files", "source_files", "test_files", "size_kb", "physical_loc",
                                   "logical_loc", "data_markup_loc", "non_authored_loc", "source_loc",
                                   "test_loc", "primary_language", "language_breakdown", "category")},
            "lang_code_loc": vol["lang_code_loc"],
            "duplication_ratio": dup["ratio"],
            "functions": struct["functions"], "classes_types": struct["classes_types"],
            "parser_coverage": struct["parser_coverage"],
            "test_to_code_ratio": round(tst / src, 4) if src else "",
            "untested_file_share": struct["untested_file_share"],
            "_matched_source_files": struct["matched_source_files"],
            "comment_docstring_ratio": struct["comment_docstring_ratio"],
            "has_tests": vol["test_files"] > 0 or flags["tests"]["has_tests"],
            "has_test_runner": flags["tests"]["has_test_runner"],
            "has_ci_cd": flags["tests"]["has_ci_cd"],
            "has_library_code": flags["paths"]["has_library_code"],
            "ai_signals_text": ai_text,
            "llm_ai_score": (llm or {}).get("ai_code_score", ""),
            "default_branch": hist["default_branch"], "branches": hist["branches"], "tags": hist["tags"],
            "total_commits": hist["total_commits"], "non_merge_commits": hist["non_merge_commits"],
            "first_commit": hist["first_commit"], "last_commit": hist["last_commit"],
            "span_days": hist["span_days"], "recency_days": hist["recency_days"],
            "contributors": hist["contributors_merged"], "bot_authors": hist["bot_authors"],
            "bot_commit_ratio": hist["bot_commit_ratio"], "snapshot_sha": hist["snapshot_sha"],
            "root_commits": hist["root_commits"], "_author_keys": hist["author_keys"],
            "prs_total": prst["prs_total"], "prs_merged": prst["prs_merged"],
            "prs_reviewed": prst["prs_reviewed"], "avg_loc_per_pr": prst["avg_changed_loc"],
            "median_pr_loc": prst["median_changed_loc"],
            "pr_simple": prst["share"].get("simple", "") if n_pr else "",
            "pr_standard": prst["share"].get("standard", "") if n_pr else "",
            "pr_rich": prst["share"].get("rich", "") if n_pr else "",
            "pr_data_source": prst["source"],
            "ai_pr_pct": round(100 * sum(1 for s in signals if s["ai_signed"]) / len(signals), 1) if signals else "",
            "build_command": hints["build_command"], "test_command": hints["test_command"],
            "runtime": hints["runtime"], "containerized": hints["containerized"],
            "oss_hint_text": oss_text,
            "open_source_loc_pct": round(100 * vol["non_authored_loc"] / vol["logical_loc"], 1) if vol["logical_loc"] else "",
            "secrets_result": secrets["status"], "secrets_count": secrets["findings"],
            "pii_result": pii["status"], "pii_count": pii["findings_total"],
            "cutoff_date": a.cutoff, **vintage, **hm,
            "_commit_shas": [c["sha"] for c in commits if len(c["parents"]) <= 1],
            "_pr_commits": {s["number"]: s["commit"] for s in signals},
            "_commit_loc": {c["sha"]: sum(f["additions"] + f["deletions"] for f in c["files"]) for c in commits},
            "_api_first_commit_hint": (meta or {}).get("created_at", ""),
        }
        return facts

    # ------------------------------------------------------------ roll up and write
    def _finish(self, targets: list[Target]) -> None:
        a, cfg, job_dir = self.a, self.cfg, self.job_dir
        facts = {}
        for t in targets:
            f = read_json(self.work / "facts" / f"{t.key}.json") or {"full_name": t.full_name, "error": "not run"}
            facts[t.full_name] = f
        ok = [t for t in targets if not facts[t.full_name].get("error")]
        keys = {t.full_name: t.key for t in targets}

        # duplication across repos and duplicate groups
        groups: list[dict] = []
        roots: dict[str, list[str]] = {}
        for t in ok:
            for r in facts[t.full_name].get("root_commits") or []:
                roots.setdefault(r, []).append(t.full_name)
        for t in ok:
            rs = facts[t.full_name].get("root_commits") or []
            facts[t.full_name]["shared_root_group_id"] = rs[0] if rs else ""
        for r, members in roots.items():
            if len(members) > 1:
                groups.append({"group_id": r, "method": "shared root commit",
                               "repos": ";".join(sorted(members)), "overlap": ""})
        hashes = {t.full_name: read_json(self.work / "fh" / f"{t.key}.json", {}) or {} for t in ok}
        for x, y, ov in overlap_groups(hashes, cfg["shared_file_overlap"]):
            gid = "f" + hashlib.sha1(f"{x}|{y}".encode()).hexdigest()[:6]
            groups.append({"group_id": gid, "method": "file hash overlap", "repos": f"{x};{y}", "overlap": ov})
        cross = None
        if self.stage_root is not None and len(ok) > 1:
            log.info("Running jscpd across all repos for cross-repo duplication")
            cross_k = cross_repo_jscpd(self.stage_root, [keys[t.full_name] for t in ok],
                                       {keys[t.full_name]: facts[t.full_name].get("source_loc", 0)
                                        + facts[t.full_name].get("test_loc", 0) for t in ok}, cfg)
            if cross_k is not None:
                cross = {t.full_name: cross_k.get(keys[t.full_name], 0.0) for t in ok}
        if cross is None:
            fps = {t.full_name: set(read_json(self.work / "fp" / f"{t.key}.json", [])) for t in ok}
            cross = cross_repo_builtin(fps) if any(fps.values()) else {t.full_name: 0.0 for t in ok}
        whole = cross_repo_files(hashes)   # identical files count fully, whatever their size
        for t in ok:
            n = t.full_name
            facts[n]["cross_repo_duplication_ratio"] = (max(cross.get(n, 0.0), whole.get(n, 0.0))
                                                        if len(ok) > 1 else 0.0)
        # commit level duplicates (also used by GildScore)
        mrows = [{"_full_name": n, "_commit_shas": f.get("_commit_shas"), "first_commit": f.get("first_commit"),
                  "_commit_loc": f.get("_commit_loc"), "_pr_commits": f.get("_pr_commits")} for n, f in facts.items()]
        mark_duplicates(mrows)
        for m in mrows:
            f = facts[m["_full_name"]]
            for k in ("duplicate_pct", "duplicate_of", "duplicate_commits"):
                f[k] = m.get(k, "")

        # catalogue rows
        columns, sources, dropdowns, _ = cat.template_spec()
        rows = []
        for t in targets:
            f = facts[t.full_name]
            r = {c: "" for c in columns}
            if f.get("error"):
                parts = t.full_name.split("/")
                r.update({"Org / Group (on host)": parts[0], "Dataset / Repo Name": parts[-1],
                          "Repo Full Name": t.full_name, "Hosting Platform": cat.PLATFORM.get(t.platform, ""),
                          "Notes": f"Extraction failed: {f['error'][:200]}"})
            else:
                r.update(cat.to_row(f, self.run_id))
                r["_matched_source_files"] = f.get("_matched_source_files")
            rows.append(r)
        qc: list[dict] = []
        missing: list[dict] = []
        if a.supplier_answers:
            answers = cat.read_answers(Path(a.supplier_answers), columns)
            log.info("Merging %d supplier answer rows", len(answers))
            cat.merge_answers(rows, answers, sources, dropdowns, qc, missing)
        bundle_dup = self._bundle_dup(rows, keys, cfg)
        rows = cat.bundle_rows(rows, facts, bundle_dup)
        for r in rows:
            for k, v in cat.formula_values(r).items():
                r[k] = v
        qc = qc + cat.qc_flags(rows, facts, cfg, groups)
        missing = missing + cat.missing_fields(rows, columns, sources)

        # ---- write
        cat.write_catalogue(rows, job_dir / "catalogue.xlsx", columns, sources)
        cat.write_summary(rows, job_dir / "summary.csv", columns)
        clients = [c.strip().lower() for c in (a.client or "").split(",") if c.strip()]
        if clients and not (cat.META_TEMPLATE.exists() and cat.HUD_TEMPLATE.exists()):
            log.warning("Buyer workbook templates are not in this edition; --client skipped")
            clients = []
        if clients:
            (job_dir / "client").mkdir(exist_ok=True)
            if "meta" in clients:
                cat.write_meta(rows, job_dir / "client" / "meta_vendor_quote.xlsx")
            if "hud" in clients:
                cat.write_hud(rows, job_dir / "client" / "hud_codebase_eval.xlsx")
        cat.write_csv(qc, job_dir / "qc" / "qc_report.csv", ["repo", "flag", "value", "rule"])
        cat.write_csv(missing, job_dir / "qc" / "missing_fields.csv", ["repo", "column", "who_fills"])
        cat.write_csv(groups, job_dir / "qc" / "duplicate_groups.csv", ["group_id", "method", "repos", "overlap"])
        with (job_dir / "raw_metrics.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=RAW_COLUMNS + ["duplicate_pct", "duplicate_of",
                                                             "shared_root_group_id"], extrasaction="ignore")
            w.writeheader()
            for t in targets:
                w.writerow(facts[t.full_name])
        if a.report:
            from .scorecard import render_html, score
            srows = [raw_to_score_row(facts[t.full_name], job_dir.name) for t in ok]
            for r in srows:
                r["_score"] = score(r)
                r["gildscore"], r["grade"] = r["_score"]["gildscore"], r["_score"]["grade"]
            (job_dir / "gildscore.html").write_text(render_html(srows, a.cutoff[:4]), encoding="utf-8")
        write_for_check(job_dir, [t for t in targets])
        write_json(job_dir / "repos.json", [{"platform": t.platform, "org": t.org, "full_name": t.full_name,
                                             "url": t.url or ""} for t in targets])
        failures = [{"repo": k, **v} for k, v in self.job["repos"].items() if v["status"] == "failed"]
        write_json(job_dir / "logs" / "failures.json", failures)
        self._manifest(targets, failures, sources, clients)
        (job_dir / "logs" / "SUPPORT.txt").write_text(
            "If this run had problems, send the zip: it holds logs/extract.log and logs/failures.json.\n"
            "Never send your token.\n\nTo continue an interrupted run:\n"
            f"  python gilddata_profiler.py --resume \"{job_dir}\"\n", encoding="utf-8")

    def _bundle_dup(self, rows: list[dict], keys: dict, cfg: dict) -> dict:
        """PRD: jscpd runs once over the whole bundle."""
        if self.stage_root is None:
            return {}
        by_id: dict[str, list[str]] = {}
        for r in rows:
            if r.get("Dataset ID"):
                by_id.setdefault(r["Dataset ID"], []).append(r["Repo Full Name"])
        out = {}
        for did, members in by_id.items():
            if len(members) < 2:
                continue
            dirs = [self.stage_root / keys[m] for m in members if m in keys and (self.stage_root / keys[m]).exists()]
            rep = run_jscpd(dirs, cfg) if dirs else None
            if rep:
                out[did] = round(((rep.get("statistics") or {}).get("total") or {}).get("percentage", 0) / 100, 4)
        return out

    def _manifest(self, targets, failures, sources, clients) -> None:
        a = self.a
        tools = {"python": __import__("sys").version.split()[0],
                 "git": run(["git", "--version"], check=False).replace("git version", "").strip()}
        for tool, args in (("scc", ["scc", "--version"]), ("jscpd", ["jscpd", "--version"]),
                           ("gitleaks", ["gitleaks", "version"])):
            tools[tool] = run(args, check=False).strip().splitlines()[0] if shutil.which(tool) else "not installed (built in fallback used)"
        tools["tree-sitter"] = "tree-sitter-language-pack" if tree_sitter_available() else "not installed (built in parser used)"
        classes: dict[str, int] = {}
        for f in failures:
            classes[f["error_class"]] = classes.get(f["error_class"], 0) + 1
        src = ("--local " + ", ".join(a.local_dir) if a.local_dir else
               "--repos " + a.repos_file if a.repos_file else
               "--org " + ", ".join(a.github_org + a.gitlab_group + a.bitbucket_workspace
                                    + a.github_repo + a.gitlab_project + a.bitbucket_repo))
        scc_on = shutil.which("scc") is not None
        write_json(self.job_dir / "manifest.json", {
            "created_at": self.run_id, "script_version": __version__,
            "tool": "gilddata-profiler (Codebase Catalogue Extractor v2)",
            "mode": "api" if a.mode != "offline" and (self.gh or self.gl or self.bb) else "offline",
            "llm": ({"enabled": True, "provider": a.llm,
                     "sent": "file paths, README start, manifests, start of up to 6 source files (under 200 lines each)"}
                    if a.llm else {"enabled": False}),
            "repos": len(targets), "ok": len(targets) - len(failures), "failed": len(failures),
            "error_classes": classes, "tools": tools,
            "inputs": {"source": src, "supplier_answers": Path(a.supplier_answers).name if a.supplier_answers else "",
                       "config": Path(a.config).name if a.config else "defaults", "cutoff": a.cutoff},
            "outputs": {"catalogue": "catalogue.xlsx", "summary_csv": "summary.csv",
                        **({"meta": "client/meta_vendor_quote.xlsx"} if "meta" in clients else {}),
                        **({"hud": "client/hud_codebase_eval.xlsx"} if "hud" in clients else {}),
                        "qc": "qc/", "evidence": "evidence/<org>__<repo>/",
                        "raw_metrics": "raw_metrics.csv"},
            "field_sources": {
                "Physical LOC (incl. blanks & comments)": "scc Lines total" if scc_on else "built in line counter, Lines total",
                "Logical LOC (excl. blanks & comments)": "scc Code total (scc --format json .)" if scc_on else "built in line counter, Code total",
                "Non-Authored LOC (auto-gen + vendored + deps)": ("scc . minus " + SCC_GENERATED_CMD + ", plus committed virtualenvs (lock files are outside scc's scope, so outside Logical LOC too)"
                                                                  if scc_on else "code LOC of vendored and generated files (file classes)"),
                "Duplication Ratio (0.00-1.00)": "jscpd percentage / 100" if jscpd_available() else "built in clone detector (5 line windows, 50 tokens)",
                "Non-Merge Commit Count": "git log --oneline --no-merges | grep -v -i revert | wc -l",
                "# of Commits (total)": "git rev-list --count <default branch>",
                "Shared-Root Group ID": "git rev-list --max-parents=0 HEAD (first 7 characters)",
                "# Functions": "tree-sitter AST nodes" if tree_sitter_available() else "Python ast; patterns for other languages",
                "Secrets Scan Result": "gitleaks" if shutil.which("gitleaks") else "built in secret rules",
                "PII Scan Result": "regex: email, phone (incl. +91), Aadhaar, PAN",
                "Avg LoC per PR": "mean added+deleted lines per merged PR, lock/generated/vendored files left out",
                "Quoted Price (USD)": "GildData internal (never filled by the script)",
            },
            "column_counts": cat.column_counts(sources),
            "privacy": {"source_code_included": False, "secret_values_included": False,
                        "free_text_included": bool(a.include_text),
                        "author_identities": "plain" if a.include_text else "hashed"},
            "support_logs": {"extract_log": "logs/extract.log", "failures_json": "logs/failures.json"},
        })

    def _setup_logging(self, job_dir: Path) -> None:
        (job_dir / "logs").mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(job_dir / "logs" / "extract.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger("profiler").addHandler(fh)

    def _diagnostics(self, n: int) -> None:
        import platform
        import sys
        log.info("=== Runtime diagnostics ===")
        log.info("gilddata-profiler=%s python=%s platform=%s", __version__, sys.version.split()[0],
                 platform.platform())
        log.info("git=%s", run(["git", "--version"], check=False).strip())
        for tool in ("scc", "jscpd", "gitleaks"):
            log.info("%s=%s", tool, shutil.which(tool) or "not installed (built in fallback)")
        log.info("tree-sitter=%s", "yes" if tree_sitter_available() else "no (built in parser)")
        log.info("repos=%d workers=%d mode=%s llm=%s", n, self.a.workers, self.a.mode, self.a.llm or "off")
        log.info("=== End diagnostics ===")


_REPO_TYPES = {"Production backend", "Production frontend", "Mobile app", "Infra / DevOps",
               "Data / ETL / ML", "Library / SDK", "Scripts / tooling", "Training exercise",
               "Scratch / POC", "Config only", "Empty"}


def _lang_summary(rows: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for r in rows:
        if not r["language"]:
            continue
        a = agg.setdefault(r["language"], {"Name": r["language"], "Files": 0, "Lines": 0, "Code": 0,
                                            "Comment": 0, "Blank": 0})
        a["Files"] += 1
        a["Lines"] += r["lines"]
        a["Code"] += r["code_loc"]
        a["Comment"] += r["comment"]
        a["Blank"] += r["blank"]
    return sorted(agg.values(), key=lambda x: -x["Code"])


def raw_to_score_row(f: dict, source: str) -> dict:
    """Map raw facts to the keys GildScore reads."""
    return {
        "repo": f.get("repo"), "_full_name": f.get("full_name"), "_source": source,
        "platform": f.get("platform"), "loc": f.get("logical_loc"), "total_commits": f.get("total_commits"),
        "merged_prs": f.get("prs_merged"), "human_authors": f.get("contributors"),
        "bus_factor": f.get("bus_factor"), "span_days": f.get("span_days"),
        "active_months": f.get("active_months"), "recency_days": f.get("recency_days"),
        "has_tests": f.get("has_tests"), "has_ci_cd": f.get("has_ci_cd"),
        "has_test_runner": f.get("has_test_runner"),
        "test_source_loc_pct": (f.get("test_to_code_ratio") * 100) if isinstance(f.get("test_to_code_ratio"), (int, float)) else "",
        "untested_files_pct": (f.get("untested_file_share") * 100) if isinstance(f.get("untested_file_share"), (int, float)) else "",
        "pr_review_pct": (100 * f["prs_reviewed"] / f["prs_merged"]) if isinstance(f.get("prs_reviewed"), int) and f.get("prs_merged") else "",
        "loc_per_pr": f.get("avg_loc_per_pr"),
        "docstring_coverage_pct": "",
        "comment_docstring_ratio": f.get("comment_docstring_ratio"),
        "median_pr_loc": f.get("median_pr_loc"),
        "duplicate_pct": f.get("duplicate_pct"), "duplicate_of": f.get("duplicate_of"),
        "duplication_ratio": f.get("duplication_ratio"),
        "open_source_loc_pct": f.get("open_source_loc_pct"),
        "ai_pr_pct": f.get("ai_pr_pct"),
        "vibe_code_signals": f.get("llm_ai_score") if f.get("llm_ai_score") not in ("", None) else "-",
        "largest_commit_loc_pct": f.get("largest_commit_loc_pct"),
        "primary_language": f.get("primary_language"), "languages_breakdown": f.get("language_breakdown"),
        "total_files": f.get("total_files"), "code_loc": f.get("code_loc") or f.get("source_loc"),
        "loc_pre_cutoff": f.get("loc_pre_cutoff"), "loc_post_cutoff": f.get("loc_post_cutoff"),
        "pct_loc_pre_cutoff": f.get("pct_loc_pre_cutoff"), "loc_by_year": f.get("loc_by_year"),
        "contributor_count": f.get("contributors"), "active_authors_90d": f.get("active_authors_90d"),
        "top_author_commit_pct": f.get("top_author_commit_pct"), "bot_commit_ratio": f.get("bot_commit_ratio"),
        "first_commit": f.get("first_commit"), "last_commit_date": f.get("last_commit"),
        "commits_per_active_month": f.get("commits_per_active_month"),
        "test_files": f.get("test_files"), "function_count": f.get("functions"),
        "class_count": f.get("classes_types"), "library_modules_loc_pct": f.get("open_source_loc_pct"),
        "pr_simple_pct": _pct(f.get("pr_simple")), "pr_standard_pct": _pct(f.get("pr_standard")),
        "pr_rich_pct": _pct(f.get("pr_rich")), "pr_other_pct": "",
        "commits_per_pr": "", "pr_linked_issue_pct": "", "median_hours_to_merge": "",
        "codebase_description": f.get("codebase_description"), "industry_domain": f.get("industry_domain_llm"),
        "repo_type": f.get("repo_type"), "cutoff_date": f.get("cutoff_date"),
        "secrets_result": f.get("secrets_result"), "pii_result": f.get("pii_result"),
        "parser_coverage": f.get("parser_coverage"), "duplication_ratio_jscpd": f.get("duplication_ratio"),
        "cross_repo_duplication_ratio": f.get("cross_repo_duplication_ratio"),
        "non_merge_commits": f.get("non_merge_commits"), "data_markup_loc": f.get("data_markup_loc"),
    }


def _pct(v):
    return round(v * 100, 1) if isinstance(v, (int, float)) else ""

# ---------------------------------------------------------------- helpers
def _size_kb(meta: dict, platform: str):
    if not meta:
        return ""
    if platform == "gitlab":
        s = (meta.get("statistics") or {}).get("repository_size")
        return round(s / 1024) if s else ""
    return meta.get("size", "")


_META_KEYS = ("id", "name", "full_name", "path_with_namespace", "private", "visibility", "fork",
              "archived", "created_at", "updated_at", "pushed_at", "last_activity_at",
              "default_branch", "size", "language", "stargazers_count", "forks_count",
              "star_count", "open_issues_count", "has_wiki", "has_issues", "html_url", "web_url",
              "topics", "license", "statistics", "forked_from_project")


def _slim_meta(meta: dict, platform: str) -> dict:
    out = {k: meta.get(k) for k in _META_KEYS if k in meta}
    if isinstance(out.get("license"), dict):
        out["license"] = out["license"].get("spdx_id") or out["license"].get("key")
    if isinstance(out.get("forked_from_project"), dict):
        out["forked_from_project"] = out["forked_from_project"].get("path_with_namespace")
    return out


def _slim_contrib(c: dict, include_text: bool) -> dict:
    login = c.get("login") or c.get("name") or c.get("email") or ""
    return {"login": login if include_text else _hash(login),
            "contributions": c.get("contributions") or c.get("commits"),
            "type": c.get("type", "")}


def _slim_pr(p: dict, include_text: bool) -> dict:
    out = dict(p)
    login = (p.get("author") or {}).get("login", "")
    out["author"] = {"login": login if include_text else _hash(login)}
    if not include_text:
        for k in ("title", "bodyText"):
            out.pop(k, None)
        out["labels"] = {"count": len((p.get("labels") or {}).get("nodes", []))}
    return out


def _slim_commit(c: dict, include_text: bool) -> dict:
    out = {k: c[k] for k in ("sha", "parents", "author_date", "is_bot", "ai_signature", "files")}
    if include_text:
        out.update(author_name=c["author_name"], author_email=c["author_email"],
                   subject=c["subject"])
    else:
        out["author"] = _hash(c["author_email"] or c["author_name"])
    return out


def _anon_authors(stats: dict, include_text: bool) -> dict:
    if include_text:
        return stats
    s = dict(stats)
    s["authors"] = [{"author": _hash(a["email"] or a["name"]), "commits": a["commits"],
                     "is_bot": a["is_bot"]} for a in stats["authors"]]
    return s


def mark_duplicates(rows: list[dict], job_dir: Path | None = None, threshold: float = 25.0) -> None:
    """Commits shared between repos. A repo is a duplicate when more than
    `threshold` % of its commits also sit in another repo; it is called a
    duplicate of the repo with the earliest first commit among those sharing."""
    owners: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        for sha in r.get("_commit_shas") or []:
            owners.setdefault(sha, []).append(i)
    for i, r in enumerate(rows):
        shas = r.get("_commit_shas")
        if not shas:
            continue
        shared_with: dict[int, int] = {}
        shared = set()
        for sha in shas:
            for j in owners[sha]:
                if j != i:
                    shared_with[j] = shared_with.get(j, 0) + 1
                    shared.add(sha)
        r["duplicate_pct"] = pct(len(shared), len(shas))
        r["duplicate_commits"] = len(shared)
        r["duplicates_with"] = "; ".join(
            f"{rows[j]['_full_name']}:{n}" for j, n in sorted(shared_with.items(), key=lambda kv: -kv[1]))
        if r["duplicate_pct"] > threshold and shared_with:
            def first(j):
                return rows[j].get("first_commit") or "9999"
            cand = sorted(shared_with, key=lambda j: (first(j), -shared_with[j]))
            other = cand[0]
            if (first(other), rows[other]["_full_name"]) < (r.get("first_commit") or "9999", r["_full_name"]):
                r["duplicate_of"] = rows[other]["_full_name"]
                loc_map = r.get("_commit_loc") or {}
                r["duplicate_loc"] = sum(loc_map.get(s, 0) for s in shared)
                pr_commits = r.get("_pr_commits") or {}
                r["duplicate_prs"] = sum(1 for sha in pr_commits.values() if sha in shared)


def write_for_check(job_dir: Path, targets: list[Target]) -> None:
    with (job_dir / "for-check.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["repo", "section", "key", "value"])
        for t in targets:
            fc = read_json(job_dir / "evidence" / t.key / "for_check.json")
            if not fc:
                continue
            r = t.full_name
            for k in ("platform", "host", "started_at", "finished_at"):
                w.writerow([r, "meta", k, fc.get(k, "")])
            before, after = fc.get("before") or {}, fc.get("after") or {}
            for k, v in (before.get("counts") or {}).items():
                w.writerow([r, "counts_before", k, v])
            for k, v in sorted((before.get("refs") or {}).items()):
                w.writerow([r, "ref_before", k, v])
            for k, v in (after.get("counts") or {}).items():
                w.writerow([r, "counts_after", k, v])
            ra, rb = after.get("refs") or {}, before.get("refs") or {}
            for k in sorted(set(ra) | set(rb)):
                if ra.get(k) != rb.get(k):
                    w.writerow([r, "ref_changed", k, f"{rb.get(k, '')} -> {ra.get(k, '')}"])
            for k, v in (fc.get("captured") or {}).items():
                w.writerow([r, "captured", k, v])
            for k, v in (fc.get("files") or {}).items():
                w.writerow([r, "file", k, v])


def zip_job(job_dir: Path) -> Path:
    zpath = job_dir.with_suffix(".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(job_dir.rglob("*")):
            rel = p.relative_to(job_dir).parts
            if p.is_file() and ".work" not in rel and p.name not in ("targets.json", "job.json"):
                z.write(p, Path(job_dir.name) / p.relative_to(job_dir))
    return zpath
