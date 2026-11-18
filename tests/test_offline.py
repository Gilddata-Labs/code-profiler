"""Offline end to end tests on small fixture repos with known counts. No network.

Fakes the GitHub, GitLab, Bitbucket and LLM APIs, runs every command, and checks
the outputs against the PRD's done criteria where they can be checked offline:
exact buyer commands, duplicate groups, secrets flag, no code or secret values in
the zip, supplier merge rules, bundles, and the buyer workbooks.

Run:  python tests/test_offline.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from profiler import cli, http, platforms  # noqa: E402
from profiler.platforms import pr_tier  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="gdp-test-"))
CALLS: list[tuple[str, str, str]] = []
SECRET_VALUE = "AKIAQWERTYUIOPASDFGH"
CODE_MARKER = "def unique_marker_function_xyz"
EMAIL_VALUE = "ravi.kumar@acmepay.in"


def git(cwd, *args):
    e = dict(os.environ, GIT_AUTHOR_NAME="Dev One", GIT_AUTHOR_EMAIL="dev@acmepay.in",
             GIT_COMMITTER_NAME="Dev One", GIT_COMMITTER_EMAIL="dev@acmepay.in")
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=e).stdout.strip()


def make_repo(path: Path) -> list[str]:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for d in ("src", "tests", "vendor/lib", ".github/workflows", "config"):
        (path / d).mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"    total += {i} * value  # step {i}" for i in range(40))
    (path / "src/app.py").write_text(f'"""App."""\n\n\ndef run(value):\n    """Run."""\n    total = 0\n{body}\n    return total\n\n\nclass Svc:\n    pass\n')
    (path / "src/util.py").write_text(f"# helper\n{CODE_MARKER}(x):\n    return x * 2\n")
    (path / "src/settings.py").write_text(f'AWS_KEY = "{SECRET_VALUE}"\n')
    (path / "tests/test_app.py").write_text("from src.app import run\n\n\ndef test_run():\n    assert run(1) >= 0\n")
    (path / "vendor/lib/lib.js").write_text("// SPDX-License-Identifier: MIT\nfunction v(){return 1}\n")
    (path / "config/users.json").write_text(json.dumps([{"email": EMAIL_VALUE}]))
    (path / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "x": list(range(30))}, indent=1))
    (path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (path / ".github/workflows/ci.yml").write_text("on: push\n")
    (path / "README.md").write_text("# Demo\n\nA demo billing service that handles merchant payouts.\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "init")
    merges = []
    for i, (fname, msg) in enumerate([("src/a.py", "feature a"),
                                      ("src/b.py", "feature b\n\nCo-Authored-By: Copilot <copilot@github.com>"),
                                      ("src/c.py", "Revert feature c")]):
        git(path, "checkout", "-qb", f"f{i}")
        (path / fname).write_text(f"def f{i}():\n    return {i}\n")
        git(path, "add", "-A")
        git(path, "commit", "-qm", msg)
        git(path, "checkout", "-q", "main")
        git(path, "merge", "-q", "--no-ff", f"f{i}", "-m", f"Merge pull request #{i + 1} from dev/f{i}")
        merges.append(git(path, "rev-parse", "HEAD"))
    git(path, "tag", "v1.0")
    return merges


class R:
    def __init__(self, code, body, headers=None):
        self.status_code = code
        self._b = body
        self.content = json.dumps(body).encode()
        self.text = self.content.decode()
        self.headers = headers or {}
        self.links = {}

    def json(self):
        return self._b


def fake_api(merges):
    def gh_pr(i, sha):
        return {"number": i + 1, "title": "t", "bodyText": "b", "url": "u",
                "createdAt": "2025-01-01T00:00:00Z", "mergedAt": "2025-01-02T00:00:00Z",
                "changedFiles": 1, "additions": 2, "deletions": 0, "totalCommentsCount": 0,
                "mergeCommit": {"oid": sha}, "author": {"login": "dev", "__typename": "User"},
                "labels": {"nodes": []}, "commits": {"totalCount": 1},
                "files": {"pageInfo": {"hasNextPage": False}, "nodes": [{"path": "src/x.py", "additions": 2, "deletions": 0}]},
                "closingIssuesReferences": {"nodes": []}, "comments": {"totalCount": 0},
                "reviews": {"totalCount": 1}, "approvals": {"totalCount": 1 if i < 2 else 0},
                "reviewThreads": {"totalCount": 0}}

    def handler(self, method, url, params=None, json=None, timeout=60):
        q = (json or {}).get("query", "") if json else ""
        CALLS.append((method, url, q))
        if "api.anthropic.com" in url:
            return R(200, {"content": [{"type": "text", "text": '{"codebase_description": "Billing API.", '
                     '"industry_domain": "fintech", "repo_type": "Production backend", "ai_code_score": 35, '
                     '"ai_code_reasons": ["uniform docstrings"]}'}]})
        if url.endswith("/graphql"):
            if "states:[OPEN,CLOSED,MERGED]" in q:
                return R(200, {"data": {"repository": {"pullRequests": {"totalCount": 5}}}})
            return R(200, {"data": {"repository": {"pullRequests": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [gh_pr(i, s) for i, s in enumerate(merges)]}}}})
        if "api.github.com" in url:
            u = url.split("?")[0]
            if u.endswith("/repos/acme/demo"):
                return R(200, {"full_name": "acme/demo", "default_branch": "main", "size": 12,
                               "created_at": "2025-01-01T00:00:00Z", "has_wiki": True, "has_issues": True})
            if "/orgs/acme/repos" in u:
                return R(200, [{"full_name": "acme/demo", "fork": False, "archived": False}])
            if u.endswith("/orgs/acme"):
                return R(200, {"login": "acme"})
            if "/branches" in u or "/tags" in u:
                return R(200, [{"name": "main", "commit": {"sha": merges[-1]}}])
            if "search/issues" in u:
                return R(200, {"total_count": 3})
            return R(200, [])
        if "gitlab.com/api/v4" in url:
            u = url.split("?")[0]
            if u.endswith("/projects/grp%2Fdemo"):
                return R(200, {"path_with_namespace": "grp/demo", "default_branch": "main",
                               "created_at": "2025-01-01T00:00:00Z", "statistics": {"repository_size": 20480}})
            if u.endswith("/groups/grp/projects"):
                return R(200, [{"path_with_namespace": "grp/demo"}])
            if u.endswith("/merge_requests"):
                return R(200, [{"iid": i + 1, "title": "t", "description": "", "web_url": "u",
                                "created_at": "2025-01-01", "merged_at": "2025-01-02", "merge_commit_sha": s,
                                "author": {"username": "dev"}, "labels": [], "user_notes_count": 0}
                               for i, s in enumerate(merges)], {"x-total": "3"})
            if u.endswith("/changes"):
                return R(200, {"changes": [{"new_path": "src/a.py", "diff": "+x\n+y\n-z"}]})
            if u.endswith("/approvals"):
                return R(200, {"approved_by": [{"user": {}}]})
            if u.endswith("/closes_issues"):
                return R(200, [])
            if "/repository/branches" in u or "/repository/tags" in u:
                return R(200, [{"name": "main", "commit": {"id": merges[-1]}}], {"x-total": "1"})
            return R(200, [], {"x-total": "0"})
        if "api.bitbucket.org" in url:
            u = url.split("?")[0]
            if u.endswith("/repositories/ws/demo"):
                return R(200, {"full_name": "ws/demo", "mainbranch": {"name": "main"},
                               "created_on": "2025-01-01T00:00:00Z", "size": 40960, "is_private": True,
                               "has_issues": False})
            if u.endswith("/repositories/ws"):
                return R(200, {"values": [{"full_name": "ws/demo", "slug": "demo"}]})
            if u.endswith("/pullrequests"):
                return R(200, {"size": 3, "values": [
                    {"id": i + 1, "title": "t", "description": "", "created_on": "2025-01-01",
                     "updated_on": "2025-01-02", "comment_count": 0, "task_count": 0,
                     "merge_commit": {"hash": s[:12]}, "author": {"nickname": "dev"},
                     "links": {"html": {"href": "u"}}} for i, s in enumerate(merges)]})
            if u.endswith("/diffstat"):
                return R(200, {"values": [{"lines_added": 2, "lines_removed": 0, "new": {"path": "src/a.py"}}]})
            if "/pullrequests/" in u:
                return R(200, {"participants": [{"approved": True}]})
            if "/refs/" in u:
                return R(200, {"size": 1, "values": [{"name": "main", "target": {"hash": merges[-1]}}]})
            return R(200, {"size": 0, "values": []})
        raise AssertionError(f"unexpected URL {url}")
    return handler


def run_cli(args: list[str]) -> Path:
    out = TMP / f"out{len(list(TMP.glob('out*')))}"
    cli.main([*args, "--out", str(out), "--workers", "1", "--skip-blame"])
    return next(out.glob("*.zip"))


def read_zip(zpath: Path, suffix: str) -> str:
    with zipfile.ZipFile(zpath) as z:
        return z.read(next(n for n in z.namelist() if n.endswith(suffix))).decode()


def rows_of(zpath: Path) -> dict[str, dict]:
    return {r["Dataset / Repo Name"]: r for r in csv.DictReader(io.StringIO(read_zip(zpath, "/summary.csv")))}


def main() -> int:
    src_root = TMP / "src"
    src = src_root / "demo"
    merges = make_repo(src)
    git(src_root, "clone", "-q", str(src), str(src_root / "demo-copy"))
    http.Session.request = fake_api(merges)
    for c in (platforms.GitHub, platforms.GitLab, platforms.Bitbucket):
        c.clone_url = lambda self, f, _s=str(src): _s
        c.clone_auth_header = lambda self: None
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(("PASS " if cond else "FAIL ") + name)

    # PRD PR bands
    # scc flags must exist in a real scc (the v2.0.0 build used --generated-code, which no scc has)
    from profiler.classify import SCC_GENERATED_ARGS, scc_totals
    if shutil.which("scc"):
        check("scc generated-code flags accepted by the installed scc",
              scc_totals(ROOT, SCC_GENERATED_ARGS) is not None)
    else:
        check("scc generated-code flags are the documented ones",
              SCC_GENERATED_ARGS[:2] == ["--no-gen", "--no-min-gen"] and "--generated-code" not in SCC_GENERATED_ARGS)
    # clone timeouts: size aware first try, a timed out clone goes straight to the extended timeout,
    # a network error is retried with the same timeout, and the repo is named in the message
    import subprocess as _sp
    from profiler.gitstats import clone, clone_timeout_for
    check("clone timeout grows with repo size", clone_timeout_for(500 * 1024, 600, 3600) == 1000
          and clone_timeout_for(10 * 1024, 600, 3600) == 600 and clone_timeout_for(5000 * 1024, 600, 3600) == 3600)
    seen = []
    def fake_run(cmd, timeout=None, **kw):
        seen.append(timeout)
        if len(seen) == 1:
            raise _sp.TimeoutExpired(cmd, timeout)
    clone("https://example.invalid/acme/big.git", TMP / "clone-test", None, 600, 2, 3600,
          name="acme/big", size_kb=100 * 1024, _run=fake_run)
    check("timed out clone retries once with the extended timeout", seen == [600, 3600])
    seen.clear()
    def fake_run2(cmd, timeout=None, **kw):
        seen.append(timeout)
        if len(seen) <= 2:
            raise RuntimeError("git failed (128): early EOF")
    clone("https://example.invalid/acme/flaky.git", TMP / "clone-test2", None, 600, 2, 3600, name="acme/flaky", _run=fake_run2)
    check("network errors retry with the same timeout", seen == [600, 600, 600])
    try:
        clone("https://example.invalid/acme/gone.git", TMP / "clone-test3", None, 600, 2, 3600, name="acme/gone",
              _run=lambda cmd, timeout=None, **kw: (_ for _ in ()).throw(_sp.TimeoutExpired(cmd, timeout)))
        named = False
    except RuntimeError as e:
        named = "acme/gone" in str(e)
    check("clone failure names the repo", named)
    check("PR bands", [pr_tier(50, 3), pr_tier(51, 3), pr_tier(10, 4), pr_tier(501, 1), pr_tier(10, 16)]
          == ["simple", "standard", "standard", "rich", "rich"])

    # ---- local run with supplier answers (bundle + conflict + company fields)
    answers = TMP / "answers.csv"
    with answers.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Repo Full Name", "Dataset ID", "Originating Company (legal name)",
                    "Originating Company's Industry", "# of Contributors", "Git Delivery Format",
                    "Builds & Tests", "Hosting Platform"])
        w.writerow(["acme/demo", "GD-ACME-001", "Acme Payments Pvt Ltd", "Financial Services", "40",
                    "Full .git (all branches + tags)", "Builds only", "Not a platform"])
        w.writerow(["acme/demo-copy", "GD-ACME-002", "", "", "", "", "", ""])
    z = run_cli(["--local", str(src_root), "--label", "acme", "--supplier-answers", str(answers),
                 "--client", "meta,hud", "--report"])
    rows = rows_of(z)
    r = rows["demo"]
    nm = len([l for l in git(src, "log", "--oneline", "--no-merges", "main").splitlines() if "revert" not in l.lower()])
    check("Non-Merge Commit Count equals the buyer's command", r["Non-Merge Commit Count"] == str(nm))
    check("# of Commits equals git rev-list --count", r["# of Commits (total)"] == git(src, "rev-list", "--count", "main"))
    check("Snapshot SHA", r["Snapshot Commit SHA"] == git(src, "rev-parse", "HEAD")[:7])
    check("tags counted", r["# Tags"] == "1")
    check("merged PRs inferred offline", r["# of Merged PRs"] == "3" and r["PR Data Source"] == "Commit-message inferred")
    check("non-authored includes vendored and lock files", int(r["Non-Authored LOC (auto-gen + vendored + deps)"]) > 0)
    check("data/markup LOC counted", int(r["Data / Markup LOC"]) > 0)
    check("primary language excludes data", r["Primary Coding Language"] == "Python")
    check("README description", "merchant payouts" in r["Codebase Description"])
    check("test to code ratio is a ratio", 0 < float(r["Test-to-Code LOC Ratio"]) < 1)
    check("secrets flagged", r["Secrets Scan Result"] == "Findings - open")
    check("PII flagged", r["PII Scan Result"] == "Findings - open")
    check("build hints", r["Test Command"] == "pytest")
    check("AI signals evidence", "AI tool signature" in r["AI-Generated Code Signals"])
    check("supplier company fields merged", r["Originating Company (legal name)"] == "Acme Payments Pvt Ltd")
    check("company fields copied to other repos of the org",
          rows["demo-copy"]["Originating Company (legal name)"] == "Acme Payments Pvt Ltd")
    qc = list(csv.DictReader(io.StringIO(read_zip(z, "qc/qc_report.csv"))))
    flags = {(q["repo"].split("/")[-1], q["flag"]) for q in qc}
    check("CONFLICT logged and script value kept", ("demo", "CONFLICT") in flags and r["# of Contributors"] != "40")
    check("invalid dropdown value rejected", ("demo", "INVALID VALUE") in flags)
    check("DUPLICATE GROUP for the clone", ("demo-copy", "DUPLICATE GROUP") in flags)
    check("SECRETS flag in QC", ("demo", "SECRETS") in flags)
    groups = read_zip(z, "qc/duplicate_groups.csv")
    check("shared root group", "shared root commit" in groups)
    check("cross repo duplication of the clone is 1.0", float(rows["demo-copy"]["Cross-Repo Duplication Ratio"]) == 1.0)
    check("formula values in summary", rows["demo"]["Net Authored LOC"] != "" and rows["demo"]["Price Tier Multiplier"] == "0.7")
    with zipfile.ZipFile(z) as zz:
        names = zz.namelist()
        blob = b"".join(zz.read(n) for n in names if not n.endswith(".xlsx"))
        xl = [n for n in names if n.endswith(".xlsx")]
    client_templates = (ROOT / "profiler" / "templates" / "client" / "meta_template.xlsx").exists()
    want = {"catalogue.xlsx"} | ({"meta_vendor_quote.xlsx", "hud_codebase_eval.xlsx"} if client_templates else set())
    check("zip holds catalogue" + (", buyer workbooks" if client_templates else " (buyer templates not in this build)"),
          want <= {n.rsplit("/", 1)[-1] for n in xl})
    check("no source code in the zip", CODE_MARKER.encode() not in blob)
    check("no secret values in the zip", SECRET_VALUE.encode() not in blob)
    check("no PII values in the zip", EMAIL_VALUE.encode() not in blob)
    check("evidence files per PRD", all(any(n.endswith(f"/{f}") for n in names) for f in (
        "scc.json", "jscpd-report.json", "git_stats.json", "pr_stats.json", "structure.json",
        "file_classes.csv", "secrets_pii.json", "build_hints.json")))
    check("GildScore report", any(n.endswith("gildscore.html") for n in names))
    import openpyxl
    wbz = zipfile.ZipFile(z)
    cat_bytes = wbz.read(next(n for n in names if n.endswith("/catalogue.xlsx")))
    ws = openpyxl.load_workbook(io.BytesIO(cat_bytes)).worksheets[0]
    check("catalogue rows start at row 9 with formulas kept",
          ws["F9"].value in ("demo", "demo-copy") and str(ws["AE9"].value).startswith("=IF"))

    # ---- bundle: two repos share a Dataset ID
    with answers.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Repo Full Name", "Dataset ID", "Dataset / Repo Name"])
        w.writerow(["acme/demo", "GD-B-1", "demo bundle"])
        w.writerow(["acme/demo-copy", "GD-B-1", "demo bundle"])
    z = run_cli(["--local", str(src_root), "--label", "acme", "--supplier-answers", str(answers)])
    rows = list(csv.DictReader(io.StringIO(read_zip(z, "/summary.csv"))))
    check("bundle rolled into one row", len(rows) == 1 and rows[0]["# Repos in Dataset"] == "2")

    # ---- GitHub API mode with LLM
    z = run_cli(["--org", "acme", "--token", "x", "--llm", "anthropic", "--llm-key", "k", "--yes-send-excerpts"])
    r = rows_of(z)["demo"]
    check("github: PR data from API", r["PR Data Source"] == "Platform API" and r["# of Merged PRs"] == "3")
    check("github: total and reviewed PRs", r["# of PRs (total)"] == "5" and r["# of Reviewed PRs"] == "2")
    check("github: LLM description and type", r["Codebase Description"] == "Billing API." and r["Repo Type"] == "Production backend")
    check("github: LLM score in AI signals", "LLM review score 35/100" in r["AI-Generated Code Signals"])

    z = run_cli(["--org", "grp", "--platform", "gitlab", "--token", "x"])
    r = rows_of(z)["demo"]
    check("gitlab: merged MRs and approvals", r["# of Merged PRs"] == "3" and r["# of Reviewed PRs"] == "3")
    z = run_cli(["--org", "ws", "--platform", "bitbucket", "--token", "x"])
    check("bitbucket: merged PRs", rows_of(z)["demo"]["# of Merged PRs"] == "3")
    check("read only: GET and POST only", {m for m, _, _ in CALLS} <= {"GET", "POST"})
    check("read only: POST only GraphQL queries or the LLM",
          all(("graphql" in u and "mutation" not in q) or "anthropic" in u for m, u, q in CALLS if m == "POST"))

    # ---- compile and compare (GildData side)
    zips = sorted(TMP.glob("out*/*.zip"))
    cli.main(["compile", *map(str, zips[:2]), "--out", str(TMP / "compiled")])
    outs = {p.name.split("-", 2)[-1] for p in (TMP / "compiled").iterdir()}
    want = {"catalogue.xlsx", "gildscore.html"} | ({"meta_vendor_quote.xlsx", "hud_codebase_eval.xlsx"} if client_templates else set())
    check("compile writes catalogue, GildScore" + (", buyer workbooks" if client_templates else ""), want <= outs)
    cmp = TMP / "cmp.csv"
    cli.main(["compare", str(zips[0]), str(zips[0]), "--out", str(cmp)])
    check("compare runs", cmp.exists())

    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)} of {len(checks)} checks passed")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
