"""Read only clients for GitHub and GitLab.

Every call here is a GET (or a GitHub GraphQL query, which is read only).
Nothing in this module writes to the platform.
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Iterator

from . import http as requests
from .util import is_bot, log, redact

PR_TIERS = ("simple", "standard", "rich")


def pr_tier_v1(changed_files: int, reviews: int, review_threads: int,
               linked_issues: int, comments: int) -> str:
    """v1 rule kept for comparison with the original profiler's pr_tiers.json."""
    if changed_files > 10:
        return "other"
    if linked_issues:
        return "rich"
    if changed_files <= 2:
        return "other" if comments else "simple"
    return "standard"


def pr_tier(changed_loc: int, changed_files: int, bands: dict | None = None) -> str:
    """PRD rule. simple: <= 50 changed lines AND <= 3 files; rich: > 500 lines OR
    > 15 files; standard: everything else."""
    b = bands or {"simple_max_loc": 50, "simple_max_files": 3, "rich_min_loc": 500, "rich_min_files": 15}
    if changed_loc > b["rich_min_loc"] or changed_files > b["rich_min_files"]:
        return "rich"
    if changed_loc <= b["simple_max_loc"] and changed_files <= b["simple_max_files"]:
        return "simple"
    return "standard"


class ApiError(RuntimeError):
    pass


class _Client:
    def __init__(self, base: str, headers: dict, rate_wait: int = 1800):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update(headers)
        self.rate_wait = rate_wait

    def _req(self, method: str, url: str, **kw) -> requests.Response:
        if not url.startswith("http"):
            url = f"{self.base}/{url.lstrip('/')}"
        for attempt in range(6):
            try:
                r = self.s.request(method, url, timeout=60, **kw)
            except requests.RequestException as e:
                if attempt == 5:
                    raise ApiError(f"network error: {redact(str(e))}")
                log.warning("Network error on %s (attempt %d of 6): %s; retrying in %ds",
                            redact(url), attempt + 1, redact(str(e))[:120], 2 ** attempt)
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 502, 503, 504) or (
                r.status_code == 403 and "rate limit" in r.text.lower()
            ):
                wait = self._rate_wait_seconds(r, attempt)
                log.warning("Rate limited or busy (%s) on %s, waiting %ss",
                            r.status_code, redact(url), wait)
                time.sleep(wait)
                continue
            return r
        raise ApiError(f"rate limit: gave up on {redact(url)}")

    def _rate_wait_seconds(self, r: requests.Response, attempt: int) -> int:
        reset = r.headers.get("x-ratelimit-reset") or r.headers.get("ratelimit-reset")
        retry = r.headers.get("retry-after")
        if retry and retry.isdigit():
            return min(int(retry) + 1, self.rate_wait)
        if reset and reset.isdigit():
            return max(1, min(int(reset) - int(time.time()) + 2, self.rate_wait))
        return min(5 * 2 ** attempt, self.rate_wait)

    def get(self, url: str, params: dict | None = None, ok404: bool = False):
        r = self._req("GET", url, params=params)
        if ok404 and r.status_code in (404, 409):
            return None
        if r.status_code >= 400:
            raise ApiError(f"GET {redact(url)} -> {r.status_code}: {r.text[:300]}")
        return r


# ----------------------------------------------------------------- GitHub
class GitHub(_Client):
    platform = "github"

    def __init__(self, token: str, host: str = "github.com", rate_wait: int = 1800):
        api = "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"
        self.graphql_url = ("https://api.github.com/graphql" if host == "github.com"
                            else f"https://{host}/api/graphql")
        super().__init__(api, {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gilddata-profiler",
        }, rate_wait)
        self.host = host
        self.token = token

    def clone_url(self, full_name: str) -> str:
        return f"https://{self.host}/{full_name}.git"

    def clone_auth_header(self) -> str:
        import base64
        raw = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return f"Authorization: Basic {raw}"

    def paginate(self, path: str, params: dict | None = None, limit: int | None = None) -> Iterator[dict]:
        params = dict(params or {}, per_page=100)
        url, n = path, 0
        while url:
            r = self.get(url, params=params)
            for item in r.json():
                yield item
                n += 1
                if limit and n >= limit:
                    return
            url = r.links.get("next", {}).get("url")
            params = None

    def count(self, path: str, params: dict | None = None) -> int | str:
        """Count list items with one request, reading the last page number."""
        try:
            r = self.get(path, params=dict(params or {}, per_page=1), ok404=True)
        except ApiError:
            return ""
        if r is None:
            return 0
        last = r.links.get("last", {}).get("url")
        if last:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(last).query)
            return int(q.get("page", ["0"])[0])
        return len(r.json())

    def list_repos(self, org: str) -> list[dict]:
        kind = "orgs"
        r = self.get(f"/orgs/{org}", ok404=True)
        if r is None:
            kind = "users"
        path = f"/{kind}/{org}/repos"
        params = {"type": "all"} if kind == "orgs" else {"type": "owner"}
        return list(self.paginate(path, params))

    def repo(self, full_name: str) -> dict:
        return self.get(f"/repos/{full_name}").json()

    def languages(self, full_name: str) -> dict:
        return self.get(f"/repos/{full_name}/languages").json()

    def contributors(self, full_name: str) -> list[dict]:
        try:
            return list(self.paginate(f"/repos/{full_name}/contributors", {"anon": "0"}))
        except ApiError:
            return []

    def refs(self, full_name: str) -> dict:
        out = {}
        for kind, prefix in (("branches", "refs/heads/"), ("tags", "refs/tags/")):
            try:
                for b in self.paginate(f"/repos/{full_name}/{kind}"):
                    out[prefix + b["name"]] = b["commit"]["sha"]
            except ApiError:
                pass
        return out

    def counts(self, full_name: str, meta: dict) -> dict:
        # One GraphQL query instead of three /search/issues calls: the search API allows only
        # 30 calls a minute, which stalled every worker on an org sized run.
        totals = self._graphql_totals(full_name)
        return {
            "prs": totals.get("prs", ""),
            "merged_prs": totals.get("merged_prs", ""),
            "issues": totals.get("issues", ""),
            "labels": self.count(f"/repos/{full_name}/labels"),
            "milestones": self.count(f"/repos/{full_name}/milestones", {"state": "all"}),
            "releases": self.count(f"/repos/{full_name}/releases"),
            "branches": self.count(f"/repos/{full_name}/branches"),
            "tags": self.count(f"/repos/{full_name}/tags"),
            "commits": self.count(f"/repos/{full_name}/commits",
                                  {"sha": meta.get("default_branch", "")}),
            "default_branch": meta.get("default_branch", ""),
            "size_kb": meta.get("size", ""),
            "has_wiki": str(meta.get("has_wiki", "")).lower(),
            "has_issues": str(meta.get("has_issues", "")).lower(),
            "issue_comments": self.count(f"/repos/{full_name}/issues/comments"),
            "review_comments": self.count(f"/repos/{full_name}/pulls/comments"),
            "commit_comments": self.count(f"/repos/{full_name}/comments"),
            "collaborators": self.count(f"/repos/{full_name}/collaborators"),
        }

    def _graphql_totals(self, full_name: str) -> dict:
        """PR and issue totals. Two queries, because a fine grained token without Issues read
        makes GitHub null the whole repository object when issues are in the same query."""
        owner, name = full_name.split("/", 1)
        out = {}
        queries = {
            ("prs", "merged_prs"): ("query($o:String!,$n:String!){repository(owner:$o,name:$n){"
                                    "prs: pullRequests{totalCount} merged: pullRequests(states:MERGED){totalCount}}}",
                                    lambda repo: {"prs": repo["prs"]["totalCount"], "merged_prs": repo["merged"]["totalCount"]}),
            ("issues",): ("query($o:String!,$n:String!){repository(owner:$o,name:$n){issues{totalCount}}}",
                          lambda repo: {"issues": repo["issues"]["totalCount"]}),
        }
        for keys, (q, pick) in queries.items():
            try:
                r = self._req("POST", self.graphql_url, json={"query": q, "variables": {"o": owner, "n": name}})
                repo = r.json()["data"]["repository"]
                out.update(pick(repo))
            except (ApiError, KeyError, TypeError, ValueError):
                out.update({k: "" for k in keys})   # not readable with this token
        return out

    def _search_count(self, q: str) -> int | str:
        try:
            return self.get("/search/issues", {"q": q, "per_page": 1}).json()["total_count"]
        except (ApiError, KeyError):
            return ""

    _PR_QUERY = """
    query($owner:String!, $name:String!, $cursor:String) {
      repository(owner:$owner, name:$name) {
        pullRequests(states:MERGED, first:25, after:$cursor,
                     orderBy:{field:CREATED_AT, direction:DESC}) {
          pageInfo { hasNextPage endCursor }
          nodes {
            number title bodyText url createdAt mergedAt
            changedFiles additions deletions totalCommentsCount
            mergeCommit { oid }
            author { login __typename }
            labels(first:20) { nodes { name } }
            commits { totalCount }
            files(first:100) { pageInfo { hasNextPage } nodes { path additions deletions } }
            closingIssuesReferences(first:10) { nodes { number } }
            comments(first:1) { totalCount }
            reviews(first:1) { totalCount }
            approvals: reviews(first:1, states:APPROVED) { totalCount }
            reviewThreads(first:1) { totalCount }
          }
        }
      }
    }"""

    def merged_prs(self, full_name: str, limit: int | None = None,
                   checkpoint: "str | None" = None) -> list[dict]:
        """All merged PRs with file stats, newest first. About 0.2 s of API time per PR, so a
        repo with 9,000 merged PRs takes about 30 minutes; progress is logged every 20 pages.
        Pages are 25 PRs: 50 made GitHub answer 502/504 on large repos.
        With `checkpoint` (a file path) every page is appended there, and a later call continues
        from the saved cursor, so a stopped or failed run does not refetch everything."""
        import json as _json
        import time as _time
        from pathlib import Path as _Path
        owner, name = full_name.split("/", 1)
        out, cursor, pages, t0 = [], None, 0, _time.time()
        ck = _Path(checkpoint) if checkpoint else None
        if ck and ck.exists():
            for line in ck.read_text(encoding="utf-8").splitlines():
                rec = _json.loads(line)
                out.extend(rec["nodes"])
                cursor = rec["cursor"]
                if rec.get("done"):
                    return out[:limit] if limit else out
            if out:
                log.info("%s: continuing merged PRs from checkpoint (%d read before)", full_name, len(out))
        while True:
            r = self._req("POST", self.graphql_url, json={
                "query": self._PR_QUERY,
                "variables": {"owner": owner, "name": name, "cursor": cursor}})
            body = r.json() if r.content else {}
            if r.status_code >= 400 or body.get("errors"):
                raise ApiError(f"GraphQL merged PRs -> {r.status_code}: {str(body)[:300]}")
            page = body["data"]["repository"]["pullRequests"]
            pages += 1
            if pages % 20 == 0:
                log.info("%s: %d merged PRs read so far (%.0fs)", full_name, len(out) + len(page["nodes"]),
                         _time.time() - t0)
            for n in page["nodes"]:
                login = (n.get("author") or {}).get("login", "")
                kind = (n.get("author") or {}).get("__typename", "")
                n["author_is_bot"] = kind == "Bot" or is_bot(login)
                n["merge_commit"] = (n.pop("mergeCommit") or {}).get("oid", "")
                n["approvals"] = (n.pop("approvals", None) or {}).get("totalCount", 0)
                n["file_stats"] = [{"path": f.get("path"), "additions": f.get("additions") or 0,
                                    "deletions": f.get("deletions") or 0}
                                   for f in (n.get("files") or {}).get("nodes", [])]
                n["files_complete"] = not ((n.get("files") or {}).get("pageInfo") or {}).get("hasNextPage")
                out.append(n)
            done = not page["pageInfo"]["hasNextPage"] or bool(limit and len(out) >= limit)
            if ck:
                with ck.open("a", encoding="utf-8") as fh:
                    fh.write(_json.dumps({"cursor": page["pageInfo"]["endCursor"], "nodes": page["nodes"],
                                          "done": done}) + "\n")
            if done:
                return out[:limit] if limit else out
            cursor = page["pageInfo"]["endCursor"]

    def total_prs(self, full_name: str) -> int | str:
        owner, name = full_name.split("/", 1)
        q = ("query($o:String!,$n:String!){repository(owner:$o,name:$n){"
             "pullRequests(states:[OPEN,CLOSED,MERGED]){totalCount}}}")
        r = self._req("POST", self.graphql_url, json={"query": q, "variables": {"o": owner, "n": name}})
        try:
            return r.json()["data"]["repository"]["pullRequests"]["totalCount"]
        except (KeyError, TypeError, ValueError):
            return ""

    @staticmethod
    def tier(pr: dict) -> str:
        return pr_tier_v1(
            pr.get("changedFiles") or 0,
            (pr.get("reviews") or {}).get("totalCount", 0),
            (pr.get("reviewThreads") or {}).get("totalCount", 0),
            len((pr.get("closingIssuesReferences") or {}).get("nodes", [])),
            (pr.get("comments") or {}).get("totalCount", 0),
        )


# ----------------------------------------------------------------- GitLab
class GitLab(_Client):
    platform = "gitlab"

    def __init__(self, token: str, host: str = "gitlab.com", rate_wait: int = 1800):
        super().__init__(f"https://{host}/api/v4", {
            "PRIVATE-TOKEN": token, "User-Agent": "gilddata-profiler"}, rate_wait)
        self.host = host
        self.token = token

    def clone_url(self, full_name: str) -> str:
        return f"https://{self.host}/{full_name}.git"

    def clone_auth_header(self) -> str:
        import base64
        raw = base64.b64encode(f"oauth2:{self.token}".encode()).decode()
        return f"Authorization: Basic {raw}"

    @staticmethod
    def pid(full_name: str) -> str:
        return urllib.parse.quote(full_name, safe="")

    def paginate(self, path: str, params: dict | None = None, limit: int | None = None) -> Iterator[dict]:
        params = dict(params or {}, per_page=100)
        page, n = 1, 0
        while True:
            r = self.get(path, params=dict(params, page=page))
            items = r.json()
            for item in items:
                yield item
                n += 1
                if limit and n >= limit:
                    return
            nxt = r.headers.get("x-next-page")
            if not nxt or not items:
                return
            page = int(nxt)

    def count(self, path: str, params: dict | None = None) -> int | str:
        try:
            r = self.get(path, params=dict(params or {}, per_page=1), ok404=True)
        except ApiError:
            return ""
        if r is None:
            return 0
        total = r.headers.get("x-total")
        return int(total) if total and total.isdigit() else ""

    def list_repos(self, group: str) -> list[dict]:
        g = self.pid(group)
        r = self.get(f"/groups/{g}", ok404=True)
        if r is None:
            projs = list(self.paginate(f"/users/{group}/projects"))
        else:
            projs = list(self.paginate(f"/groups/{g}/projects",
                                       {"include_subgroups": "true", "archived": "false"}))
        return projs

    def repo(self, full_name: str) -> dict:
        return self.get(f"/projects/{self.pid(full_name)}", {"statistics": "true"}).json()

    def languages(self, full_name: str) -> dict:
        try:
            return self.get(f"/projects/{self.pid(full_name)}/languages").json()
        except ApiError:
            return {}

    def contributors(self, full_name: str) -> list[dict]:
        try:
            return list(self.paginate(f"/projects/{self.pid(full_name)}/repository/contributors"))
        except ApiError:
            return []

    def refs(self, full_name: str) -> dict:
        out, p = {}, self.pid(full_name)
        for kind, prefix in (("branches", "refs/heads/"), ("tags", "refs/tags/")):
            try:
                for b in self.paginate(f"/projects/{p}/repository/{kind}"):
                    out[prefix + b["name"]] = b["commit"]["id"]
            except ApiError:
                pass
        return out

    def counts(self, full_name: str, meta: dict) -> dict:
        p = self.pid(full_name)
        stats = meta.get("statistics") or {}
        return {
            "prs": self.count(f"/projects/{p}/merge_requests", {"state": "all"}),
            "merged_prs": self.count(f"/projects/{p}/merge_requests", {"state": "merged"}),
            "issues": self.count(f"/projects/{p}/issues"),
            "labels": self.count(f"/projects/{p}/labels"),
            "milestones": self.count(f"/projects/{p}/milestones"),
            "releases": self.count(f"/projects/{p}/releases"),
            "branches": self.count(f"/projects/{p}/repository/branches"),
            "tags": self.count(f"/projects/{p}/repository/tags"),
            "commits": stats.get("commit_count", ""),
            "default_branch": meta.get("default_branch", ""),
            "size_kb": round(stats["repository_size"] / 1024) if stats.get("repository_size") else "",
            "has_wiki": str(meta.get("wiki_enabled", "")).lower(),
            "has_issues": str(meta.get("issues_enabled", "")).lower(),
            "issue_comments": "",
            "review_comments": "",
            "commit_comments": "",
            "collaborators": self.count(f"/projects/{p}/members/all"),
        }

    def merged_prs(self, full_name: str, limit: int | None = None) -> list[dict]:
        p = self.pid(full_name)
        out = []
        for mr in self.paginate(f"/projects/{p}/merge_requests",
                                {"state": "merged", "order_by": "created_at"}, limit=limit):
            iid = mr["iid"]
            detail = self.get(f"/projects/{p}/merge_requests/{iid}/changes",
                              {"access_raw_diffs": "false"}, ok404=True)
            changes = (detail.json().get("changes", []) if detail else [])
            adds = dels = 0
            file_stats = []
            for c in changes:
                fa = fd = 0
                for line in (c.get("diff") or "").splitlines():
                    if line.startswith("+") and not line.startswith("+++"):
                        fa += 1
                    elif line.startswith("-") and not line.startswith("---"):
                        fd += 1
                adds, dels = adds + fa, dels + fd
                file_stats.append({"path": c.get("new_path"), "additions": fa, "deletions": fd})
            approvals = self.get(f"/projects/{p}/merge_requests/{iid}/approvals", ok404=True)
            n_approved = len((approvals.json() if approvals else {}).get("approved_by", []))
            closes = self.get(f"/projects/{p}/merge_requests/{iid}/closes_issues", ok404=True)
            n_closes = len(closes.json()) if closes else 0
            discussions = self.count(f"/projects/{p}/merge_requests/{iid}/discussions") or 0
            author = (mr.get("author") or {}).get("username", "")
            out.append({
                "number": iid,
                "title": mr.get("title"),
                "bodyText": mr.get("description") or "",
                "url": mr.get("web_url"),
                "createdAt": mr.get("created_at"),
                "mergedAt": mr.get("merged_at"),
                "changedFiles": len(changes),
                "additions": adds,
                "deletions": dels,
                "totalCommentsCount": mr.get("user_notes_count", 0),
                "merge_commit": mr.get("merge_commit_sha") or mr.get("squash_commit_sha") or "",
                "author": {"login": author},
                "labels": {"nodes": [{"name": l} for l in mr.get("labels", [])]},
                "files": {"nodes": [{"path": c.get("new_path")} for c in changes]},
                "closingIssuesReferences": {"nodes": [{}] * n_closes},
                "comments": {"totalCount": mr.get("user_notes_count", 0)},
                "reviews": {"totalCount": n_approved},
                "reviewThreads": {"totalCount": discussions if isinstance(discussions, int) else 0},
                "author_is_bot": bool((mr.get("author") or {}).get("bot")) or is_bot(author),
                "approvals": n_approved,
                "file_stats": file_stats,
                "files_complete": True,
            })
        return out

    def total_prs(self, full_name: str) -> int | str:
        return self.count(f"/projects/{self.pid(full_name)}/merge_requests", {"state": "all"})

    tier = staticmethod(GitHub.tier)


# -------------------------------------------------------------- Bitbucket
class Bitbucket(_Client):
    """Bitbucket Cloud. Auth is either an Atlassian API token plus the account
    email, or a workspace / repository access token (Bearer)."""
    platform = "bitbucket"

    def __init__(self, token: str, email: str = "", rate_wait: int = 1800):
        import base64
        if email:
            auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
            self._git_user = "x-bitbucket-api-token-auth"
        else:
            auth = f"Bearer {token}"
            self._git_user = "x-token-auth"
        super().__init__("https://api.bitbucket.org/2.0",
                         {"Authorization": auth, "User-Agent": "gilddata-profiler"}, rate_wait)
        self.host = "bitbucket.org"
        self.token = token

    def clone_url(self, full_name: str) -> str:
        return f"https://bitbucket.org/{full_name}.git"

    def clone_auth_header(self) -> str:
        import base64
        raw = base64.b64encode(f"{self._git_user}:{self.token}".encode()).decode()
        return f"Authorization: Basic {raw}"

    def paginate(self, path: str, params: dict | None = None, limit: int | None = None) -> Iterator[dict]:
        url, p, n = path, dict(params or {}, pagelen=50), 0
        while url:
            body = self.get(url, params=p).json()
            for item in body.get("values", []):
                yield item
                n += 1
                if limit and n >= limit:
                    return
            url, p = body.get("next"), None

    def count(self, path: str, params: dict | None = None) -> int | str:
        try:
            r = self.get(path, params=dict(params or {}, pagelen=1), ok404=True)
        except ApiError:
            return ""
        if r is None:
            return 0
        return r.json().get("size", "")

    def list_repos(self, workspace: str) -> list[dict]:
        out = []
        for r in self.paginate(f"/repositories/{workspace}"):
            r["full_name"] = r.get("full_name") or f"{workspace}/{r['slug']}"
            out.append(r)
        return out

    def repo(self, full_name: str) -> dict:
        m = self.get(f"/repositories/{full_name}").json()
        m["default_branch"] = (m.get("mainbranch") or {}).get("name", "")
        m["created_at"] = m.get("created_on", "")
        m["private"] = m.get("is_private")
        m["size"] = round((m.get("size") or 0) / 1024) or ""
        m["has_wiki"], m["has_issues"] = m.get("has_wiki"), m.get("has_issues")
        m["fork"] = bool(m.get("parent"))
        return m

    def languages(self, full_name: str) -> dict:
        return {}  # Bitbucket gives one language label only; LOC per language is used instead

    def contributors(self, full_name: str) -> list[dict]:
        return []  # no contributors endpoint; commit authors are used instead

    def refs(self, full_name: str) -> dict:
        out = {}
        for kind, prefix in (("branches", "refs/heads/"), ("tags", "refs/tags/")):
            try:
                for b in self.paginate(f"/repositories/{full_name}/refs/{kind}"):
                    out[prefix + b["name"]] = (b.get("target") or {}).get("hash", "")
            except ApiError:
                pass
        return out

    def counts(self, full_name: str, meta: dict) -> dict:
        base = f"/repositories/{full_name}"
        return {
            "prs": self.count(f"{base}/pullrequests",
                              {"state": ["OPEN", "MERGED", "DECLINED", "SUPERSEDED"]}),
            "merged_prs": self.count(f"{base}/pullrequests", {"state": "MERGED"}),
            "issues": self.count(f"{base}/issues") if meta.get("has_issues") else 0,
            "labels": "", "milestones": self.count(f"{base}/milestones") if meta.get("has_issues") else "",
            "releases": "",
            "branches": self.count(f"{base}/refs/branches"),
            "tags": self.count(f"{base}/refs/tags"),
            "commits": "",
            "default_branch": meta.get("default_branch", ""),
            "size_kb": meta.get("size", ""),
            "has_wiki": str(meta.get("has_wiki", "")).lower(),
            "has_issues": str(meta.get("has_issues", "")).lower(),
            "issue_comments": "", "review_comments": "", "commit_comments": "",
            "collaborators": "",
        }

    def merged_prs(self, full_name: str, limit: int | None = None) -> list[dict]:
        base = f"/repositories/{full_name}/pullrequests"
        out = []
        for pr in self.paginate(base, {"state": "MERGED", "sort": "-created_on"}, limit=limit):
            pid = pr["id"]
            adds = dels = 0
            paths = []
            file_stats = []
            try:
                for d in self.paginate(f"{base}/{pid}/diffstat"):
                    adds += d.get("lines_added") or 0
                    dels += d.get("lines_removed") or 0
                    path = ((d.get("new") or d.get("old")) or {}).get("path")
                    paths.append(path)
                    file_stats.append({"path": path, "additions": d.get("lines_added") or 0,
                                       "deletions": d.get("lines_removed") or 0})
            except ApiError:
                pass
            detail = self.get(f"{base}/{pid}", ok404=True)
            detail = detail.json() if detail else {}
            approvals = sum(1 for p in detail.get("participants", []) if p.get("approved"))
            author = (pr.get("author") or {}).get("nickname") or (pr.get("author") or {}).get("display_name", "")
            out.append({
                "number": pid,
                "title": pr.get("title"),
                "bodyText": pr.get("description") or "",
                "url": ((pr.get("links") or {}).get("html") or {}).get("href"),
                "createdAt": pr.get("created_on"),
                "mergedAt": pr.get("updated_on"),
                "changedFiles": len(paths),
                "additions": adds,
                "deletions": dels,
                "totalCommentsCount": pr.get("comment_count", 0),
                "merge_commit": (pr.get("merge_commit") or {}).get("hash", ""),
                "author": {"login": author},
                "labels": {"nodes": []},
                "files": {"nodes": [{"path": p} for p in paths]},
                "closingIssuesReferences": {"nodes": []},
                "comments": {"totalCount": pr.get("comment_count", 0)},
                "reviews": {"totalCount": approvals},
                "reviewThreads": {"totalCount": pr.get("task_count", 0)},
                "author_is_bot": (pr.get("author") or {}).get("type") == "app_user" or is_bot(author),
                "approvals": approvals,
                "file_stats": file_stats,
                "files_complete": True,
            })
        return out

    def total_prs(self, full_name: str) -> int | str:
        return self.count(f"/repositories/{full_name}/pullrequests",
                          {"state": ["OPEN", "MERGED", "DECLINED", "SUPERSEDED"]})

    tier = staticmethod(GitHub.tier)
