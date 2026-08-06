import base64
from urllib.parse import quote

import httpx

from .retry import with_retries

LOOP_LABELS = {
    "loop:run": "1d76db",
    "loop:running": "fbca04",
    "loop:done": "0e8a16",
    "loop:failed": "b60205",
    "loop:needs-review": "e4e669",
    "loop:ready": "5319e7",
}


class GitHubError(Exception):
    pass


class FastForwardError(GitHubError):
    pass


class MergeError(GitHubError):
    pass


class GitHubClient:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None):
        self._owns_http = client is None
        self._http = client or httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _req(self, method: str, url: str, **kw) -> httpx.Response:
        async def call() -> httpx.Response:
            r = await self._http.request(method, url, **kw)
            if r.status_code >= 500:
                r.raise_for_status()
            return r
        return await with_retries(call)

    async def get_file(self, repo: str, ref: str, path: str) -> str | None:
        r = await self._req("GET", f"/repos/{repo}/contents/{path}", params={"ref": ref})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return base64.b64decode(r.json()["content"]).decode()

    async def list_pr_files(self, repo: str, pr_number: int) -> list[str]:
        files: list[str] = []
        page = 1
        while True:
            r = await self._req("GET", f"/repos/{repo}/pulls/{pr_number}/files",
                                params={"per_page": 100, "page": page})
            r.raise_for_status()
            batch = r.json()
            files += [f["filename"] for f in batch]
            if len(batch) < 100:
                return files
            page += 1

    async def ensure_labels(self, repo: str) -> None:
        for name, color in LOOP_LABELS.items():
            r = await self._req("POST", f"/repos/{repo}/labels", json={"name": name, "color": color})
            if r.status_code not in (201, 422):  # 422 = already exists
                r.raise_for_status()

    async def add_labels(self, repo: str, pr_number: int, labels: list[str]) -> None:
        r = await self._req("POST", f"/repos/{repo}/issues/{pr_number}/labels", json={"labels": labels})
        r.raise_for_status()

    async def remove_label(self, repo: str, pr_number: int, label: str) -> None:
        r = await self._req("DELETE", f"/repos/{repo}/issues/{pr_number}/labels/{quote(label, safe='')}")
        if r.status_code not in (200, 404):
            r.raise_for_status()

    async def create_comment(self, repo: str, pr_number: int, body: str) -> None:
        r = await self._req("POST", f"/repos/{repo}/issues/{pr_number}/comments", json={"body": body})
        r.raise_for_status()

    async def branch_sha(self, repo: str, branch: str) -> str:
        r = await self._req("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        r.raise_for_status()
        return r.json()["object"]["sha"]

    async def fast_forward(self, repo: str, branch: str, sha: str) -> None:
        r = await self._req("PATCH", f"/repos/{repo}/git/refs/heads/{branch}",
                            json={"sha": sha, "force": False})
        if r.status_code == 422:
            raise FastForwardError(r.text)
        r.raise_for_status()

    async def merge_pr(self, repo: str, pr_number: int,
                       commit_title: str | None = None) -> None:
        body: dict = {"merge_method": "squash"}
        if commit_title:
            body["commit_title"] = commit_title
        r = await self._req("PUT", f"/repos/{repo}/pulls/{pr_number}/merge", json=body)
        if r.status_code in (404, 405, 409, 422):
            try:
                msg = r.json().get("message") or r.text
            except ValueError:
                msg = r.text
            raise MergeError(msg)
        r.raise_for_status()

    async def get_pr(self, repo: str, pr_number: int) -> dict:
        r = await self._req("GET", f"/repos/{repo}/pulls/{pr_number}")
        r.raise_for_status()
        return r.json()

    async def list_check_runs(self, repo: str, sha: str) -> list[dict]:
        """Latest check run per check name for the commit (GitHub's default
        filter=latest). Repos without checks return an empty list."""
        runs: list[dict] = []
        page = 1
        while True:
            r = await self._req("GET", f"/repos/{repo}/commits/{sha}/check-runs",
                                params={"per_page": 100, "page": page})
            r.raise_for_status()
            batch = r.json().get("check_runs", [])
            runs += batch
            if len(batch) < 100:
                return runs
            page += 1

    async def required_checks(self, repo: str, branch: str) -> list[str]:
        """Check names a ruleset requires before anything may merge into branch.

        Empty for an unprotected branch, or when the token cannot read the
        rules — the caller treats "unknown" the same as "none" and lets the
        merge attempt be the source of truth.
        """
        try:
            r = await self._req("GET", f"/repos/{repo}/rules/branches/{branch}")
            r.raise_for_status()
            rules = r.json()
        except Exception:  # noqa: BLE001 — best-effort: never block a merge on this
            return []
        names: list[str] = []
        for rule in rules if isinstance(rules, list) else []:
            if rule.get("type") != "required_status_checks":
                continue
            params = rule.get("parameters") or {}
            for check in params.get("required_status_checks") or []:
                name = check.get("context") if isinstance(check, dict) else check
                if isinstance(name, str):
                    names.append(name)
        return names

    async def update_pr_branch(self, repo: str, pr_number: int) -> None:
        """GitHub-side merge of the base branch into the PR head branch."""
        r = await self._req("PUT", f"/repos/{repo}/pulls/{pr_number}/update-branch")
        if r.status_code == 422:
            try:
                msg = r.json().get("message") or r.text
            except ValueError:
                msg = r.text
            raise GitHubError(msg)
        r.raise_for_status()

    async def delete_branch(self, repo: str, branch: str) -> None:
        r = await self._req("DELETE", f"/repos/{repo}/git/refs/heads/{branch}")
        if r.status_code not in (204, 404, 422):
            r.raise_for_status()

    async def get_repo_default_branch(self, repo: str) -> str:
        r = await self._req("GET", f"/repos/{repo}")
        r.raise_for_status()
        return r.json()["default_branch"]

    async def get_branch_sha(self, repo: str, branch: str) -> str | None:
        r = await self._req("GET", f"/repos/{repo}/git/ref/heads/{branch}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()["object"]["sha"]

    async def create_branch(self, repo: str, branch: str, sha: str) -> None:
        r = await self._req("POST", f"/repos/{repo}/git/refs",
                            json={"ref": f"refs/heads/{branch}", "sha": sha})
        if r.status_code != 422:  # 422 = reference already exists
            r.raise_for_status()

    async def put_file(self, repo: str, branch: str, path: str,
                       content: str, message: str) -> None:
        existing = await self._req("GET", f"/repos/{repo}/contents/{path}",
                                   params={"ref": branch})
        body = {"message": message, "branch": branch,
                "content": base64.b64encode(content.encode()).decode()}
        if existing.status_code == 200:
            body["sha"] = existing.json()["sha"]
        r = await self._req("PUT", f"/repos/{repo}/contents/{path}", json=body)
        r.raise_for_status()

    async def create_pr(self, repo: str, head: str, base: str,
                        title: str, body: str) -> int:
        r = await self._req("POST", f"/repos/{repo}/pulls",
                            json={"title": title, "head": head,
                                  "base": base, "body": body})
        r.raise_for_status()
        return r.json()["number"]

    async def list_ready_issues(self, repo: str, label: str = "loop:ready") -> list[dict]:
        issues: list[dict] = []
        page = 1
        while True:
            r = await self._req("GET", f"/repos/{repo}/issues",
                                params={"labels": label, "state": "open",
                                        "per_page": 100, "page": page})
            r.raise_for_status()
            batch = r.json()
            issues += [i for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                return issues
            page += 1

    async def issue_blocked_by(self, repo: str, number: int) -> list[int]:
        """Numbers of OPEN issues this one is blocked by (native dependencies).

        Repos/plans without the dependencies feature answer 404/410 — treated
        as "no blockers" so the scheduler keeps working.
        """
        r = await self._req("GET",
                            f"/repos/{repo}/issues/{number}/dependencies/blocked_by")
        if r.status_code in (404, 410):
            return []
        r.raise_for_status()
        return [i["number"] for i in r.json() if i.get("state") == "open"]

    async def list_issue_comments(self, repo: str, number: int,
                                  since: str | None = None) -> list[dict]:
        params: dict = {"per_page": 100}
        if since:
            params["since"] = since
        r = await self._req("GET", f"/repos/{repo}/issues/{number}/comments",
                            params=params)
        r.raise_for_status()
        return r.json()

    async def get_issue(self, repo: str, number: int) -> dict:
        r = await self._req("GET", f"/repos/{repo}/issues/{number}")
        r.raise_for_status()
        return r.json()
