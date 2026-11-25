<div align="center">

# Trend Radar

**A daily digest of the open-source repos that are rising, not just the ones that are big.**

Trend Radar merges GitHub Trending, Trendshift and the Hacker News front page into one ranked list,
remembers every day, and flags what is new, what is accelerating and what is crossing over between sources.

[![radar](https://github.com/dimzachar/trend-radar/actions/workflows/radar.yml/badge.svg)](https://github.com/dimzachar/trend-radar/actions/workflows/radar.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![No API keys](https://img.shields.io/badge/API%20keys-none%20required-brightgreen)

**[Live digest →](https://trend-radar.q2wz0v.workers.dev)**

</div>

---

## Why

Every trending page answers the same question: *what is popular today?* It forgets yesterday.

The interesting signal is the **change**: a repo that went from 200 to 900 stars a day, a project
Trendshift caught two days before GitHub did, a tool that keeps coming back all week. Trend Radar keeps
a small SQLite history so it can show exactly that.

## Features

- **Three sources, one list.** GitHub Trending, [Trendshift](https://trendshift.io) and Hacker News
  front-page GitHub links, merged by `owner/name`.
- **Signals from history.** Each repo is labelled from the daily snapshots:

  | Signal | Meaning |
  |---|---|
  | `NEW` | First time seen on any source |
  | `ACCELERATING` | Stars per day rose three days in a row |
  | `CROSSOVER` | On Trendshift first, on GitHub Trending today |
  | `STICKY` | Trending on 5 of the last 7 days |
  | `MULTI-SOURCE` | On two or more sources today |

- **Themes.** Repos are grouped into themes such as *Agent skills & plugins*, *AI video & media* or
  *Self-hosted alternatives*, so the shape of the day is visible at a glance.
- **A digest you can read.** A self-contained HTML page per day (light and dark, filters by signal and
  theme, sparklines) plus a Markdown version.
- **Enrichment.** Language, topics, license and total stars from the GitHub API.
- **No keys, no scraping service.** Plain HTTP and BeautifulSoup. A `GITHUB_TOKEN` is optional and
  only raises the enrichment rate limit.
- **Cheap to host.** A daily GitHub Actions run (about a minute) and a static site on Cloudflare Workers.

## Quick start

```bash
git clone https://github.com/dimzachar/trend-radar
cd trend-radar
pip install -r requirements.txt
python radar.py
```

Open `digests/index.html`. Run it again tomorrow and the signals start to appear.

| Option | Effect |
|---|---|
| `--no-enrich` | Skip the GitHub API enrichment |
| `--date YYYY-MM-DD` | Store the snapshot under another date (testing) |
| `GITHUB_TOKEN=…` | Enrichment at 5,000 requests/hour instead of 60 |

Running twice on the same day replaces that day's snapshot, so retries never double-count.

## How it works

```
Cloudflare Worker cron (21:00 UTC)
        │  workflow_dispatch
        ▼
GitHub Actions ── radar.py ──► fetch ─► merge ─► store ─► signals ─► themes ─► render
        │                                   data/radar.db         digests/YYYY-MM-DD.{html,md}
        ├─ commit data/ and digests/ back to the repo
        └─ wrangler deploy ──► static site (+ /radar.db)
```

The run happens at **21:00 UTC** because Trendshift's "gained today" counter resets at midnight UTC,
so a late run captures most of the day. Snapshots are dated in UTC.

The trigger lives in a Worker rather than in the workflow's `schedule:` because GitHub's scheduler
is best effort and can start late or skip a run. The workflow still has a `schedule:` at 21:30 UTC
as a backup: it exits early when the Worker's run already saved tonight's digest.

### Data model

`data/radar.db` is a small SQLite file, committed so that every run continues the same history.

| Table | Columns |
|---|---|
| `snapshots` | `date`, `repo`, `source` (`gh` / `ts` / `hn`), `stars_today`, `rank` |
| `repos` | `repo`, `slug`, `first_seen`, `desc`, `lang`, `stars_total`, `topics`, `license`, `created_at`, `theme` |

It grows by roughly 25 KB a day. The published site serves it at `/radar.db` for anyone who wants
to analyse it.

## Deploy your own

1. **Fork** this repository.
2. In `wrangler.jsonc`, set `name` (part of your site's URL) and `vars.GH_REPO` to your fork.
3. Add three **repository secrets** (*Settings → Secrets and variables → Actions*):

   | Secret | Value |
   |---|---|
   | `CF_ACCOUNT_ID` | Your Cloudflare account ID |
   | `CF_API_TOKEN` | A Cloudflare API token from the *Edit Cloudflare Workers* template |
   | `WORKER_GH_TOKEN` | A fine-grained GitHub token for your fork only, with *Actions: Read and write* |

4. Run the workflow once: *Actions → radar → Run workflow*, or `gh workflow run radar.yml`.
   This deploys the site and the Worker, and hands `WORKER_GH_TOKEN` to the Worker, which then
   starts the workflow every day at 21:00 UTC.

Without Cloudflare, skip the Worker and the publish steps: the backup `schedule:` at 21:30 UTC
runs the workflow on its own every day.

## Project structure

```
radar.py                    fetch, merge, store, signals, themes, render
template.html               the digest page (data is injected as JSON)
requirements.txt
data/radar.db               daily snapshots
digests/                    one HTML + Markdown digest per day; index.html is the latest
worker/index.js             Cloudflare cron that starts the workflow
wrangler.jsonc              Worker and static-site config
.github/workflows/radar.yml the daily run
```

## Limitations

- Trending pages are parsed from HTML. If GitHub or Trendshift change their markup, a source can
  return nothing; the other sources still produce a digest and the page shows the failed source.
- Themes come from keyword rules, not a model. They are good for grouping, not for fine distinctions.
- Signals need history: `NEW` needs two days, `ACCELERATING` three, `STICKY` five.
- Hacker News contributes only front-page stories that link straight to a GitHub repository.

## Contributing

Issues and pull requests are welcome, especially new sources, better theme rules and parser fixes
when a site changes its markup. Please run `python radar.py` locally before opening a pull request
and include the digest it produced if you changed the output.

## Acknowledgements

Data from [GitHub Trending](https://github.com/trending), [Trendshift](https://trendshift.io) and
[Hacker News](https://news.ycombinator.com) via the [Algolia HN API](https://hn.algolia.com/api).
Each source belongs to its owner; Trend Radar only links back to it.
