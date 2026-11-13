"""Command line entry. Run with no arguments for a guided setup."""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import shutil
import sys
from pathlib import Path

from . import __version__

BANNER = f"""
GildData Codebase Profiler v{__version__}
Reads your repositories and writes a metadata only zip. No source code,
commit messages or PR text leave your machine (unless you pass --include-text).
Nothing is ever written to GitHub or GitLab.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gilddata_profiler",
                                description="Privacy safe repository profiler for GildData.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    e = sub.add_parser("extract", help="profile repositories (default command)")
    k = sub.add_parser("check", help="test tools, tokens and access without profiling")
    for parser in (e, k):
        add_common(parser)

    c = sub.add_parser("compile", help="(GildData side) merge zips into Excel reports")
    c.add_argument("inputs", nargs="+", help="zips or job folders from customers")
    c.add_argument("--out", default="compiled", help="output folder")
    c.add_argument("--client", default="meta,hud", help="GildData internal: buyer workbook templates are not part of this build")

    d = sub.add_parser("compare", help="(GildData side) compare the reference profiler's zip with ours")
    d.add_argument("reference", help="zip or folder from the original profiler")
    d.add_argument("ours", help="zip or folder from this profiler, same repos")
    d.add_argument("--out", default="", help="CSV to write (default compare-<time>.csv)")
    return p


def add_common(e: argparse.ArgumentParser) -> None:
    g = e.add_argument_group("what to scan (PRD flags)")
    g.add_argument("--org", action="append", default=[], metavar="ORG",
                   help="GitHub org, GitLab group or Bitbucket workspace (see --platform); repeatable")
    g.add_argument("--platform", choices=["github", "gitlab", "bitbucket"], default="github",
                   help="platform for --org and --token (default github)")
    g.add_argument("--token", default="", help="token for --platform (or GITHUB_TOKEN / GITLAB_TOKEN / BITBUCKET_TOKEN)")
    g.add_argument("--repos", dest="repos_file", default="", metavar="repos.json",
                   help="list of {platform, org, full_name, url}, same format as repos.json")
    g.add_argument("--local", dest="local_dir", action="append", default=[], metavar="DIR",
                   help="folder of repos already cloned; each subfolder with .git is one repo")
    g.add_argument("--supplier-answers", default="", metavar="answers.xlsx",
                   help="our catalogue template with the supplier columns filled (or a CSV)")
    g.add_argument("--config", default="", metavar="config.json|yaml",
                   help="lists and thresholds: rare languages, data languages, PR bands, "
                        "excluded dirs, bot patterns, author alias map")
    g.add_argument("--mode", choices=["auto", "offline", "api"], default="auto",
                   help="api uses the platform API for PR and review data; offline uses git only; "
                        "auto = api when a token is given (default)")
    g.add_argument("--include-history-scan", action="store_true",
                   help="scan secrets across all commits, not just the working tree (slower)")
    g.add_argument("--client", default="", metavar="NAMES",
                   help="GildData internal: buyer workbook templates are not part of this build")
    g.add_argument("--report", action="store_true", help="also write the GildScore report cards")
    g.add_argument("--keep-work", action="store_true", help="keep the temporary work folder")
    g.add_argument("--github-org", action="append", default=[], metavar="ORG",
                   help="GitHub organisation or user; repeatable")
    g.add_argument("--github-repo", action="append", default=[], metavar="OWNER/NAME")
    g.add_argument("--gitlab-group", action="append", default=[], metavar="GROUP",
                   help="GitLab group (subgroups included) or user; repeatable")
    g.add_argument("--gitlab-project", action="append", default=[], metavar="GROUP/NAME")
    g.add_argument("--bitbucket-workspace", action="append", default=[], metavar="WORKSPACE")
    g.add_argument("--bitbucket-repo", action="append", default=[], metavar="WORKSPACE/SLUG")
    g.add_argument("--local-dir", dest="local_dir", action="append", default=[], metavar="PATH",
                   help=argparse.SUPPRESS)
    g.add_argument("--include-forks", action="store_true")
    g.add_argument("--include-archived", action="store_true")
    g.add_argument("--limit", type=int, default=0, help="profile at most N repos")
    t = e.add_argument_group("access")
    t.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN", ""),
                   help="or set GITHUB_TOKEN; classic PAT with repo + read:org")
    t.add_argument("--gitlab-token", default=os.environ.get("GITLAB_TOKEN", ""),
                   help="or set GITLAB_TOKEN; read_api + read_repository")
    t.add_argument("--bitbucket-token", default=os.environ.get("BITBUCKET_TOKEN", ""),
                   help="or set BITBUCKET_TOKEN; Atlassian API token (with --bitbucket-email) "
                        "or a workspace/repository access token")
    t.add_argument("--bitbucket-email", default=os.environ.get("BITBUCKET_EMAIL", ""),
                   help="Atlassian account email, only with an Atlassian API token")
    t.add_argument("--github-host", default="github.com", help="for GitHub Enterprise Server")
    t.add_argument("--gitlab-host", default="gitlab.com", help="for self managed GitLab")
    o = e.add_argument_group("run")
    o.add_argument("--out", default="profiler-output", help="output folder")
    o.add_argument("--label", default="", help="name used in the output file")
    o.add_argument("--resume", default="", metavar="JOB_DIR", help="continue a stopped run")
    o.add_argument("--workers", type=int, default=2)
    o.add_argument("--include-text", action="store_true",
                   help="also include commit subjects, PR titles/bodies and author names")
    o.add_argument("--skip-prs", action="store_true", help="skip pull/merge request data")
    o.add_argument("--max-prs", type=int, default=0, help="cap merged PRs read per repo")
    o.add_argument("--pr-time-budget", type=int, default=900, help="seconds per repo for PR signals")
    o.add_argument("--no-scc", action="store_true", help="use the built in line counter")
    o.add_argument("--clone-timeout", type=int, default=300)
    o.add_argument("--clone-retries", type=int, default=2)
    o.add_argument("--clone-extended-timeout", type=int, default=1200)
    o.add_argument("-v", "--verbose", action="store_true")
    v = e.add_argument_group("report card")
    v.add_argument("--cutoff", default="2024-01-01",
                   help="date that splits code vintage, before vs after (default 2024-01-01)")
    v.add_argument("--skip-blame", action="store_true", help="skip the code vintage (git blame) step")
    v.add_argument("--blame-time-budget", type=int, default=60,
                   help="seconds per repo for git blame; the rest is estimated from a random sample (default 60)")
    m = e.add_argument_group("optional LLM analysis (sends code excerpts to an AI provider)")
    m.add_argument("--llm", choices=["anthropic", "openai"], default="",
                   help="turn on LLM analysis with this provider; off by default")
    m.add_argument("--llm-key", default="", help="API key; or ANTHROPIC_API_KEY / OPENAI_API_KEY")
    m.add_argument("--llm-model", default="", help="override the default model")
    m.add_argument("--yes-send-excerpts", action="store_true",
                   help="agree up front to send code excerpts (skips the question)")


def wizard() -> list[str]:
    """Ask a few questions when run with no arguments."""
    print(BANNER)
    args: list[str] = ["extract"]
    print("Where is the code?\n  1) GitHub\n  2) GitLab\n  3) Folders on this computer\n  4) Bitbucket Cloud")
    choice = input("Pick 1, 2, 3 or 4: ").strip()
    if choice == "1":
        host = input("GitHub host [github.com]: ").strip() or "github.com"
        names = input("Organisation(s) or user(s), comma separated: ").strip()
        for n in filter(None, (x.strip() for x in names.split(","))):
            args += (["--github-repo", n] if "/" in n else ["--github-org", n])
        if not os.environ.get("GITHUB_TOKEN"):
            os.environ["GITHUB_TOKEN"] = getpass.getpass("GitHub token (hidden): ").strip()
        args += ["--github-host", host, "--github-token", os.environ["GITHUB_TOKEN"]]
    elif choice == "2":
        host = input("GitLab host [gitlab.com]: ").strip() or "gitlab.com"
        names = input("Group(s) or project path(s), comma separated: ").strip()
        for n in filter(None, (x.strip() for x in names.split(","))):
            args += ["--gitlab-group", n]
        if not os.environ.get("GITLAB_TOKEN"):
            os.environ["GITLAB_TOKEN"] = getpass.getpass("GitLab token (hidden): ").strip()
        args += ["--gitlab-host", host, "--gitlab-token", os.environ["GITLAB_TOKEN"]]
    elif choice == "4":
        names = input("Bitbucket workspace(s) or workspace/repo, comma separated: ").strip()
        for n in filter(None, (x.strip() for x in names.split(","))):
            args += (["--bitbucket-repo", n] if "/" in n else ["--bitbucket-workspace", n])
        if not os.environ.get("BITBUCKET_TOKEN"):
            email = input("Atlassian account email (leave blank for an access token): ").strip()
            if email:
                args += ["--bitbucket-email", email]
            os.environ["BITBUCKET_TOKEN"] = getpass.getpass("Bitbucket token (hidden): ").strip()
        args += ["--bitbucket-token", os.environ["BITBUCKET_TOKEN"]]
    elif choice == "3":
        folder = input("Folder that holds your git checkouts: ").strip().strip('"')
        args += ["--local-dir", folder]
    else:
        sys.exit("Nothing chosen.")
    print("\nOptional: AI analysis describes each codebase and scores how much looks AI written.\n"
          "It sends a short excerpt of each repo (README start, file list and the start of up to\n"
          "6 source files) to an AI provider using YOUR API key. Skip it to keep all code local.")
    if input("Turn on AI analysis? [y/N]: ").strip().lower() == "y":
        prov = input("Provider, anthropic or openai [anthropic]: ").strip().lower() or "anthropic"
        args += ["--llm", prov, "--yes-send-excerpts"]
        env = "ANTHROPIC_API_KEY" if prov == "anthropic" else "OPENAI_API_KEY"
        if not os.environ.get(env):
            args += ["--llm-key", getpass.getpass(f"{prov} API key (hidden): ").strip()]
    label = input("Your company name (used in the file name): ").strip()
    if label:
        args += ["--label", label]
    return args


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = wizard()
    elif argv[0] not in ("extract", "check", "compile", "compare", "-h", "--help", "--version"):
        argv = ["extract", *argv]
    a = build_parser().parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if getattr(a, "verbose", False) else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if a.cmd == "compare":
        from .compile import compare_runs
        out = compare_runs(Path(a.reference), Path(a.ours), Path(a.out) if a.out else None)
        print(f"\nFull comparison: {out}")
        return 0

    if a.cmd == "compile":
        from .compile import compile_runs
        out = compile_runs([Path(x) for x in a.inputs], Path(a.out),
                           [c.strip() for c in a.client.split(",") if c.strip()])
        print(f"\nGildScore report cards: {out}\nExcel files are in the same folder.")
        return 0

    if a.llm and not a.llm_key:
        a.llm_key = os.environ.get("ANTHROPIC_API_KEY" if a.llm == "anthropic" else "OPENAI_API_KEY", "")
    for x in list(a.bitbucket_workspace):
        if "/" in x:
            a.bitbucket_workspace.remove(x)
            a.bitbucket_repo.append(x)

    if a.cmd in ("extract", "check"):
        from .config import load as load_config
        a.cfg = load_config(a.config or None)
        if a.org:
            {"github": a.github_org, "gitlab": a.gitlab_group, "bitbucket": a.bitbucket_workspace}[a.platform].extend(a.org)
        if a.token:
            setattr(a, f"{a.platform}_token", a.token)
        if a.mode == "auto":
            a.mode = "api"
        from datetime import datetime, timezone
        try:
            a.cutoff_dt = datetime.fromisoformat(a.cutoff).replace(tzinfo=timezone.utc)
        except ValueError:
            sys.exit("--cutoff must look like 2024-01-01")

    if a.cmd == "check":
        for x in list(a.github_org):
            if "/" in x:
                a.github_org.remove(x)
                a.github_repo.append(x)
        from .check import preflight
        return preflight(a)

    if a.llm:
        if not a.llm_key:
            sys.exit(f"--llm {a.llm} needs an API key: pass --llm-key or set the environment variable.")
        if not a.yes_send_excerpts:
            if not sys.stdin.isatty():
                sys.exit("LLM mode sends code excerpts off this machine. Add --yes-send-excerpts to agree.")
            print("\nLLM mode sends a short excerpt of EACH repo (README start, file list, manifests and\n"
                  f"the start of up to 6 source files, at most 40,000 characters) to {a.llm}.")
            if input("Type yes to agree: ").strip().lower() != "yes":
                sys.exit("Stopped. Run without --llm to keep all code on this machine.")

    if not shutil.which("git"):
        sys.exit("git is not installed or not on PATH. Install it from https://git-scm.com and retry.")
    a.github_org = [x.strip() for x in a.github_org]
    for x in list(a.github_org):  # "owner/name" given as an org means one repo
        if "/" in x:
            a.github_org.remove(x)
            a.github_repo.append(x)
    from .extract import Extractor
    ex = Extractor(a, Path(a.out).expanduser().resolve())
    if not a.resume and not (a.github_org or a.github_repo or a.gitlab_group or a.gitlab_project
                             or a.bitbucket_workspace or a.bitbucket_repo or a.local_dir or a.repos_file):
        sys.exit("Say what to scan, for example --github-org acme. Run with --help for options.")
    zpath = ex.run(Path(a.resume).resolve() if a.resume else None)
    print(f"\nFinished. Output zip:\n  {zpath}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
