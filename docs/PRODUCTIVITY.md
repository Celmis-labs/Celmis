# Engineering productivity metrics

Cycle time, review pickup, pull request size, activity and the four DORA
measures (deployment frequency, lead time, change failure rate, time to
restore), computed from the pull-request history of the repositories you choose.

The split follows the licence boundary. The tables, the sync engine, the
scheduler and the webhook hook are part of the AGPL core (`src/productivity`) and
stay quiet until a repository is switched on, so an install that never opts in
spends no provider API calls. The metrics maths, the API and the `/productivity`
page are Enterprise (`src/ee/analytics`, the `analytics` feature of the licence).

## Who can see it

The page and every endpoint under `/api/analytics/productivity` are for workspace admins
and owners only. Developer-level tables show names next to numbers, so the
default is the narrow one. Editors and viewers do not see the page or the
settings, and an API call from them is refused.

## Turn it on

Settings are repository over workspace over built-in, and the workspace default
is the row with an empty repository. Nothing is synced until `enabled` is on.

| Setting | Built-in | Meaning |
| --- | --- | --- |
| `enabled` | off | Sync this repository's history. |
| `backfill_days` | 180 | How far back the first sync reads (up to 730). |
| `production_branches` | `main`, `master` | Merges into these are deployments. |
| `integration_branches` | none | Pull requests merged here ride in a later release. |
| `deploy_source` | `merge` | A deployment is a `merge` into production, the provider's own `provider` deployment record, or a `tags` match. |
| `tag_pattern` | `^v?\d` | Which tags count when the source is `tags`. |
| `revert_patterns`, `hotfix_branch_patterns`, `bugfix_patterns` | built-in regular expressions | How a revert, a hotfix and a bug fix are recognised. |
| `ignored_authors` | none | Accounts left out of every figure. |
| `bot_markers` | the reviewer's own | Comments carrying one of these are bots', never a human review. |
| `failure_window_days` | 7 | A revert or hotfix within this many days marks a deployment failed. |
| `deploy_group_minutes` | 30 | Merges closer than this are one deployment. |

A request to estimate the cost of the first sync is on the settings page
(`GET /api/analytics/productivity/sync/estimate`), and a sync can be started by hand
(`POST /api/analytics/productivity/sync/run`). A scheduler also ticks hourly and queues
one job per enabled repository. Requests to a provider share one rate limit per
credential and honour `Retry-After`.

The scheduler is an install setting, not a repository one:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CELMIS_PRODUCTIVITY_INTERVAL_MINUTES` | 60 | Minutes between ticks; `0` turns the tick off. |
| `CELMIS_PRODUCTIVITY_FIRST_DELAY_SECONDS` | 180 | Wait after start-up before the first tick. |
| `CELMIS_PRODUCTIVITY_JOB_BUDGET_SECONDS` | 480 | One sync job stops at this budget and queues the rest as a later slice, so a long history never holds a worker. |
| `CELMIS_DISABLE_PRODUCTIVITY_SCHED` | unset | `1` never starts the scheduler (a hand-started sync still works). |

## Definitions

Every figure on the page has a tooltip with its definition. In short:

- coding time: first commit to the pull request being opened;
- pickup time: opened to the first human review (a comment, approval or review by
  somebody who is neither the author nor a bot);
- review time: first human review to merge;
- cycle time: first commit to merge;
- a pull request merged with no human review before the merge is counted as
  merged without review, and left out of pickup and review;
- a pull request whose details are not read yet is counted separately, as
  unknown rather than unreviewed;
- lead time: from first commit to the deployment that carried it;
- change failure rate: failed deployments over settled ones (a deployment still
  inside the failure window can fail tomorrow, so it counts in neither number);
- time to restore: from the failed deployment to the deployment of the fix.

Medians come with the 75th and 90th percentiles, never means, and every headline
figure carries the same figure for the previous period of equal length.
Deployments derived from merges approximate a rollout, and the page says so.

## Reading it

A change in a median over a few pull requests means little; look at the sample
size shown with it. Compare a team with itself over time, not with another team.
The developer table is for finding where a pull request waits (for review, for
fixes), not for ranking people.
