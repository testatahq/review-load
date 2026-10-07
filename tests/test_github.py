import json
import subprocess
import unittest

from review_load.github import (MORE_REVIEWS_QUERY, PULLS_QUERY, GitHubClient, GitHubError, HttpResponse,
                                fetch_pull_requests, parse_repo, resolve_token)

from .helpers import FakeTransport, fixture_text, ok, page, pr_node, review, ts

TOKEN = "ghp_secret_test_token_value"


def client(responses, logs=None, clock=1_800_000_000.0):
    transport = FakeTransport(responses)
    sleeps = []
    c = GitHubClient(TOKEN, transport=transport, sleep=sleeps.append, clock=lambda: clock,
                     log=(logs.append if logs is not None else (lambda m: None)))
    return c, transport, sleeps


class TokenTests(unittest.TestCase):
    def test_github_token_env_wins(self):
        self.assertEqual(resolve_token({"GITHUB_TOKEN": " abc ", "GH_TOKEN": "def"}), ("abc", "GITHUB_TOKEN"))

    def test_gh_token_env(self):
        self.assertEqual(resolve_token({"GH_TOKEN": "def"}), ("def", "GH_TOKEN"))

    def test_falls_back_to_gh_cli(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="gho_fromcli\n", stderr="")

        self.assertEqual(resolve_token({}, run=run), ("gho_fromcli", "gh auth token"))
        self.assertEqual(calls, [["gh", "auth", "token"]])

    def test_gh_not_installed_or_logged_out(self):
        def missing(cmd, **kw):
            raise FileNotFoundError("gh")

        def logged_out(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not logged in")

        self.assertEqual(resolve_token({}, run=missing), (None, None))
        self.assertEqual(resolve_token({}, run=logged_out), (None, None))


class ParseRepoTests(unittest.TestCase):
    def test_forms(self):
        for text in ("cli/cli", "https://github.com/cli/cli", "https://github.com/cli/cli/",
                     "https://github.com/cli/cli.git", "git@github.com:cli/cli.git",
                     "github.com/cli/cli/pulls"):
            self.assertEqual(parse_repo(text), ("cli", "cli"), text)

    def test_rejects_garbage(self):
        for text in ("cli", "", "a b/c"):
            with self.assertRaises(ValueError):
                parse_repo(text)


class ClientTests(unittest.TestCase):
    def test_sends_token_only_in_header_and_never_logs_it(self):
        logs = []
        c, t, _ = client([ok(page([]))], logs=logs)
        c.query(PULLS_QUERY, {"owner": "acme", "name": "app", "first": 50, "after": None})
        self.assertEqual(t.sent[0]["headers"]["Authorization"], f"bearer {TOKEN}")
        self.assertNotIn(TOKEN, json.dumps(t.sent[0]["payload"]))
        self.assertFalse(any(TOKEN in m for m in logs))

    def test_only_queries_never_mutations(self):
        for q in (PULLS_QUERY, MORE_REVIEWS_QUERY):
            self.assertTrue(q.lstrip().startswith("query"))
            self.assertNotIn("mutation", q)

    def test_retry_after_is_respected(self):
        c, t, sleeps = client([HttpResponse(429, {"retry-after": "7"}, "slow down"), ok(page([]))])
        c.query(PULLS_QUERY, {})
        self.assertEqual(sleeps, [7.0])
        self.assertEqual(c.calls, 2)

    def test_primary_rate_limit_waits_for_reset_header(self):
        resp = HttpResponse(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000100"},
                            '{"message":"API rate limit exceeded"}')
        c, _, sleeps = client([resp, ok(page([]))])
        c.query(PULLS_QUERY, {})
        self.assertEqual(sleeps, [101.0])

    def test_graphql_rate_limited_error_retries(self):
        limited = ok({"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]})
        c, _, sleeps = client([limited, ok(page([]))])
        c.query(PULLS_QUERY, {})
        self.assertEqual(len(sleeps), 1)

    def test_low_remaining_points_pause_until_reset(self):
        low = page([], remaining=40, reset="2026-01-15T08:01:40Z")  # clock below is 08:00:00
        c, _, sleeps = client([ok(low)], clock=ts("2026-01-15T08:00:00Z").timestamp())
        c.query(PULLS_QUERY, {})
        self.assertEqual(sleeps, [101.0])
        self.assertEqual(c.remaining, 40)

    def test_refuses_to_sleep_for_hours(self):
        resp = HttpResponse(429, {"retry-after": "99999"}, "slow down")
        c, _, _ = client([resp])
        with self.assertRaises(GitHubError):
            c.query(PULLS_QUERY, {})

    def test_server_error_backs_off_then_succeeds(self):
        c, _, sleeps = client([HttpResponse(500, {}, "oops"), ok(page([]))])
        c.query(PULLS_QUERY, {})
        self.assertEqual(sleeps, [2.0])

    def test_bad_token(self):
        c, _, _ = client([HttpResponse(401, {}, '{"message":"Bad credentials"}')])
        with self.assertRaisesRegex(GitHubError, "401"):
            c.query(PULLS_QUERY, {})

    def test_forbidden_explains_sso(self):
        c, _, _ = client([HttpResponse(403, {}, '{"message":"Resource protected by SAML"}')])
        with self.assertRaisesRegex(GitHubError, "SAML"):
            c.query(PULLS_QUERY, {})

    def test_repo_not_found_explains_token(self):
        body = {"data": {"repository": None},
                "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}]}
        c, _, _ = client([ok(body)])
        with self.assertRaisesRegex(GitHubError, "fine-grained"):
            c.query(PULLS_QUERY, {})


class FetchTests(unittest.TestCase):
    def test_recorded_pages_stop_at_window_start(self):
        # Recorded from cli/cli (logins scrubbed). Page 2 says hasNextPage, but its last PR was
        # updated before `since`, so no third request is made.
        responses = [ok(fixture_text("recorded_page1.json")), ok(fixture_text("recorded_page2.json"))]
        c, t, _ = client(responses)
        nodes, meta = fetch_pull_requests(c, "cli", "cli", ts("2026-10-07T11:00:00Z"), page_size=4)
        self.assertEqual([n["number"] for n in nodes], [13013, 14578, 14583, 14565, 14620, 14572, 14564])
        self.assertEqual(meta["repo"], "cli/cli")
        self.assertEqual(meta["api_calls"], 2)
        self.assertEqual(meta["api_points"], 2)
        self.assertIsNone(t.sent[0]["payload"]["variables"]["after"])
        self.assertEqual(t.sent[1]["payload"]["variables"]["after"],
                         json.loads(fixture_text("recorded_page1.json"))
                         ["data"]["repository"]["pullRequests"]["pageInfo"]["endCursor"])

    def test_stops_mid_first_page(self):
        responses = [ok(fixture_text("recorded_page1.json"))]
        c, _, _ = client(responses)
        nodes, _ = fetch_pull_requests(c, "cli", "cli", ts("2026-10-07T19:00:00Z"), page_size=4)
        self.assertEqual([n["number"] for n in nodes], [13013, 14578])

    def test_dedupes_prs_that_move_between_pages(self):
        a = pr_node(1, updated="2026-09-30T10:00:00Z")
        b = pr_node(2, updated="2026-09-29T10:00:00Z")
        c_ = pr_node(3, updated="2026-09-28T10:00:00Z")
        c, _, _ = client([ok(page([a, b], has_next=True, cursor="x")), ok(page([b, c_]))])
        nodes, _ = fetch_pull_requests(c, "acme", "app", ts("2026-09-01T00:00:00Z"))
        self.assertEqual([n["number"] for n in nodes], [1, 2, 3])

    def test_halves_page_size_when_github_times_out(self):
        c, t, _ = client([HttpResponse(502, {}, "Bad gateway"), ok(page([]))])
        fetch_pull_requests(c, "acme", "app", ts("2026-09-01T00:00:00Z"), page_size=50)
        self.assertEqual([s["payload"]["variables"]["first"] for s in t.sent], [50, 25])

    def test_fetches_remaining_reviews(self):
        node = pr_node(7, updated="2026-09-30T10:00:00Z",
                       reviews=[review("bob", "2026-09-20T10:00:00Z")])
        node["reviews"]["pageInfo"] = {"hasNextPage": True, "endCursor": "r1"}
        more = {"data": {"rateLimit": {"cost": 1, "remaining": 4000, "resetAt": "2026-10-01T00:00:00Z"},
                         "repository": {"pullRequest": {"reviews": {
                             "pageInfo": {"hasNextPage": False, "endCursor": None},
                             "nodes": [review("carol", "2026-09-21T10:00:00Z")]}}}}}
        c, t, _ = client([ok(page([node])), ok(more)])
        nodes, _ = fetch_pull_requests(c, "acme", "app", ts("2026-09-01T00:00:00Z"))
        self.assertEqual([r["author"]["login"] for r in nodes[0]["reviews"]["nodes"]], ["bob", "carol"])
        self.assertEqual(t.sent[1]["payload"]["variables"], {"owner": "acme", "name": "app",
                                                             "number": 7, "after": "r1"})

    def test_max_prs_truncates(self):
        nodes = [pr_node(i, updated="2026-09-30T10:00:00Z") for i in range(1, 6)]
        c, _, _ = client([ok(page(nodes, has_next=True, cursor="x"))])
        got, meta = fetch_pull_requests(c, "acme", "app", ts("2026-09-01T00:00:00Z"), max_prs=3)
        self.assertEqual(len(got), 3)
        self.assertTrue(meta["truncated_at_max_prs"])


if __name__ == "__main__":
    unittest.main()
