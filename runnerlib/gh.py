"""Minimal GitHub REST client. Stdlib only; token via `gh auth token`;
ETag conditional requests (304s don't count against the rate limit)."""

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
NOT_MODIFIED = object()  # sentinel returned on 304


class GitHub:
    def __init__(self, etag_path: Path):
        self._token = None
        self.etag_path = etag_path
        try:
            self.etags = json.loads(etag_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.etags = {}

    # -- plumbing ------------------------------------------------------------

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, check=True
            ).stdout.strip()
        return self._token

    def _save_etags(self):
        self.etag_path.parent.mkdir(parents=True, exist_ok=True)
        self.etag_path.write_text(json.dumps(self.etags))

    def _request(self, method: str, url: str, body: dict | None, etag_key: str | None,
                 _retry_auth: bool = True):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pipeline-runner",
        }
        if etag_key and etag_key in self.etags:
            headers["If-None-Match"] = self.etags[etag_key]
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                if etag_key and resp.headers.get("ETag"):
                    self.etags[etag_key] = resp.headers["ETag"]
                    self._save_etags()
                parsed = json.loads(payload) if payload else None
                return parsed, resp.headers
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return NOT_MODIFIED, e.headers
            if e.code == 401 and _retry_auth:
                # gh's keyring token rotates out from under the long-lived
                # daemon; re-read it once instead of replaying a dead
                # credential until someone restarts the process.
                self._token = None
                return self._request(method, url, body, etag_key, _retry_auth=False)
            detail = e.read().decode(errors="replace")[:500]
            raise RuntimeError(f"GitHub {method} {url} -> {e.code}: {detail}") from e

    def get(self, path: str, params: dict | None = None, etag: bool = False, paginate: bool = True):
        """GET with optional ETag caching and Link-header pagination.
        Returns NOT_MODIFIED when the ETag matched."""
        url = f"{API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        etag_key = url if etag else None
        data, headers = self._request("GET", url, None, etag_key)
        if data is NOT_MODIFIED:
            return NOT_MODIFIED
        while paginate and isinstance(data, list):
            nxt = _next_link(headers.get("Link", ""))
            if not nxt:
                break
            more, headers = self._request("GET", nxt, None, None)
            data.extend(more)
        return data

    def post(self, path: str, body: dict):
        data, _ = self._request("POST", f"{API}{path}", body, None)
        return data

    def put(self, path: str, body: dict):
        data, _ = self._request("PUT", f"{API}{path}", body, None)
        return data

    # -- endpoints -----------------------------------------------------------

    def pulls(self, repo: str, base: str = None, head: str = None):
        """All PRs (open + closed) targeting `base` or from `head`
        ("owner:branch"). One ETag'd call per story per filter."""
        params = {"state": "all", "per_page": 100, "sort": "updated", "direction": "desc"}
        if base:
            params["base"] = base
        if head:
            params["head"] = head
        return self.get(f"/repos/{repo}/pulls", params, etag=True)

    def issue_comments_since(self, repo: str, since: str):
        """Repo-wide issue comments (includes PR conversation comments)."""
        return self.get(
            f"/repos/{repo}/issues/comments",
            {"since": since, "per_page": 100, "sort": "created", "direction": "asc"},
        )

    def review_comments_since(self, repo: str, since: str):
        """Repo-wide PR review (line) comments."""
        return self.get(
            f"/repos/{repo}/pulls/comments",
            {"since": since, "per_page": 100, "sort": "created", "direction": "asc"},
        )

    def reviews(self, repo: str, pr: int):
        return self.get(f"/repos/{repo}/pulls/{pr}/reviews", {"per_page": 100}, etag=True)

    def review_comments_for(self, repo: str, pr: int, review_id: int):
        return self.get(f"/repos/{repo}/pulls/{pr}/reviews/{review_id}/comments", {"per_page": 100})

    def comment(self, repo: str, issue: int, body: str):
        return self.post(f"/repos/{repo}/issues/{issue}/comments", {"body": body})

    def pr(self, repo: str, number: int):
        return self.get(f"/repos/{repo}/pulls/{number}")

    def check_runs(self, repo: str, ref: str):
        """Check runs for a branch head. Returns list of {name, status, conclusion}."""
        data = self.get(f"/repos/{repo}/commits/{ref}/check-runs", {"per_page": 100})
        return (data or {}).get("check_runs", [])

    def merge_pr(self, repo: str, number: int, method: str = "merge"):
        return self.put(f"/repos/{repo}/pulls/{number}/merge", {"merge_method": method})


def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None
