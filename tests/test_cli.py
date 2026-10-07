"""End-to-end runs of the command line with recorded responses. No network."""

import io
import json
import os
import re
import shutil
import tempfile
import unittest

from review_load import CTA_LINE
from review_load.cli import main
from review_load.github import HttpResponse
from review_load.report import to_terminal
from review_load.timeutil import format_hours as fh

from .helpers import FakeTransport, fixture_text, ok, page, pr_node, ts

NOW = ts("2026-10-07T20:00:00Z")
ENV = {"GITHUB_TOKEN": "ghp_test"}


def recorded_responses():
    # Two recorded cli/cli pages, then one synthetic page whose PR is older than the window.
    old = pr_node(1, created="2026-06-01T00:00:00Z", updated="2026-08-01T00:00:00Z")
    tail = page([old])
    tail["data"]["repository"]["nameWithOwner"] = "cli/cli"
    return [ok(fixture_text("recorded_page1.json")), ok(fixture_text("recorded_page2.json")), ok(tail)]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "report")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, *args, responses=None, env=ENV):
        stdout, stderr = io.StringIO(), io.StringIO()
        transport = FakeTransport(recorded_responses() if responses is None else responses)
        code = main(["cli/cli", "--out", self.out, *args], transport=transport, now=NOW, env=env,
                    run=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()),
                    sleep=lambda s: None, stdout=stdout, stderr=stderr)
        return code, stdout.getvalue(), stderr.getvalue(), transport

    def read(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as fh_:
            return fh_.read()

    def test_end_to_end(self):
        code, stdout, _, transport = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(len(transport.sent), 3)
        md = self.read("review-load.md")
        data = json.loads(self.read("review-load.json"))

        self.assertTrue(md.startswith("# Review load: cli/cli, last 30 days"))
        self.assertEqual(md.rstrip("\n").splitlines()[-1], CTA_LINE)
        self.assertIn("**3 people did 83% of the reviews. Review bus factor: 1.**", md)
        self.assertIn("| user-f | 3 | 50% |", md)
        self.assertIn("Median wait for a first human review: 4.1 d, p90 5.9 d (4 reviewed PRs). "
                      "100% of PRs (5 of 5) waited more than 24 h.", md)
        self.assertIn("| Waited more than 24 h | 100% (5 of 5) |", md)
        self.assertIn("| M (101-400) | 2 | 33% | 4.1 d | 100% |", md)

        self.assertEqual(data["meta"]["repo"], "cli/cli")
        self.assertEqual(data["meta"]["window"], {"days": 30, "since": "2026-09-07T20:00:00Z",
                                                  "until": "2026-10-07T20:00:00Z"})
        self.assertEqual(data["meta"]["api"], {"calls": 3, "points": 3})
        self.assertEqual(data["counts"]["prs_updated_in_window"], 8)
        self.assertEqual(data["review_load"]["total_reviews"], 6)

        self.assertIn("Who carries review (6 reviews by 4 people)", stdout)
        self.assertIn("user-f", stdout)
        self.assertTrue(stdout.rstrip("\n").endswith(CTA_LINE))

    def test_report_has_no_other_links(self):
        self.run_cli()
        md = self.read("review-load.md")
        for url in re.findall(r"https?://[^\s)|]+", md):
            self.assertTrue(url.startswith("https://github.com/cli/cli/pull/")
                            or url in ("https://testata.co/engineering#kit",
                                       "https://testata.co/engineering/audit"), url)
        self.assertEqual(md.count("testata.co"), 2)

    def test_anonymize(self):
        code, stdout, _, _ = self.run_cli("--anonymize")
        self.assertEqual(code, 0)
        md, js = self.read("review-load.md"), self.read("review-load.json")
        for text in (md, js, stdout):
            self.assertNotIn("user-", text)
        self.assertIn("| dev-01 | 3 | 50% |", md)
        self.assertIn("copilot-pull-request-reviewer", md)   # bots keep their names
        data = json.loads(js)
        self.assertTrue(data["anonymized"])
        # The same person gets the same alias as reviewer and as author.
        authors = {row["author"] for row in data["prs"]}
        reviewers = {r["login"] for r in data["review_load"]["reviewers"]}
        self.assertTrue(authors and authors <= reviewers)

    def test_quiet_prints_paths_only(self):
        code, stdout, stderr, _ = self.run_cli("--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(stdout.splitlines(), [os.path.join(self.out, "review-load.md"),
                                               os.path.join(self.out, "review-load.json")])
        self.assertEqual(stderr, "")

    def test_missing_token(self):
        code, _, stderr, transport = self.run_cli(env={})
        self.assertEqual(code, 2)
        self.assertIn("GITHUB_TOKEN", stderr)
        self.assertEqual(transport.sent, [])

    def test_bad_repo_and_days(self):
        stderr = io.StringIO()
        self.assertEqual(main(["not-a-repo"], env=ENV, stderr=stderr), 2)
        self.assertEqual(main(["cli/cli", "--days", "0"], env=ENV, stderr=stderr), 2)

    def test_api_error_exit_code(self):
        code, _, stderr, _ = self.run_cli(responses=[HttpResponse(401, {}, "Bad credentials")])
        self.assertEqual(code, 1)
        self.assertIn("401", stderr)
        self.assertFalse(os.path.exists(self.out))

    def test_empty_repo_renders(self):
        responses = [ok(page([]))]
        code, stdout, _, _ = self.run_cli(responses=responses)
        self.assertEqual(code, 0)
        md = self.read("review-load.md")
        self.assertIn("No human reviews in this window.", md)
        self.assertEqual(md.rstrip("\n").splitlines()[-1], CTA_LINE)


class FormatTests(unittest.TestCase):
    def test_format_hours(self):
        self.assertEqual(fh(None), "n/a")
        self.assertEqual(fh(0.5), "30 min")
        self.assertEqual(fh(5.25), "5.2 h")
        self.assertEqual(fh(72), "3.0 d")

    def test_terminal_ascii_fallback(self):
        out_dir = tempfile.mkdtemp()
        try:
            transport = FakeTransport(recorded_responses())
            main(["cli/cli", "--out", out_dir, "--quiet"], transport=transport, now=NOW, env=ENV,
                 stdout=io.StringIO(), stderr=io.StringIO())
            with open(os.path.join(out_dir, "review-load.json"), encoding="utf-8") as fh_:
                data = json.load(fh_)
        finally:
            shutil.rmtree(out_dir)
        text = to_terminal(data, unicode=False)
        text.encode("ascii")  # raises if anything outside ASCII slipped in
        self.assertIn("###", text)


if __name__ == "__main__":
    unittest.main()
