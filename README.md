# review-load

Who is carrying code review on your GitHub repo? One command, one repo, the last 30 days:

- **Review load per reviewer**: reviews given, share of all reviews, how much the top 3 carry, and the review "bus factor".
- **Time to first human review**: median, p90, and the share of PRs that waited more than 24 hours.
- **PR size vs wait**: how big PRs are, and how long each size waits for a first review.
- **Bot vs human reviews**: counts of each.

You get a one-screen Markdown report, a JSON file with every number (and one row per PR), and a bar chart in your terminal. Python 3.9+, standard library only, nothing to install. It reads pull-request metadata and never your code.

```text
review-load · cli/cli · last 30 days (2026-09-07 → 2026-10-07 UTC)

Who carries review (70 reviews by 15 people)
  dev-01  ██████████████████████████████    26   37%
  dev-02  ██████████████                    12   17%
  dev-03  ████████████                      10   14%
  dev-04  ████████                           7   10%
  dev-05  █████                              4    6%
  dev-06  ██                                 2    3%
  dev-07  █                                  1    1%
  dev-08  █                                  1    1%
  dev-09  █                                  1    1%
  dev-10  █                                  1    1%
  ... 5 more, 5 reviews
  top 3: 69% · bus factor: 2

Time to first human review (39 reviewed PRs)
  median 6.2 h · p90 21.4 d · waited >24 h: 50% · open with no review: 11

PR size → median wait for a first review
  XS     0-10  █████████████           16 PRs     2.2 h
  S    11-100  ████████████████████    24 PRs     1.3 h
  M   101-400  █████████               11 PRs     4.1 d
  L  401-1000  ████████                10 PRs     9.0 h
  XL    1001+  ███████████             13 PRs     2.9 d

Reviews: 70 human, 48 bot (2 bots on 47 PRs)
```

That is a real run against the public [cli/cli](https://github.com/cli/cli) repo on 2026-10-07, made with `--anonymize` (people's logins replaced by `dev-01`, `dev-02`, ...). It took 3 API calls. The full report it wrote is in [examples/cli-cli/review-load.md](examples/cli-cli/review-load.md), and the JSON next to it.

## Quick start (30 seconds)

```bash
git clone https://github.com/testatahq/review-load
cd review-load
python3 -m review_load your-org/your-repo
```

The token comes from `GITHUB_TOKEN`, then `GH_TOKEN`, then `gh auth token` if you use the GitHub CLI. Any token works for a public repo. The report lands in `./review-load-report/` (`review-load.md` and `review-load.json`).

Prefer a command on your PATH? `pipx install git+https://github.com/testatahq/review-load` gives you `review-load your-org/your-repo`.

### Private repos: a read-only token

Create a **fine-grained personal access token** (GitHub → Settings → Developer settings → Fine-grained tokens):

- Resource owner: your organization
- Repository access: only the repo you want to measure
- Repository permissions: **Pull requests: Read-only** (Metadata: Read-only is added automatically). Nothing else.

```bash
export GITHUB_TOKEN=github_pat_...
python3 -m review_load your-org/your-repo
```

If the org requires approval for fine-grained tokens, an owner approves it once. A classic token also works but needs the broad `repo` scope for private repos, so prefer fine-grained. The tool sends GraphQL queries only, never mutations.

### Every week in GitHub Actions

Copy [.github/workflows/review-load.yml](.github/workflows/review-load.yml) into your repo's `.github/workflows/`. It runs on Mondays (or on demand from the Actions tab), uses the built-in `GITHUB_TOKEN` with `contents: read` and `pull-requests: read`, and posts the report as the run's job summary. It writes nothing to your repo: no comments, no issues, no commits. The Markdown and JSON are kept as a run artifact for 90 days.

## Options

| Option | What it does |
|---|---|
| `--days N` | Window length, default 30 (1 to 365) |
| `--out DIR` | Where to write `review-load.md` and `review-load.json`, default `./review-load-report` |
| `--anonymize` | Replace people's logins with `dev-01`, `dev-02`, ... (busiest reviewer first). Bots keep their names. PR numbers and links stay, so this is for sharing a screenshot, not a privacy guarantee |
| `--bots a,b` | Extra logins to treat as bots. GitHub Apps (Dependabot, Copilot, Renovate, ...) are detected already; use this for machine users that are normal accounts |
| `--include-bot-prs` | Count PRs opened by bots in wait and size too (left out by default; see below) |
| `--top N` | Reviewers listed by name in the Markdown report, default 8 |
| `--max-prs N` | Stop after N PRs, default 5000 |
| `--page-size N` | PRs per API call, default 50 (lowered automatically if GitHub times out) |
| `--api-url URL` | GraphQL endpoint for GitHub Enterprise Server, e.g. `https://github.example.com/api/graphql` (in Actions it is picked up from `GITHUB_GRAPHQL_URL`) |
| `--quiet` | Print only the two output paths |

Exit codes: `0` report written, `1` GitHub API problem (the message says what to do), `2` usage problem or no token.

## What it measures, and how

The window is the last N days up to the moment you run it, in UTC.

| Number | Definition |
|---|---|
| **A review** | One person submitting at least one review (approve, request changes, comment, or a review later dismissed) on one PR inside the window. Ten comment rounds by the same person on the same PR count once, so long threads do not inflate anyone's load. The raw count of review submissions is in the JSON as `submissions`. |
| Not human reviews | Reviews by the PR's own author (replies in their own threads show up as reviews), by bots, and by deleted accounts. |
| **Share** | A reviewer's reviews ÷ all human reviews in the window. |
| **Top 3 share** | The three busiest reviewers' combined share. |
| **Review bus factor** | The fewest people who together did at least half of all reviews. A bus factor of 1 means one person does half the reviewing. |
| **Review clock** | Starts when a PR is opened, or when it is marked ready for review if it was opened as a draft. Stops at the first human review. A review left while the PR was still a draft counts as 0 hours. |
| **Wait population** | PRs that became ready for review inside the window, were opened by a person, and are not drafts now. |
| **Median, p90** | Over the PRs in the wait population that got a human review. p90 means 9 in 10 got their first review faster than this. |
| **Waited more than 24 h** | Of the PRs in the wait population that have been ready for at least 24 hours (so the answer is known) and either got a human review or are still open: the share whose first human review came after 24 hours, or has not come yet. PRs merged or closed with no human review are counted separately, not here. |
| **Size** | Lines added + lines deleted. Buckets: XS 0-10, S 11-100, M 101-400, L 401-1000, XL 1001+. M ends at 400 because that is where reviewer attention is commonly reported to drop off. |
| **Bot reviews** | Reviews by GitHub App accounts (for example `copilot-pull-request-reviewer`), logins ending in `[bot]`, and anything passed to `--bots`. Counted per bot per PR, like human reviews. |

**Which PRs are read.** Every PR in the repo updated inside the window, newest update first, stopping at the first one last updated before the window. A review updates its PR, so every review submitted in the window is seen. Reviews count toward load if they were submitted inside the window, whatever PR they are on (including PRs opened by bots: reviewing a dependency bump is still review work).

**Bot-authored PRs** (Dependabot, Renovate, and coding agents that open PRs under a bot account) are left out of the wait and size numbers by default, because they often sit unreviewed by design and would swamp the human picture. The report says how many were left out. `--include-bot-prs` puts them back.

**What it reads.** For each PR: number, URL, state, draft flag, timestamps, lines added and deleted, the author's login, the first draft/ready-for-review event, and each review's author, state and time. Never code, diffs, titles, descriptions or comments. The token is sent only to the GitHub API and is never printed or written to disk. No telemetry: the only network calls go to GitHub.

**Cost.** Roughly one API call per 50 PRs updated in the window, plus one per PR with more than 100 reviews. A repo with 200 PRs a month costs about 5 calls (GitHub allows 5,000 points an hour). If fewer than 100 points remain, the tool waits for the reset; on `Retry-After` or a secondary rate limit it waits as told; if GitHub times out it halves the page size and tries again.

## Limits

- **Snapshot of one repo.** No before/after comparison, no trend, no org-wide roll-up. Run it weekly in Actions and keep the artifacts if you want a series.
- **Only GitHub reviews count.** A "looks good" left as a plain PR comment, in Slack, or at someone's desk is invisible. Teams that review that way will look slower than they are.
- **The clock is wall-clock time.** Nights, weekends and holidays count. It starts at ready-for-review, not when a reviewer was requested.
- **Open-source repos look slower.** Many outside contributions are never reviewed by design; they show up in "waited more than 24 h" and "closed with no human review" (the cli/cli sample has 19 of those).
- **Small numbers are noisy.** A p90 over 12 reviewed PRs is one or two PRs. The report shows the counts next to every figure.
- **Machine users** that are ordinary GitHub accounts look human until you list them with `--bots`.
- **Very busy repos.** It stops at `--max-prs` (5000) PRs updated in the window and warns when it does. A PR updated while the tool is paging can, rarely, be missed.

## Development

```bash
python3 -m unittest discover -s tests -t . -v
```

The tests use responses recorded from the GitHub API (logins scrubbed) plus hand-built cases for the edge cases, and fail if anything tries to reach the network. Code layout: `github.py` (token, client, paging, rate limits), `model.py` (parsing), `metrics.py` (every calculation, no I/O), `report.py` (Markdown, JSON, terminal), `cli.py`.

The before/after comparison, the hours-and-dollars estimate, AI-assisted PR detection and org-wide aggregation are deliberately not here; they are part of the [AI Coding Rollout Kit](https://testata.co/engineering#kit).

## License

MIT. Copyright (c) 2026 Testata. Maintained by Testata, raul@testata.co. Bug reports and pull requests are welcome.
