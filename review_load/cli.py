"""Command line: review-load OWNER/REPO [--days 30] [--out DIR]."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from . import CTA_LINE, __version__
from .github import DEFAULT_API_URL, GitHubClient, GitHubError, fetch_pull_requests, parse_repo, resolve_token
from .metrics import compute
from .model import pr_from_node
from .report import anonymize, to_json, to_markdown, to_terminal
from .timeutil import iso

TOKEN_HELP = """\
No GitHub token found. Do one of these:
  * export GITHUB_TOKEN=...   (a fine-grained token with Pull requests: Read-only on the repo)
  * gh auth login             (review-load then uses `gh auth token`)
Public repos work with any token; the tool only reads."""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="review-load",
        description="Who is carrying code review on one GitHub repo over the last N days.")
    p.add_argument("repo", help="OWNER/REPO or a github.com URL, e.g. cli/cli")
    p.add_argument("--days", type=int, default=30, help="window length in days (default 30)")
    p.add_argument("--out", default="review-load-report",
                   help="folder for review-load.md and review-load.json (default ./review-load-report)")
    p.add_argument("--anonymize", action="store_true",
                   help="replace people's logins with dev-01, dev-02, ... (bots keep their names)")
    p.add_argument("--bots", default="",
                   help="extra logins to treat as bots, comma separated (GitHub Apps are detected already)")
    p.add_argument("--include-bot-prs", action="store_true",
                   help="also count PRs opened by bots (Dependabot, coding agents) in wait and size")
    p.add_argument("--top", type=int, default=8, help="reviewers listed by name in the report (default 8)")
    p.add_argument("--max-prs", type=int, default=5000, help="stop after this many PRs (default 5000)")
    p.add_argument("--page-size", type=int, default=50, help="PRs per API call (default 50)")
    p.add_argument("--api-url", default=os.environ.get("GITHUB_GRAPHQL_URL") or DEFAULT_API_URL,
                   help="GraphQL endpoint; set for GitHub Enterprise Server")
    p.add_argument("--quiet", action="store_true", help="print only the output file paths")
    p.add_argument("--version", action="version", version=f"review-load {__version__}")
    return p


def _supports_unicode(stream) -> bool:
    enc = (getattr(stream, "encoding", None) or "").lower()
    return "utf" in enc


def main(argv: Optional[List[str]] = None, *, transport=None, now: Optional[datetime] = None,
         env: Optional[dict] = None, run=None, sleep: Optional[Callable[[float], None]] = None,
         stdout=None, stderr=None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, file=stderr)

    try:
        owner, name = parse_repo(args.repo)
    except ValueError as exc:
        print(f"review-load: {exc}", file=stderr)
        return 2
    if not 1 <= args.days <= 365:
        print("review-load: --days must be between 1 and 365", file=stderr)
        return 2

    token_kwargs = {"env": env}
    if run is not None:
        token_kwargs["run"] = run
    token, source = resolve_token(**token_kwargs)
    if not token:
        print(TOKEN_HELP, file=stderr)
        return 2

    until = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    since = until - timedelta(days=args.days)
    client_kwargs = {"api_url": args.api_url, "log": log}
    if transport is not None:
        client_kwargs["transport"] = transport
    if sleep is not None:
        client_kwargs["sleep"] = sleep
    client = GitHubClient(token, **client_kwargs)

    log(f"Reading pull requests for {owner}/{name} updated since {since:%Y-%m-%d} (token from {source})")
    try:
        nodes, fetch = fetch_pull_requests(client, owner, name, since,
                                           page_size=max(10, min(100, args.page_size)),
                                           max_prs=args.max_prs)
    except GitHubError as exc:
        print(f"review-load: {exc}", file=stderr)
        return 1

    extra_bots = [b for b in args.bots.split(",") if b.strip()]
    prs = [pr_from_node(n, extra_bots) for n in nodes]
    report = compute(prs, since, until, include_bot_prs=args.include_bot_prs)
    report = {
        "meta": {
            "tool": "review-load",
            "version": __version__,
            "repo": fetch["repo"],
            "generated_at": iso(until),
            "window": {"days": args.days, "since": iso(since), "until": iso(until)},
            "api": {"calls": fetch["api_calls"], "points": fetch["api_points"]},
            "truncated_at_max_prs": fetch["truncated_at_max_prs"],
        },
        **report,
    }
    if args.anonymize:
        report = anonymize(report)
    if fetch["truncated_at_max_prs"]:
        log(f"  warning: stopped at --max-prs {args.max_prs}; the oldest part of the window is missing")

    os.makedirs(args.out, exist_ok=True)
    md_path = os.path.join(args.out, "review-load.md")
    json_path = os.path.join(args.out, "review-load.json")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(to_markdown(report, top=max(1, args.top)))
    with open(json_path, "w", encoding="utf-8") as fh:
        fh.write(to_json(report))

    if not args.quiet:
        print("", file=stdout)
        print(to_terminal(report, unicode=_supports_unicode(stdout)), file=stdout)
        print(f"Wrote {md_path} and {json_path} ({fetch['api_calls']} API calls, "
              f"{fetch['api_points']} rate-limit points)", file=stdout)
        print("", file=stdout)
        print(CTA_LINE, file=stdout)
    else:
        print(md_path, file=stdout)
        print(json_path, file=stdout)
    return 0
