"""Pure calculations. No I/O; everything here is covered by tests.

Definitions (also in the README):

* A **review** is one person submitting at least one review (approve, request changes,
  comment, or a review later dismissed) on one PR inside the window. Ten comment rounds by
  the same person on the same PR count once, so thread replies do not inflate the load.
  Reviews by the PR's own author, by bots and by deleted accounts are not human reviews.
* The **review clock** starts when a PR is opened, or when it is marked ready for review if
  it was opened as a draft, and stops at the first human review.
* **Wait metrics** cover PRs that became ready for review inside the window, were written by
  a person (not a bot), and are not drafts now.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from .model import GHOST, PullRequest, Review
from .timeutil import hours_between, iso

COUNTED_STATES = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"}
WAIT_THRESHOLD_H = 24.0

# (label, lowest lines, highest lines or None). 400 lines is where reviewer attention is
# commonly reported to drop off, so M ends there.
SIZE_BUCKETS = (
    ("XS", 0, 10),
    ("S", 11, 100),
    ("M", 101, 400),
    ("L", 401, 1000),
    ("XL", 1001, None),
)


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    """Linear-interpolated percentile (pct 0-100), same method as numpy's default."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def median(values: Sequence[float]) -> Optional[float]:
    return float(statistics.median(values)) if values else None


def size_bucket(lines: int) -> str:
    for label, _lo, hi in SIZE_BUCKETS:
        if hi is None or lines <= hi:
            return label
    return SIZE_BUCKETS[-1][0]  # pragma: no cover


def bucket_range(label: str) -> str:
    for name, lo, hi in SIZE_BUCKETS:
        if name == label:
            return f"{lo}-{hi}" if hi is not None else f"{lo}+"
    raise KeyError(label)


def bus_factor(counts: Sequence[int]) -> Optional[int]:
    """Fewest people who together did at least half of all reviews."""
    total = sum(counts)
    if not total:
        return None
    running = 0
    for i, c in enumerate(sorted(counts, reverse=True), start=1):
        running += c
        if running * 2 >= total:
            return i
    return len(counts)  # pragma: no cover


def _counted(review: Review) -> bool:
    return review.submitted_at is not None and review.state in COUNTED_STATES


def _is_human_review(review: Review, pr: PullRequest) -> bool:
    return (_counted(review) and not review.is_bot and review.login != GHOST
            and review.login.lower() != pr.author.lower())


def _first_human_review(pr: PullRequest) -> Optional[datetime]:
    times = [r.submitted_at for r in pr.reviews if _is_human_review(r, pr)]
    return min(times) if times else None


def compute(prs: List[PullRequest], since: datetime, until: datetime,
            include_bot_prs: bool = False) -> dict:
    """Every metric for one repo and one window [since, until].

    Bot-authored PRs (dependency bumps, coding agents that open PRs under a bot account) are
    left out of wait and size unless `include_bot_prs`; reviews on them always count as load.
    """
    in_window = lambda ts: ts is not None and since <= ts <= until  # noqa: E731

    # --- Review load -------------------------------------------------------
    human_prs: Dict[str, set] = defaultdict(set)
    human_subs: Counter = Counter()
    bot_prs: Dict[str, set] = defaultdict(set)
    bot_subs: Counter = Counter()
    prs_with_human, prs_with_bot = set(), set()
    for pr in prs:
        for r in pr.reviews:
            if not _counted(r) or not in_window(r.submitted_at):
                continue
            if r.is_bot:
                bot_prs[r.login].add(pr.number)
                bot_subs[r.login] += 1
                prs_with_bot.add(pr.number)
            elif _is_human_review(r, pr):
                human_prs[r.login].add(pr.number)
                human_subs[r.login] += 1
                prs_with_human.add(pr.number)

    ranked = sorted(((login, len(nums)) for login, nums in human_prs.items()),
                    key=lambda item: (-item[1], item[0].lower()))
    total_reviews = sum(c for _, c in ranked)
    reviewers = [
        {"login": login, "reviews": c, "share": c / total_reviews,
         "submissions": human_subs[login]}
        for login, c in ranked
    ]
    top3 = sum(c for _, c in ranked[:3])
    load = {
        "total_reviews": total_reviews,
        "total_submissions": sum(human_subs.values()),
        "reviewers_count": len(ranked),
        "top3_share": (top3 / total_reviews) if total_reviews else None,
        "bus_factor": bus_factor([c for _, c in ranked]),
        "reviewers": reviewers,
    }

    # --- Time to first human review ---------------------------------------
    population = [p for p in prs
                  if (include_bot_prs or not p.author_is_bot)
                  and p.ready_at is not None and in_window(p.ready_at)]
    rows = []
    for pr in population:
        first = _first_human_review(pr)
        if first is not None:
            status, wait = "reviewed", max(0.0, hours_between(pr.ready_at, first))
        elif pr.state == "OPEN":
            status, wait = "waiting", max(0.0, hours_between(pr.ready_at, until))
        elif pr.state == "MERGED":
            status, wait = "merged_unreviewed", None
        else:
            status, wait = "closed_unreviewed", None
        eligible = (status in ("reviewed", "waiting")
                    and hours_between(pr.ready_at, until) >= WAIT_THRESHOLD_H)
        rows.append({
            "number": pr.number,
            "url": pr.url,
            "author": pr.author,
            "state": pr.state,
            "ready_at": iso(pr.ready_at),
            "first_human_review_at": iso(first),
            "status": status,
            "wait_h": None if wait is None else round(wait, 2),
            "lines_changed": pr.lines_changed,
            "size": size_bucket(pr.lines_changed),
            "over_24h": (wait is not None and wait > WAIT_THRESHOLD_H) if eligible else None,
        })

    reviewed_waits = [r["wait_h"] for r in rows if r["status"] == "reviewed"]
    eligible_rows = [r for r in rows if r["over_24h"] is not None]
    over = sum(1 for r in eligible_rows if r["over_24h"])
    waiting = sorted((r for r in rows if r["status"] == "waiting"), key=lambda r: -r["wait_h"])
    first_review = {
        "prs_ready_in_window": len(rows),
        "reviewed": len(reviewed_waits),
        "median_h": median(reviewed_waits),
        "p90_h": percentile(reviewed_waits, 90),
        "over_24h": {"count": over, "eligible": len(eligible_rows),
                     "share": (over / len(eligible_rows)) if eligible_rows else None},
        "still_waiting": {"count": len(waiting),
                          "oldest": [{"number": r["number"], "url": r["url"], "wait_h": r["wait_h"]}
                                     for r in waiting[:3]]},
        "merged_without_human_review": sum(1 for r in rows if r["status"] == "merged_unreviewed"),
        "closed_without_human_review": sum(1 for r in rows if r["status"] == "closed_unreviewed"),
    }

    # --- Size and wait ----------------------------------------------------
    sizes = []
    for label, _lo, _hi in SIZE_BUCKETS:
        bucket = [r for r in rows if r["size"] == label]
        waits = [r["wait_h"] for r in bucket if r["status"] == "reviewed"]
        elig = [r for r in bucket if r["over_24h"] is not None]
        sizes.append({
            "size": label,
            "lines": bucket_range(label),
            "prs": len(bucket),
            "share": (len(bucket) / len(rows)) if rows else None,
            "reviewed": len(waits),
            "median_wait_h": median(waits),
            "over_24h_share": (sum(1 for r in elig if r["over_24h"]) / len(elig)) if elig else None,
        })
    small = [r["wait_h"] for r in rows if r["status"] == "reviewed" and r["size"] in ("XS", "S")]
    large = [r["wait_h"] for r in rows if r["status"] == "reviewed" and r["size"] in ("L", "XL")]
    size_vs_wait = {
        "small_median_h": median(small), "small_n": len(small),
        "large_median_h": median(large), "large_n": len(large),
    }

    # --- Bots vs humans ---------------------------------------------------
    bots = sorted(((login, len(nums), bot_subs[login]) for login, nums in bot_prs.items()),
                  key=lambda item: (-item[1], item[0].lower()))
    bot_vs_human = {
        "human_reviews": total_reviews,
        "human_reviewers": len(ranked),
        "bot_reviews": sum(n for _, n, _ in bots),
        "bot_submissions": sum(s for _, _, s in bots),
        "bots": [{"login": login, "reviews": n, "submissions": s} for login, n, s in bots],
        "prs_with_human_review": len(prs_with_human),
        "prs_with_bot_review": len(prs_with_bot),
        "prs_with_review": len(prs_with_human | prs_with_bot),
    }

    counts = {
        "prs_updated_in_window": len(prs),
        "prs_ready_in_window": len(rows),
        "human_authors": len({p.author for p in population if not p.author_is_bot}),
        "bot_authored_prs_skipped": 0 if include_bot_prs else sum(
            1 for p in prs if p.author_is_bot and p.ready_at and in_window(p.ready_at)),
        "open_drafts_skipped": sum(1 for p in prs if p.is_draft and in_window(p.created_at)),
    }

    return {
        "counts": counts,
        "review_load": load,
        "first_review": first_review,
        "size": sizes,
        "size_vs_wait": size_vs_wait,
        "bot_vs_human": bot_vs_human,
        "prs": rows,
    }
