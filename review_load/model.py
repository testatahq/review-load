"""Turn raw GraphQL pull request nodes into small records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Optional

from .timeutil import parse_ts

GHOST = "ghost"  # GitHub's placeholder for deleted accounts


@dataclass
class Review:
    login: str
    is_bot: bool
    state: str
    submitted_at: Optional[datetime]


@dataclass
class PullRequest:
    number: int
    url: str
    state: str                       # OPEN, CLOSED or MERGED
    is_draft: bool
    author: str
    author_is_bot: bool
    created_at: datetime
    updated_at: datetime
    ready_at: Optional[datetime]     # when it was first ready for review; None while a draft
    closed_at: Optional[datetime]
    merged_at: Optional[datetime]
    additions: int
    deletions: int
    reviews: List[Review] = field(default_factory=list)

    @property
    def lines_changed(self) -> int:
        return self.additions + self.deletions


def _actor(actor: Optional[dict], extra_bots: "set[str]") -> "tuple[str, bool]":
    if not actor or not actor.get("login"):
        return GHOST, False
    login = actor["login"]
    suffixed = login.lower().endswith("[bot]")
    if suffixed:
        login = login[:-5]
    is_bot = actor.get("__typename") == "Bot" or suffixed or login.lower() in extra_bots
    return login, is_bot


def _ready_at(node: dict, created_at: datetime) -> Optional[datetime]:
    """A PR opened as a draft starts its review clock at its first ReadyForReviewEvent.

    We fetch the first draft-related timeline event. If it is a ReadyForReviewEvent the PR
    was opened as a draft; if it is a ConvertToDraftEvent (or there is none) the PR was
    opened ready for review, so the clock starts at creation.
    """
    if node.get("isDraft"):
        return None
    events = ((node.get("timelineItems") or {}).get("nodes")) or []
    first = events[0] if events else None
    if first and first.get("__typename") == "ReadyForReviewEvent":
        return parse_ts(first.get("createdAt")) or created_at
    return created_at


def pr_from_node(node: dict, extra_bots: Iterable[str] = ()) -> PullRequest:
    bots = {b.strip().lower() for b in extra_bots if b and b.strip()}
    author, author_is_bot = _actor(node.get("author"), bots)
    created = parse_ts(node["createdAt"])
    reviews = []
    for r in ((node.get("reviews") or {}).get("nodes")) or []:
        if not r:
            continue
        login, is_bot = _actor(r.get("author"), bots)
        reviews.append(Review(login, is_bot, r.get("state") or "", parse_ts(r.get("submittedAt"))))
    return PullRequest(
        number=int(node["number"]),
        url=node.get("url") or "",
        state=node.get("state") or "OPEN",
        is_draft=bool(node.get("isDraft")),
        author=author,
        author_is_bot=author_is_bot,
        created_at=created,
        updated_at=parse_ts(node.get("updatedAt")) or created,
        ready_at=_ready_at(node, created),
        closed_at=parse_ts(node.get("closedAt")),
        merged_at=parse_ts(node.get("mergedAt")),
        additions=int(node.get("additions") or 0),
        deletions=int(node.get("deletions") or 0),
        reviews=reviews,
    )
