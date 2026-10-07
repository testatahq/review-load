import json
import unittest

from review_load.metrics import bus_factor, compute, percentile, size_bucket
from review_load.model import pr_from_node

from .helpers import fixture_text, pr_node, review, ts

SINCE = ts("2026-09-01T00:00:00Z")
UNTIL = ts("2026-10-01T00:00:00Z")


def run(nodes, bots=(), include_bot_prs=False, since=SINCE, until=UNTIL):
    return compute([pr_from_node(n, bots) for n in nodes], since, until, include_bot_prs=include_bot_prs)


class HelperTests(unittest.TestCase):
    def test_percentile(self):
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2.5)
        self.assertAlmostEqual(percentile([1, 2, 3, 4], 90), 3.7)
        self.assertEqual(percentile([5], 90), 5.0)
        self.assertIsNone(percentile([], 90))

    def test_size_bucket_edges(self):
        cases = {0: "XS", 10: "XS", 11: "S", 100: "S", 101: "M", 400: "M", 401: "L", 1000: "L", 1001: "XL"}
        for lines, label in cases.items():
            self.assertEqual(size_bucket(lines), label, lines)

    def test_bus_factor(self):
        self.assertEqual(bus_factor([5, 3, 2]), 1)
        self.assertEqual(bus_factor([3, 3, 3, 1]), 2)
        self.assertEqual(bus_factor([1, 1, 1, 1]), 2)
        self.assertIsNone(bus_factor([]))


class ModelTests(unittest.TestCase):
    def test_ready_at(self):
        opened_ready = pr_from_node(pr_node(1, created="2026-09-10T10:00:00Z"))
        self.assertEqual(opened_ready.ready_at, ts("2026-09-10T10:00:00Z"))
        opened_draft = pr_from_node(pr_node(2, created="2026-09-10T10:00:00Z", ready="2026-09-12T09:00:00Z"))
        self.assertEqual(opened_draft.ready_at, ts("2026-09-12T09:00:00Z"))
        # Opened ready, later converted to draft and back: the clock started at creation.
        flipped = pr_from_node(pr_node(3, created="2026-09-10T10:00:00Z", converted="2026-09-11T10:00:00Z"))
        self.assertEqual(flipped.ready_at, ts("2026-09-10T10:00:00Z"))
        self.assertIsNone(pr_from_node(pr_node(4, draft=True)).ready_at)

    def test_bots_and_ghosts(self):
        node = pr_node(1, author=None, reviews=[review("renovate[bot]", "2026-09-11T00:00:00Z"),
                                                review("ci-user", "2026-09-11T00:00:00Z")])
        pr = pr_from_node(node, extra_bots=["CI-User"])
        self.assertEqual(pr.author, "ghost")
        self.assertEqual([(r.login, r.is_bot) for r in pr.reviews], [("renovate", True), ("ci-user", True)])


class ReviewLoadTests(unittest.TestCase):
    def test_counts_people_per_pr_not_comment_rounds(self):
        nodes = [
            pr_node(1, author="alice", reviews=[
                review("bob", "2026-09-10T11:00:00Z", "COMMENTED"),
                review("bob", "2026-09-10T12:00:00Z", "COMMENTED"),
                review("bob", "2026-09-10T13:00:00Z", "APPROVED"),
                review("alice", "2026-09-10T12:30:00Z", "COMMENTED"),   # author replying: not a review
                review(None, "2026-09-10T12:30:00Z"),                    # deleted account
                review("carol", None, "PENDING"),                        # unsubmitted draft review
            ]),
            pr_node(2, author="bob", reviews=[review("alice", "2026-09-11T10:00:00Z")]),
            pr_node(3, author="alice", reviews=[review("bob", "2026-08-20T10:00:00Z")]),  # before window
        ]
        load = run(nodes)["review_load"]
        self.assertEqual([(r["login"], r["reviews"], r["submissions"]) for r in load["reviewers"]],
                         [("alice", 1, 1), ("bob", 1, 3)])
        self.assertEqual(load["total_reviews"], 2)
        self.assertEqual(load["bus_factor"], 1)

    def test_concentration(self):
        reviews = {"ann": 6, "ben": 2, "cat": 1, "dan": 1}
        nodes, n = [], 0
        for login, count in reviews.items():
            for _ in range(count):
                n += 1
                nodes.append(pr_node(n, author="zed", reviews=[review(login, "2026-09-15T10:00:00Z")]))
        load = run(nodes)["review_load"]
        self.assertEqual([r["login"] for r in load["reviewers"]], ["ann", "ben", "cat", "dan"])
        self.assertAlmostEqual(load["top3_share"], 0.9)
        self.assertEqual(load["bus_factor"], 1)
        self.assertAlmostEqual(load["reviewers"][0]["share"], 0.6)

    def test_bots_counted_separately(self):
        nodes = [pr_node(1, reviews=[review("copilot-pull-request-reviewer", "2026-09-10T10:05:00Z", "COMMENTED", bot=True),
                                     review("copilot-pull-request-reviewer", "2026-09-10T10:06:00Z", "COMMENTED", bot=True),
                                     review("ci-user", "2026-09-10T10:07:00Z", "COMMENTED"),
                                     review("bob", "2026-09-10T15:00:00Z")])]
        r = run(nodes, bots=["ci-user"])
        bvh = r["bot_vs_human"]
        self.assertEqual((bvh["human_reviews"], bvh["bot_reviews"], bvh["bot_submissions"]), (1, 2, 3))
        self.assertEqual([b["login"] for b in bvh["bots"]], ["ci-user", "copilot-pull-request-reviewer"])
        # The bot's earlier review does not stop the human-review clock.
        self.assertEqual(r["first_review"]["median_h"], 5.0)


class WaitTests(unittest.TestCase):
    def test_wait_statuses_and_24h_share(self):
        nodes = [
            pr_node(1, created="2026-09-10T10:00:00Z", reviews=[review("bob", "2026-09-10T12:00:00Z")]),       # 2 h
            pr_node(2, created="2026-09-10T10:00:00Z", reviews=[review("bob", "2026-09-12T10:00:00Z")]),       # 48 h
            pr_node(3, created="2026-09-20T00:00:00Z"),                                                         # waiting 11 d
            pr_node(4, created="2026-09-30T12:00:00Z"),                                                         # waiting 12 h: too young
            pr_node(5, created="2026-09-15T00:00:00Z", state="MERGED", merged="2026-09-15T01:00:00Z"),          # merged, no review
            pr_node(6, created="2026-09-15T00:00:00Z", state="CLOSED", closed="2026-09-16T00:00:00Z"),          # closed, no review
            pr_node(7, created="2026-09-05T00:00:00Z", ready="2026-09-08T00:00:00Z",                            # draft review: 0 h
                    reviews=[review("bob", "2026-09-06T00:00:00Z", "COMMENTED")]),
            pr_node(8, created="2026-08-01T00:00:00Z", reviews=[review("bob", "2026-09-02T00:00:00Z")]),       # ready before window
            pr_node(9, draft=True, created="2026-09-20T00:00:00Z"),                                             # draft now
            pr_node(10, author="dependabot", author_bot=True, created="2026-09-20T00:00:00Z"),                 # bot-authored
        ]
        r = run(nodes)
        fr = r["first_review"]
        self.assertEqual(fr["prs_ready_in_window"], 7)
        self.assertEqual(fr["reviewed"], 3)
        self.assertEqual(fr["median_h"], 2.0)
        self.assertAlmostEqual(fr["p90_h"], 38.8)
        # Eligible: 1, 2, 3, 7 (4 is under 24 h old). Over 24 h: 2 and 3.
        self.assertEqual(fr["over_24h"], {"count": 2, "eligible": 4, "share": 0.5})
        self.assertEqual(fr["still_waiting"]["count"], 2)
        self.assertEqual(fr["still_waiting"]["oldest"][0]["number"], 3)
        self.assertEqual(fr["merged_without_human_review"], 1)
        self.assertEqual(fr["closed_without_human_review"], 1)
        self.assertEqual(r["counts"]["bot_authored_prs_skipped"], 1)
        self.assertEqual(r["counts"]["open_drafts_skipped"], 1)
        self.assertEqual(run(nodes, include_bot_prs=True)["first_review"]["prs_ready_in_window"], 8)

    def test_size_vs_wait(self):
        nodes = []
        for i in range(3):
            nodes.append(pr_node(10 + i, additions=20, created="2026-09-10T00:00:00Z",
                                 reviews=[review("bob", "2026-09-10T01:00:00Z")]))
            nodes.append(pr_node(20 + i, additions=900, created="2026-09-10T00:00:00Z",
                                 reviews=[review("bob", "2026-09-11T06:00:00Z")]))
        r = run(nodes)
        by = {s["size"]: s for s in r["size"]}
        self.assertEqual((by["S"]["prs"], by["S"]["median_wait_h"]), (3, 1.0))
        self.assertEqual((by["L"]["prs"], by["L"]["median_wait_h"], by["L"]["over_24h_share"]), (3, 30.0, 1.0))
        self.assertEqual(by["XS"]["prs"], 0)
        self.assertIsNone(by["XS"]["median_wait_h"])
        self.assertEqual(r["size_vs_wait"], {"small_median_h": 1.0, "small_n": 3,
                                             "large_median_h": 30.0, "large_n": 3})

    def test_empty_repo(self):
        r = run([])
        self.assertEqual(r["review_load"]["total_reviews"], 0)
        self.assertIsNone(r["review_load"]["bus_factor"])
        self.assertIsNone(r["first_review"]["median_h"])


class RecordedFixtureTests(unittest.TestCase):
    """Metrics over the two recorded cli/cli pages, checked by hand against the raw JSON."""

    def test_recorded(self):
        nodes = []
        for name in ("recorded_page1.json", "recorded_page2.json"):
            nodes += json.loads(fixture_text(name))["data"]["repository"]["pullRequests"]["nodes"]
        r = run(nodes, since=ts("2026-09-07T20:00:00Z"), until=ts("2026-10-07T20:00:00Z"))
        load = r["review_load"]
        self.assertEqual([(x["login"], x["reviews"], x["submissions"]) for x in load["reviewers"]],
                         [("user-f", 3, 6), ("user-c", 1, 1), ("user-d", 1, 1), ("user-e", 1, 1)])
        self.assertAlmostEqual(load["top3_share"], 5 / 6)
        self.assertEqual(load["bus_factor"], 1)
        bvh = r["bot_vs_human"]
        self.assertEqual(bvh["bots"], [{"login": "copilot-pull-request-reviewer", "reviews": 5, "submissions": 6}])
        self.assertEqual((bvh["prs_with_human_review"], bvh["prs_with_bot_review"], bvh["prs_with_review"]), (5, 5, 6))
        fr = r["first_review"]
        waits = {row["number"]: row["wait_h"] for row in r["prs"]}
        self.assertEqual(waits, {14578: 27.42, 14583: 60.02, 14565: 141.55, 14620: 5.62,
                                 14572: 147.01, 14564: 138.85})
        self.assertAlmostEqual(fr["median_h"], 99.435)
        self.assertAlmostEqual(fr["p90_h"], 140.74)
        self.assertEqual(fr["over_24h"], {"count": 5, "eligible": 5, "share": 1.0})
        self.assertEqual(fr["still_waiting"]["count"], 2)
        self.assertEqual(r["counts"]["bot_authored_prs_skipped"], 1)   # copilot-swe-agent's PR
        self.assertEqual(r["counts"]["human_authors"], 2)
        sizes = {s["size"]: s["prs"] for s in r["size"]}
        self.assertEqual(sizes, {"XS": 0, "S": 2, "M": 2, "L": 1, "XL": 1})


if __name__ == "__main__":
    unittest.main()
