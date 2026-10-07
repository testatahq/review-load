"""Render the computed metrics as Markdown, JSON and a terminal chart."""

from __future__ import annotations

import copy
import json
from typing import Dict, List, Optional

from . import CTA_LINE
from .timeutil import format_hours


def pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def bar(value: float, maximum: float, width: int, block: str = "█") -> str:
    if maximum <= 0 or value <= 0:
        return ""
    return block * max(1, round(width * value / maximum))


# ---------------------------------------------------------------------------
# Anonymizing


def anonymize(report: dict) -> dict:
    """Replace people's logins with dev-01, dev-02, ... (busiest reviewer first).

    Bots keep their names. The same person gets the same alias everywhere in the report.
    """
    out = copy.deepcopy(report)
    aliases: Dict[str, str] = {}

    def alias(login: str) -> str:
        key = login.lower()
        if key not in aliases:
            aliases[key] = f"dev-{len(aliases) + 1:02d}"
        return aliases[key]

    for r in out["review_load"]["reviewers"]:
        r["login"] = alias(r["login"])
    for row in out["prs"]:
        row["author"] = alias(row["author"])
    out["anonymized"] = True
    return out


# ---------------------------------------------------------------------------
# Headline


def headline(report: dict) -> List[str]:
    load = report["review_load"]
    fr = report["first_review"]
    svw = report["size_vs_wait"]
    lines = []
    n = load["reviewers_count"]
    if not load["total_reviews"]:
        lines.append("No human reviews in this window.")
    else:
        if n <= 3:
            people = "1 person" if n == 1 else f"{n} people"
            lines.append(f"**{people} did all {load['total_reviews']} reviews. "
                         f"Review bus factor: {load['bus_factor']}.**")
        else:
            lines.append(f"**3 people did {pct(load['top3_share'])} of the reviews. "
                         f"Review bus factor: {load['bus_factor']}.**")
    if fr["reviewed"]:
        over = fr["over_24h"]
        text = (f"Median wait for a first human review: {format_hours(fr['median_h'])}, "
                f"p90 {format_hours(fr['p90_h'])} ({plural(fr['reviewed'], 'reviewed PR')}).")
        if over["share"] is not None:
            text += (f" {pct(over['share'])} of PRs ({over['count']} of {over['eligible']}) "
                     "waited more than 24 h.")
        lines.append(text)
    if svw["small_n"] >= 3 and svw["large_n"] >= 3:
        lines.append(f"Large PRs (401+ lines) waited a median {format_hours(svw['large_median_h'])} "
                     f"for a first review; small ones (up to 100 lines) {format_hours(svw['small_median_h'])}.")
    return lines


# ---------------------------------------------------------------------------
# Markdown


def to_markdown(report: dict, top: int = 8) -> str:
    meta = report["meta"]
    load = report["review_load"]
    fr = report["first_review"]
    bvh = report["bot_vs_human"]
    counts = report["counts"]
    w = meta["window"]

    out = [f"# Review load: {meta['repo']}, last {w['days']} days", ""]
    out.append(f"{w['since'][:10]} to {w['until'][:10]} (UTC) · "
               f"{counts['prs_ready_in_window']} PRs opened for review · "
               f"{load['total_reviews']} reviews by {load['reviewers_count']} people")
    out.append("")
    out.append(" ".join(headline(report)))
    out.append("")

    # Who reviews
    out += ["## Who carries review", ""]
    if load["reviewers"]:
        out += ["| Reviewer | Reviews | Share | |", "|---|--:|--:|---|"]
        shown = load["reviewers"][:top]
        peak = shown[0]["reviews"]
        for r in shown:
            out.append(f"| {r['login']} | {r['reviews']} | {pct(r['share'])} | {bar(r['reviews'], peak, 20)} |")
        rest = load["reviewers"][top:]
        if rest:
            n = sum(r["reviews"] for r in rest)
            out.append(f"| {len(rest)} others | {n} | {pct(n / load['total_reviews'])} | |")
        out.append("")
        out.append(f"Top 3 share: {pct(load['top3_share'])} · Bus factor (fewest people doing half "
                   f"the reviews): {load['bus_factor']} · {load['reviewers_count']} reviewers, "
                   f"{counts['human_authors']} PR authors")
    else:
        out.append("No human reviews in this window.")
    out.append("")

    # Wait
    out += ["## Time to first human review", "", "| | |", "|---|--:|"]
    out.append(f"| Median | {format_hours(fr['median_h'])} |")
    out.append(f"| p90 | {format_hours(fr['p90_h'])} |")
    over = fr["over_24h"]
    out.append(f"| Waited more than 24 h | {pct(over['share'])} ({over['count']} of {over['eligible']}) |")
    sw = fr["still_waiting"]
    oldest = ""
    if sw["oldest"]:
        o = sw["oldest"][0]
        oldest = f", oldest [#{o['number']}]({o['url']}) at {format_hours(o['wait_h'])}"
    out.append(f"| Open now with no human review | {sw['count']}{oldest} |")
    out.append(f"| Merged / closed with no human review | {fr['merged_without_human_review']} / "
               f"{fr['closed_without_human_review']} |")
    out.append("")

    # Size
    out += ["## PR size and wait", "",
            "| Size (lines changed) | PRs | Share | Median wait | Waited >24 h |",
            "|---|--:|--:|--:|--:|"]
    for s in report["size"]:
        out.append(f"| {s['size']} ({s['lines']}) | {s['prs']} | {pct(s['share'])} | "
                   f"{format_hours(s['median_wait_h'])} | {pct(s['over_24h_share'])} |")
    out.append("")

    # Bots
    out += ["## Bot vs human reviews", "", "| | Reviews | Reviewers |", "|---|--:|--:|"]
    out.append(f"| Human | {bvh['human_reviews']} | {bvh['human_reviewers']} |")
    out.append(f"| Bot | {bvh['bot_reviews']} | {len(bvh['bots'])} |")
    out.append("")
    reviewed = (f"PRs reviewed in the window: {bvh['prs_with_human_review']} by a human, "
                f"{bvh['prs_with_bot_review']} by a bot.")
    if bvh["bots"]:
        names = ", ".join(f"{b['login']} ({plural(b['reviews'], 'PR')})" for b in bvh["bots"][:5])
        reviewed += f" Bots: {names}."
    out.append(reviewed)
    out.append("")

    out.append(
        "<sub>A review is one person reviewing one PR in the window (approve, request changes or "
        "comment); the PR author's own replies and bots are not counted as human reviews. The wait "
        "clock starts when a PR is opened or marked ready for review. "
        f"{_skipped(counts)}"
        f"Generated {meta['generated_at'][:16].replace('T', ' ')} UTC by review-load {meta['version']}"
        f"{', logins anonymized' if report.get('anonymized') else ''}.</sub>")
    out.append("")
    out.append("---")
    out.append("")
    out.append(CTA_LINE)
    return "\n".join(out) + "\n"


def _skipped(counts: dict) -> str:
    if counts["bot_authored_prs_skipped"]:
        return (f"Bot-authored PRs ({counts['bot_authored_prs_skipped']}) and open drafts are left "
                "out of wait and size. ")
    return "Open drafts are left out of wait and size. "


# ---------------------------------------------------------------------------
# JSON


def to_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=False) + "\n"


# ---------------------------------------------------------------------------
# Terminal


def to_terminal(report: dict, top: int = 10, unicode: bool = True, width: int = 30) -> str:
    block = "█" if unicode else "#"
    arrow = "→" if unicode else "->"
    dot = "·" if unicode else "|"
    meta = report["meta"]
    w = meta["window"]
    load = report["review_load"]
    fr = report["first_review"]
    bvh = report["bot_vs_human"]

    lines = [f"review-load {dot} {meta['repo']} {dot} last {w['days']} days "
             f"({w['since'][:10]} {arrow} {w['until'][:10]} UTC)", ""]
    lines.append(f"Who carries review ({load['total_reviews']} reviews by {load['reviewers_count']} people)")
    if load["reviewers"]:
        shown = load["reviewers"][:top]
        name_w = max(len(r["login"]) for r in shown)
        peak = shown[0]["reviews"]
        for r in shown:
            lines.append(f"  {r['login']:<{name_w}}  {bar(r['reviews'], peak, width, block):<{width}}  "
                         f"{r['reviews']:>4}  {pct(r['share']):>4}")
        rest = load["reviewers"][top:]
        if rest:
            lines.append(f"  ... {len(rest)} more, {sum(r['reviews'] for r in rest)} reviews")
        lines.append(f"  top 3: {pct(load['top3_share'])} {dot} bus factor: {load['bus_factor']}")
    else:
        lines.append("  (no human reviews)")
    lines.append("")

    over = fr["over_24h"]
    lines.append(f"Time to first human review ({fr['reviewed']} reviewed PRs)")
    lines.append(f"  median {format_hours(fr['median_h'])} {dot} p90 {format_hours(fr['p90_h'])} {dot} "
                 f"waited >24 h: {pct(over['share'])} {dot} open with no review: {fr['still_waiting']['count']}")
    lines.append("")

    lines.append(f"PR size {arrow} median wait for a first review")
    sizes = report["size"]
    peak = max((s["prs"] for s in sizes), default=0)
    for s in sizes:
        label = f"{s['size']:<2} {s['lines']:>8}"
        lines.append(f"  {label}  {bar(s['prs'], peak, 20, block):<20}  {s['prs']:>4} PRs  "
                     f"{format_hours(s['median_wait_h']):>8}")
    lines.append("")
    lines.append(f"Reviews: {bvh['human_reviews']} human, {bvh['bot_reviews']} bot "
                 f"({plural(len(bvh['bots']), 'bot')} on {plural(bvh['prs_with_bot_review'], 'PR')})")
    return "\n".join(lines) + "\n"
