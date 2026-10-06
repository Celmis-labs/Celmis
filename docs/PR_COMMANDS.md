# Talking to the reviewer in a pull request

Celmis answers comments on a pull request. Everything below works the same on
GitHub, GitLab and Bitbucket Cloud, as long as the repository's webhook
delivers comment events (see [Webhooks](#webhooks)).

The reviewer is addressed by a handle, `@celmis` by default (`REVIEW_BOT_HANDLE`
changes it). The alias `/celmis` is always accepted too, which is the one to
use on a GitHub install where `@celmis` happens to be a real user.

## Commands

| Comment | What it does |
| --- | --- |
| `@celmis help` (or just `@celmis`) | Lists the commands. |
| `@celmis start-review` | Reviews what is new since the last review (the whole pull request on the first one), and resumes a paused pull request. |
| `@celmis review` | The same as `start-review`. |
| `@celmis review --force` | Reads the whole pull request again, ignoring what was already reviewed. Skips the draft, title and cadence gates, never the size and enabled gates. |
| `@celmis remember: <rule>` | Teaches the reviewers a rule for this repository. `--org` makes it workspace-wide, `--dir=src/api` limits it to a directory (`--dir` alone, written on a line of a file, takes that file's directory). |
| `@celmis -v business-logic PROJ-123` | Checks the change against a Jira task on demand (a link to the task works too). See [Jira](JIRA.md). |
| `@celmis <anything else>` | A question about the change. Answered by the model in the same thread. |

A command word counts as a command only when nothing but recognised flags
follows it on the line. `@celmis review this function for races` is a question,
not a review request. Fenced code, inline code and quoted lines are never read,
so a comment that quotes the help text does not run it.

### Questions

Text after the handle that is not a command word is answered as a question. The
answer is posted as a reply in the same thread (a child comment on Bitbucket, a
thread reply on GitHub and GitLab). The model sees the comment, the replies
under it, the diff of the file the comment is anchored on (the digest of the
whole change for a comment elsewhere) and the team's memories. The pull
request's text and the code are fenced as untrusted data, and the answer is
cleaned before posting: no markers of ours, no images, no raw HTML, no live
@-mentions. A bare "thanks" is a reaction, not a model call.

Spend is booked under the `pr_chat` surface on the Usage page. The knobs are
`REVIEW_CHAT_TIMEOUT_SECONDS`, `REVIEW_CHAT_MAX_CONTEXT_CHARS` and
`REVIEW_CHAT_MAX_REPLY_CHARS`.

### Feedback in a finding's thread

A reply in the thread of one of the reviewer's own comments is feedback first and
a question second. A thumb, `@celmis dismiss`, a short "false positive" or "by
design" (dismissals), or "useful" and "good catch" (confirmations) teach the
reviewer ([Memories and learning](MEMORIES_AND_LEARNING.md)). A correction in
longer words is read by one cheap model call. A question in that thread,
with or without the handle, goes on to the chat.

## Who may command the reviewer

The setting `command_permission` (repository over workspace over built-in):

- `repo_access` (the built-in): on a private repository, whoever can comment; on
  a public one, an owner, member or collaborator, a person with write access, or
  a participant of the pull request.
- `participants`: the author, reviewers and assignees of the pull request only.
- `anyone`: whoever can comment.

A provider that cannot tell never grants more than the participants rule does.
The same rule decides whose thumbs-down and resolved threads count as feedback.

Two more switches sit next to it: `commands_enabled` (the handle is ignored
altogether when off) and `chat_enabled` (questions are not answered).

## The reviewer never answers itself

A comment is the reviewer's own when it carries one of its markers, or when its
author is a bot account. Markers are invisible lines (see
`src/review/markers.py`), so a person who quotes the reviewer does not trigger
it. One token usually belongs to one person's account: a comment written
through it without a marker is that person talking, and they may command the
reviewer, teach it and give feedback like anyone else.

## Limits

Every command is recorded in a ledger. A redelivered or edited comment runs
once. Two hourly limits apply, a per-pull-request reply budget
(`command_replies_per_pr_per_hour`, 20) and a per-person command budget
(`commands_per_actor_per_hour`, 30); forced reviews have a small budget of their
own. A refusal is announced once per window. A refused command from a stranger
does not use up the pull request's budget. The pull-requests page shows the
commands given on a pull request next to its reviews, to people who may read the
repository.

## The guide in the review summary

With `commands_guide_enabled` (on by default) the completed comment ends with a
short guide to the commands. It lists only the commands this installation can
run and this repository has not switched off.

## Webhooks

Comment commands need comment events on top of the pull-request events:

| Provider | Events |
| --- | --- |
| GitHub | `issue_comment`, `pull_request_review_comment`, `pull_request_review_thread` |
| GitLab | Note events (`note_events`) |
| Bitbucket Cloud | `pullrequest:comment_created`, `pullrequest:comment_updated` |

Deliveries are verified (HMAC or token) exactly like the pull-request webhooks
and bound to the workspace of the registered repository. A webhook installed by
an earlier version does not deliver comments: the repositories page marks it
"needs repair", and one button (`POST /api/repos/webhooks/repair-outdated`, admin
only) re-subscribes all of them.

`scripts/pr_commands_smoke.py` sends a signed synthetic comment to a running
instance, and `scripts/probe_bitbucket_markdown.py` checks on a throwaway
Bitbucket pull request how invisible markers render.
