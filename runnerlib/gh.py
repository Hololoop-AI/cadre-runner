"""GitHub transport via the gh CLI. The runner speaks to the forge with the
same tool an operator does: gh owns auth, hostname, proxies and TLS, so a
GitHub Enterprise deployment is `gh auth login --hostname` plus GH_HOST, not
a code change (driver decision 2026-09-14 — the CLI is the edge; this module
only shapes requests and parses responses).

ETag conditional requests are kept from the urllib era: 304s don't count
against the rate limit, and the 45s poll loop leans on that. `gh api` treats
a 304 as an error exit, so the status line is read off `--include` output
rather than trusting the exit code.
"""

import json
import os
import subprocess
import urllib.parse
from pathlib import Path

NOT_MODIFIED = object()  # sentinel returned on 304


def host() -> str:
    """The forge gh talks to. GH_HOST is gh's own routing variable — reusing
    it means the API transport and these links can never disagree."""
    return os.environ.get("GH_HOST") or "github.com"


def web_url(repo: str, number) -> str:
    """Browser link for a PR, on whatever host the runner is deployed against."""
    return f"https://{host()}/{repo}/pull/{number}"


class GitHub:
    def __init__(self, etag_path: Path):
        self.etag_path = etag_path
        try:
            self.etags = json.loads(etag_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.etags = {}

    # -- plumbing ------------------------------------------------------------

    def _save_etags(self):
        self.etag_path.parent.mkdir(parents=True, exist_ok=True)
        self.etag_path.write_text(json.dumps(self.etags))

    def _request(self, method: str, url: str, body: dict | None, etag_key: str | None):
        """One `gh api` invocation. `url` is a path (/repos/...) or the
        absolute next-page URL from a Link header — gh accepts both."""
        argv = ["gh", "api", "--include", "--method", method,
                "-H", "X-GitHub-Api-Version: 2022-11-28", url]
        if etag_key and etag_key in self.etags:
            argv += ["-H", f"If-None-Match: {self.etags[etag_key]}"]
        stdin = None
        if body is not None:
            argv += ["--input", "-"]
            stdin = json.dumps(body)
        proc = subprocess.run(argv, capture_output=True, text=True,
                              input=stdin, timeout=120)
        status, headers, payload = _split_response(proc.stdout)
        if status == 304:
            return NOT_MODIFIED, headers
        if proc.returncode != 0 or (status or 0) >= 400:
            detail = (payload or proc.stderr).strip()[:500]
            raise RuntimeError(
                f"GitHub {method} {url} -> {status or f'gh exit {proc.returncode}'}: {detail}")
        if etag_key and headers.get("etag"):
            self.etags[etag_key] = headers["etag"]
            self._save_etags()
        return (json.loads(payload) if payload.strip() else None), headers

    def get(self, path: str, params: dict | None = None, etag: bool = False, paginate: bool = True):
        """GET with optional ETag caching and Link-header pagination.
        Returns NOT_MODIFIED when the ETag matched."""
        url = path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        etag_key = url if etag else None
        data, headers = self._request("GET", url, None, etag_key)
        if data is NOT_MODIFIED:
            return NOT_MODIFIED
        while paginate and isinstance(data, list):
            nxt = _next_link(headers.get("link", ""))
            if not nxt:
                break
            more, headers = self._request("GET", nxt, None, None)
            data.extend(more)
        return data

    def post(self, path: str, body: dict):
        data, _ = self._request("POST", path, body, None)
        return data

    def put(self, path: str, body: dict):
        data, _ = self._request("PUT", path, body, None)
        return data

    # -- endpoints -----------------------------------------------------------

    def pulls(self, repo: str, base: str = None, head: str = None, etag: bool = True):
        """All PRs (open + closed) targeting `base` or from `head`
        ("owner:branch"). One ETag'd call per story per filter; pass
        etag=False to force a full fetch (a consumer with no cache to fall
        back on gets nothing useful from a 304)."""
        params = {"state": "all", "per_page": 100, "sort": "updated", "direction": "desc"}
        if base:
            params["base"] = base
        if head:
            params["head"] = head
        return self.get(f"/repos/{repo}/pulls", params, etag=etag)

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


def _split_response(text: str) -> tuple[int | None, dict, str]:
    """(status, lower-cased headers, body) from `gh api --include` output.
    Consecutive header blocks (redirects) are consumed; the last one wins."""
    status, headers = None, {}
    while text.startswith("HTTP/"):
        sep = text.find("\r\n\r\n")
        nl = "\r\n"
        if sep == -1:
            sep, nl = text.find("\n\n"), "\n"
        if sep == -1:
            break
        block, text = text[:sep], text[sep + 2 * len(nl):]
        lines = block.split(nl)
        try:
            status = int(lines[0].split()[1])
        except (IndexError, ValueError):
            status = None
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
    return status, headers, text


def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None
