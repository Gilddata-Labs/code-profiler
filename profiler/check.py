"""Preflight check: confirms tools, tokens and access before a real run.
Read only. Prints PASS / WARN / FAIL lines and returns a non zero code on FAIL."""
from __future__ import annotations

import shutil
import sys

from .platforms import ApiError, Bitbucket, GitHub, GitLab
from .util import run


def _line(status: str, msg: str) -> None:
    print(f"  [{status}] {msg}")


def preflight(a) -> int:
    fails = 0
    print("\nChecking this computer")
    if sys.version_info >= (3, 9):
        _line("PASS", f"Python {sys.version.split()[0]}")
    else:
        _line("FAIL", "Python 3.9 or newer is needed"); fails += 1
    if shutil.which("git"):
        _line("PASS", run(["git", "--version"], check=False).strip())
    else:
        _line("FAIL", "git not found on PATH"); fails += 1
    _line("PASS" if shutil.which("scc") else "WARN",
          "scc found, it will count lines" if shutil.which("scc")
          else "scc not found, the built in line counter will be used (fine)")

    def try_list(client, name, orgs, repos):
        nonlocal fails
        for org in orgs:
            try:
                n = len(client.list_repos(org))
                _line("PASS" if n else "WARN", f"{name} {org}: {n} repositories visible")
            except Exception as e:  # noqa: BLE001
                _line("FAIL", f"{name} {org}: {e}"); fails += 1
        for full in repos:
            try:
                meta = client.repo(full)
                _line("PASS", f"{name} {full}: readable, default branch "
                              f"{meta.get('default_branch') or '?'}")
                prs = client.merged_prs(full, limit=1)
                _line("PASS", f"{name} {full}: pull request data readable ({len(prs)} sampled)")
                hdr = client.clone_auth_header()
                run(["git", "-c", f"http.extraHeader={hdr}", "ls-remote", "--heads",
                     client.clone_url(full)], timeout=60)
                _line("PASS", f"{name} {full}: git clone access works")
            except Exception as e:  # noqa: BLE001
                _line("FAIL", f"{name} {full}: {e}"); fails += 1

    if a.github_org or a.github_repo:
        print("\nChecking GitHub")
        if not a.github_token:
            _line("FAIL", "no GitHub token (set GITHUB_TOKEN or pass --github-token)"); fails += 1
        else:
            gh = GitHub(a.github_token, a.github_host)
            try:
                r = gh.get("/user")
                scopes = r.headers.get("x-oauth-scopes", "")
                _line("PASS", f"token works for {r.json().get('login')}; scopes: {scopes or 'fine grained'}")
                if scopes and "repo" not in scopes and "public_repo" not in scopes:
                    _line("WARN", "token lacks the repo scope; private repos will be missing")
                rl = gh.get("/rate_limit").json().get("resources", {}).get("core", {})
                _line("PASS", f"API calls left this hour: {rl.get('remaining')} of {rl.get('limit')}")
            except ApiError as e:
                _line("FAIL", f"token rejected: {e}"); fails += 1
            try_list(gh, "GitHub", a.github_org, a.github_repo[:3])

    if a.gitlab_group or a.gitlab_project:
        print("\nChecking GitLab")
        if not a.gitlab_token:
            _line("FAIL", "no GitLab token (set GITLAB_TOKEN or pass --gitlab-token)"); fails += 1
        else:
            gl = GitLab(a.gitlab_token, a.gitlab_host)
            try:
                _line("PASS", f"token works for {gl.get('/user').json().get('username')}")
            except ApiError as e:
                _line("FAIL", f"token rejected: {e}"); fails += 1
            try_list(gl, "GitLab", a.gitlab_group, a.gitlab_project[:3])

    if a.bitbucket_workspace or a.bitbucket_repo:
        print("\nChecking Bitbucket")
        if not a.bitbucket_token:
            _line("FAIL", "no Bitbucket token (set BITBUCKET_TOKEN or pass --bitbucket-token)"); fails += 1
        else:
            bb = Bitbucket(a.bitbucket_token, a.bitbucket_email)
            try_list(bb, "Bitbucket", a.bitbucket_workspace, a.bitbucket_repo[:3])

    if a.llm:
        print("\nChecking LLM")
        if not a.llm_key:
            _line("FAIL", "LLM mode is on but no API key was given"); fails += 1
        else:
            from .llm import DEFAULT_MODELS, _call
            try:
                _call(a.llm, a.llm_key, a.llm_model or DEFAULT_MODELS[a.llm], "Reply with {}")
                _line("PASS", f"{a.llm} key works")
            except Exception as e:  # noqa: BLE001
                _line("FAIL", f"{a.llm}: {e}"); fails += 1

    print("\nAll checks passed. You can run the profiler." if not fails
          else f"\n{fails} check(s) failed. Fix them before running.")
    return 1 if fails else 0
