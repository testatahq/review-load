"""Shared test helpers: a fake transport that replays recorded responses, and node builders."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from review_load.github import HttpResponse

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture_text(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


class FakeTransport:
    """Replays queued HttpResponses in order and records what was sent. No network."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.sent = []

    def __call__(self, url, payload, headers):
        self.sent.append({"url": url, "payload": payload, "headers": headers})
        if not self.responses:
            raise AssertionError("unexpected extra request: " + json.dumps(payload["variables"]))
        return self.responses.pop(0)


def ok(body, headers=None) -> HttpResponse:
    text = body if isinstance(body, str) else json.dumps(body)
    return HttpResponse(200, headers or {}, text)


def actor(login, bot=False):
    if login is None:
        return None
    return {"login": login, "__typename": "Bot" if bot else "User"}


def review(login, at, state="APPROVED", bot=False):
    return {"author": actor(login, bot), "state": state, "submittedAt": at}


def pr_node(number, author="alice", created="2026-09-10T10:00:00Z", updated=None, state="OPEN",
            draft=False, additions=10, deletions=0, reviews=(), ready=None, converted=None,
            author_bot=False, merged=None, closed=None):
    events = []
    if ready:
        events.append({"__typename": "ReadyForReviewEvent", "createdAt": ready})
    elif converted:
        events.append({"__typename": "ConvertToDraftEvent", "createdAt": converted})
    return {
        "number": number,
        "url": f"https://github.com/acme/app/pull/{number}",
        "state": state,
        "isDraft": draft,
        "createdAt": created,
        "updatedAt": updated or created,
        "closedAt": closed,
        "mergedAt": merged,
        "additions": additions,
        "deletions": deletions,
        "author": actor(author, author_bot),
        "timelineItems": {"nodes": events},
        "reviews": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": list(reviews)},
    }


def page(nodes, has_next=False, cursor=None, remaining=4990, reset="2026-10-01T13:00:00Z"):
    return {"data": {
        "rateLimit": {"cost": 1, "remaining": remaining, "resetAt": reset},
        "repository": {"nameWithOwner": "acme/app", "pullRequests": {
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": nodes}},
    }}
