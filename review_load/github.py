"""Read-only GitHub GraphQL client: token lookup, polite retries, pull request paging.

All network access goes through `urllib_transport`, so tests swap in recorded responses.
The tool only ever sends queries (never mutations) and reads PR metadata: timestamps,
line counts, review events and logins. It never requests code, titles or descriptions.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from . import __version__
from .timeutil import parse_ts

DEFAULT_API_URL = "https://api.github.com/graphql"
LOW_POINTS = 100          # pause for the reset when fewer rate-limit points than this remain
MAX_RATE_WAIT_S = 3600    # never sleep longer than this for a rate-limit reset
MIN_PAGE_SIZE = 10

PULLS_QUERY = """
query($owner: String!, $name: String!, $first: Int!, $after: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequests(first: $first, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        url
        state
        isDraft
        createdAt
        updatedAt
        closedAt
        mergedAt
        additions
        deletions
        author { login __typename }
        timelineItems(first: 1, itemTypes: [READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT]) {
          nodes {
            __typename
            ... on ReadyForReviewEvent { createdAt }
            ... on ConvertToDraftEvent { createdAt }
          }
        }
        reviews(first: 100) {
          pageInfo { hasNextPage endCursor }
          nodes { author { login __typename } state submittedAt }
        }
      }
    }
  }
}
""".strip()

MORE_REVIEWS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviews(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { author { login __typename } state submittedAt }
      }
    }
  }
}
""".strip()


class GitHubError(RuntimeError):
    """A problem the user can act on; the message says what to do."""


class PageTooHeavy(RuntimeError):
    """GitHub timed out building the page (502/504); retry with a smaller page."""


@dataclass
class HttpResponse:
    status: int
    headers: dict
    body: str


Transport = Callable[[str, dict, dict], HttpResponse]


def urllib_transport(url: str, payload: dict, headers: dict) -> HttpResponse:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return HttpResponse(resp.status, {k.lower(): v for k, v in resp.headers.items()},
                                resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return HttpResponse(exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body)
    except urllib.error.URLError as exc:
        # Network hiccup: report as a retryable server error.
        return HttpResponse(599, {}, f"network error: {exc.reason}")


# ---------------------------------------------------------------------------
# Token and repo argument


def resolve_token(env: Optional[dict] = None,
                  run: Callable = subprocess.run) -> "tuple[Optional[str], Optional[str]]":
    """Return (token, source). Looks at GITHUB_TOKEN, then GH_TOKEN, then `gh auth token`."""
    env = os.environ if env is None else env
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = (env.get(var) or "").strip()
        if value:
            return value, var
    try:
        out = run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None, None
    token = (out.stdout or "").strip() if out.returncode == 0 else ""
    return (token, "gh auth token") if token else (None, None)


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def parse_repo(text: str) -> "tuple[str, str]":
    """Accept owner/name, a github.com URL, or a git remote URL."""
    value = text.strip()
    value = re.sub(r"^(https?://|git@)", "", value)
    value = re.sub(r"^[^/:]*github[^/:]*[/:]", "", value)
    value = value.rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    parts = value.split("/")
    if len(parts) >= 2:
        value = "/".join(parts[:2])
    if not _REPO_RE.match(value):
        raise ValueError(f"expected OWNER/REPO (for example cli/cli), got {text!r}")
    owner, name = value.split("/")
    return owner, name


# ---------------------------------------------------------------------------
# Client


class GitHubClient:
    def __init__(self, token: str, api_url: str = DEFAULT_API_URL,
                 transport: Transport = urllib_transport,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time,
                 log: Optional[Callable[[str], None]] = None,
                 max_retries: int = 5):
        if not token:
            raise GitHubError("no GitHub token")
        self._token = token
        self.api_url = api_url
        self.transport = transport
        self.sleep = sleep
        self.clock = clock
        self.log = log or (lambda msg: print(msg, file=sys.stderr))
        self.max_retries = max_retries
        self.calls = 0
        self.points = 0
        self.remaining: Optional[int] = None

    def _headers(self) -> dict:
        return {
            "Authorization": f"bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"review-load/{__version__}",
        }

    def _backoff(self, resp: HttpResponse, attempt: int) -> float:
        retry_after = (resp.headers.get("retry-after") or "").strip()
        if retry_after.isdigit():
            return float(retry_after)
        reset = (resp.headers.get("x-ratelimit-reset") or "").strip()
        if resp.headers.get("x-ratelimit-remaining") == "0" and reset.isdigit():
            return max(1.0, float(reset) - self.clock() + 1)
        return min(60.0, 2.0 ** (attempt + 1))

    @staticmethod
    def _looks_rate_limited(resp: HttpResponse) -> bool:
        return ("retry-after" in resp.headers
                or resp.headers.get("x-ratelimit-remaining") == "0"
                or "rate limit" in resp.body.lower())

    def query(self, query: str, variables: dict) -> dict:
        payload = {"query": query, "variables": variables}
        for attempt in range(self.max_retries + 1):
            self.calls += 1
            resp = self.transport(self.api_url, payload, self._headers())
            last = attempt == self.max_retries

            if resp.status == 401:
                raise GitHubError("GitHub rejected the token (401). It may be expired or mistyped.")
            if resp.status in (403, 429) and self._looks_rate_limited(resp):
                if last:
                    raise GitHubError("GitHub kept rate-limiting the requests; try again later.")
                wait = self._backoff(resp, attempt)
                self._wait(wait, f"GitHub asked us to slow down ({resp.status})")
                continue
            if resp.status == 403:
                raise GitHubError(
                    "GitHub returned 403. If the org enforces SAML SSO, authorize the token for it; "
                    "for a fine-grained token, check the org allows them and grants Pull requests: "
                    "Read-only on this repo.")
            if resp.status in (502, 504):
                raise PageTooHeavy()
            if resp.status >= 500:
                if last:
                    raise GitHubError(f"GitHub API error {resp.status} after {attempt + 1} tries.")
                wait = self._backoff(resp, attempt)
                self._wait(wait, f"GitHub returned {resp.status}")
                continue
            if resp.status != 200:
                raise GitHubError(f"GitHub API error {resp.status}: {resp.body[:200]}")

            try:
                body = json.loads(resp.body)
            except ValueError:
                raise GitHubError("GitHub returned a response that is not JSON.")
            errors = body.get("errors") or []
            if any((e or {}).get("type") == "RATE_LIMITED" for e in errors):
                if last:
                    raise GitHubError("GitHub's GraphQL rate limit is used up; try again after it resets.")
                self._wait(self._backoff(resp, attempt), "GraphQL rate limit reached")
                continue
            if any((e or {}).get("type") == "NOT_FOUND" for e in errors):
                raise GitHubError(
                    "Repository not found, or this token cannot see it. For a private repo use a "
                    "fine-grained token with Pull requests: Read-only on that repo.")
            data = body.get("data")
            if errors and not data:
                raise GitHubError("GraphQL error: " + "; ".join(str((e or {}).get("message")) for e in errors)[:300])
            if errors:
                self.log("  note: GitHub returned partial data: "
                         + "; ".join(str((e or {}).get("message")) for e in errors)[:200])
            data = data or {}
            self._track(data.get("rateLimit"))
            return data
        raise GitHubError("unreachable")  # pragma: no cover

    def _track(self, rate: Optional[dict]) -> None:
        if not rate:
            return
        self.points += int(rate.get("cost") or 0)
        remaining = rate.get("remaining")
        if remaining is None:
            return
        self.remaining = int(remaining)
        reset = parse_ts(rate.get("resetAt"))
        if self.remaining < LOW_POINTS and reset is not None:
            wait = max(1.0, reset.timestamp() - self.clock() + 1)
            self._wait(wait, f"only {self.remaining} rate-limit points left")

    def _wait(self, seconds: float, why: str) -> None:
        if seconds > MAX_RATE_WAIT_S:
            raise GitHubError(f"{why}; the reset is {seconds / 60:.0f} minutes away. Try again later.")
        self.log(f"  {why}; waiting {seconds:.0f}s")
        self.sleep(seconds)


# ---------------------------------------------------------------------------
# Fetching


def fetch_pull_requests(client: GitHubClient, owner: str, name: str, since: datetime,
                        page_size: int = 50, max_prs: int = 5000) -> "tuple[list[dict], dict]":
    """Return raw PR nodes updated at or after `since`, newest update first, plus fetch info.

    Pages through `repository.pullRequests` ordered by last update and stops at the first
    PR last updated before `since`. Any review, push or comment updates a PR, so every PR
    that got a review inside the window is included.
    """
    nodes: list = []
    seen: set = set()
    cursor = None
    size = page_size
    truncated = False
    repo_name = f"{owner}/{name}"
    while True:
        try:
            data = client.query(PULLS_QUERY, {"owner": owner, "name": name, "first": size, "after": cursor})
        except PageTooHeavy:
            if size <= MIN_PAGE_SIZE:
                raise GitHubError("GitHub keeps timing out on this repo even with small pages; try again later.")
            size = max(MIN_PAGE_SIZE, size // 2)
            client.log(f"  GitHub timed out; retrying with pages of {size}")
            continue
        repo = data.get("repository")
        if not repo:
            raise GitHubError(f"Repository {repo_name} not found, or this token cannot see it.")
        repo_name = repo.get("nameWithOwner") or repo_name
        conn = repo.get("pullRequests") or {}
        page = [n for n in (conn.get("nodes") or []) if n]
        reached_end = False
        for node in page:
            updated = parse_ts(node.get("updatedAt"))
            if updated is None or updated < since:
                reached_end = True
                break
            if node["number"] in seen:
                continue
            seen.add(node["number"])
            _complete_reviews(client, owner, name, node)
            nodes.append(node)
            if len(nodes) >= max_prs:
                truncated = True
                reached_end = True
                break
        info = conn.get("pageInfo") or {}
        if reached_end or not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
        client.log(f"  fetched {len(nodes)} PRs so far")
    meta = {"repo": repo_name, "prs_fetched": len(nodes), "truncated_at_max_prs": truncated,
            "api_calls": client.calls, "api_points": client.points}
    return nodes, meta


def _complete_reviews(client: GitHubClient, owner: str, name: str, node: dict) -> None:
    """Fetch the rest of a PR's reviews when it has more than one page (rare)."""
    reviews = node.get("reviews") or {}
    info = reviews.get("pageInfo") or {}
    pages = 0
    while info.get("hasNextPage") and pages < 20:
        data = client.query(MORE_REVIEWS_QUERY, {"owner": owner, "name": name,
                                                 "number": node["number"], "after": info.get("endCursor")})
        more = (((data.get("repository") or {}).get("pullRequest") or {}).get("reviews")) or {}
        reviews.setdefault("nodes", []).extend(more.get("nodes") or [])
        info = more.get("pageInfo") or {}
        pages += 1
    reviews["pageInfo"] = {"hasNextPage": False, "endCursor": None}
    node["reviews"] = reviews
