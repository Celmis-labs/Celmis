# Review settings, incremental review and cadence

Every setting below is inheritable: a repository value wins over the workspace
default, which wins over the built-in. A repository that stores nothing follows
the workspace, so changing the workspace default reaches every repository that
has not made its own choice. The settings live on the repository's review page
(and the workspace defaults page); each has the same name in the API.

## What a review reads

| Setting | Built-in | Meaning |
| --- | --- | --- |
| `target_branches` | every branch | Base branches that are reviewed. Entries are names or globs, and a leading `!` excludes (`["develop", "release/*"]`, `["!main"]`). Exclusion wins. |
| `review_scope` | `incremental` | `incremental` reviews only what is new since the last review; `full` always reads the whole pull request. |
| `run_on_drafts` | off | Review draft pull requests as well. |
| `ignore_globs` | none | Files never reviewed. |
| `ignored_title_keywords` | none | A pull request whose title contains one of these (case-insensitive) is not reviewed automatically, for example `WIP` or `[skip review]`. |

The review language is a workspace choice (`review_language` on the LLM
configuration page, for example `en` or `uk`); it applies to the findings, the
summary and the comments the reviewer posts.

### Incremental review

After a complete review that was posted, Celmis remembers the commit it read
(`last_reviewed_sha`). The next review of the same pull request reads only the
commits since then, and answers with the whole pull request instead whenever it
cannot be sure:

1. the review was forced, or the repository's scope is `full`;
2. nothing was reviewed before (the first review);
3. the provider cannot list the commits;
4. the reviewed commit is gone from the branch (force-push, rebase);
5. otherwise the new commits are read, and merge commits that bring in nothing of
   the pull request's own are skipped.

When nothing is new, an automatic trigger is skipped quietly, and a person's
request (`@celmis review`, the Review button) reads the whole pull request,
because a quiet skip would look broken. Agents that need the whole picture (the
business-logic check against a task) always read the whole pull request.

After an incremental run, earlier comments whose code the new commits removed
or rewrote are resolved by the reviewer (outdated threads), and a finding
that is already on the pull request is not posted twice. Those resolved threads
are not read as feedback from a person ([Memories and learning](MEMORIES_AND_LEARNING.md)).

## When a review starts

| Setting | Built-in | Meaning |
| --- | --- | --- |
| `review_cadence` | `automatic` | `automatic`: every push is reviewed. `auto_pause`: the same until the pull request receives `auto_pause_pushes` pushes within `auto_pause_window_minutes`, then automatic reviews wait. `manual`: only when somebody asks. |
| `auto_pause_pushes` | 3 | Pushes (2 to 20) that pause a pull request. The push that reaches the limit is itself the first one skipped. |
| `auto_pause_window_minutes` | 15 | The sliding window (1 to 240) the pushes are counted in. |

A paused pull request gets one short note saying so. `@celmis start-review`
(or the Resume button) resumes it and reviews it at once; `@celmis pause` and the
Pause button pause it until somebody resumes, whatever the cadence says. An
automatic pause lapses by itself when the repository leaves the `auto_pause`
cadence.

A person's explicit request (command, button, bulk review, CLI, MCP tool) skips
the draft, title and cadence gates. `--force` also skips the target-branch gate
and the "no new commits" check. Nothing skips the enabled switch, the size
limit, or the check that the pull request has a diff to read.

## What a review posts

| Setting | Built-in | Meaning |
| --- | --- | --- |
| `summary_enabled` | on | Describe the change in the pull request. |
| `summary_target` | `comment` | `comment` posts the overview as a comment; `description` writes it into the pull request description inside a marked block. |
| `summary_on_new_commits` | `replace` | What the next push does to an overview already there: `replace`, `append` or `nothing`. |
| `summary_existing_description` | `append` | With `description`, what happens to text a person already wrote: `append` after it, `complement` the gaps, or `replace` it. |
| `completed_comment` | `completed` | The layout of the closing comment: `completed` (verdict, count, breakdown, what was read) or `classic`. |
| `commands_guide_enabled` | on | End the closing comment with a guide to the commands the repository allows. |
| `started_comment_enabled` | on | Post a short "review started" comment. |
| `comment_min_severity` | per workspace | Findings below this are kept off the pull request. |
| `max_inline_comments` | per workspace | Inline comments per review. |
| `approve_when_clean` / `request_changes_on_critical` | off | Let the review approve a clean pull request or request changes on a critical finding. |
| `status_feedback` | on | Show the review as a commit status or check. |
| `committable_suggestions` | off | Use the provider's suggestion blocks where a fix is a few lines. |
| `base_instruction` | none | A sentence every agent is given, such as "we use `Result` types, not exceptions". |
| `message_started` / `message_finished_header` | built-in | Replace the wording of the two comments. |

## Other settings

Settings for commands, memories, learning, issues and Jira are described where
they belong: [PR commands](PR_COMMANDS.md), [Memories and learning](MEMORIES_AND_LEARNING.md),
[Issues backlog](ISSUES_BACKLOG.md), [Jira](JIRA.md).

## A rollout checklist

For a first repository, in this order:

1. Set `target_branches` to the branches that matter, for example `["develop"]`.
2. Set the workspace's review language.
3. Choose where the overview goes (`summary_target`) and the closing comment
   (`completed_comment`).
4. Create the webhook with comment events (the repositories page does it) so the
   commands work, and check that it is not marked "needs repair".
5. Pick a cadence. `auto_pause` suits a repository where people push often.
6. Leave `learning_suppression` on `shadow` for a few weeks.
7. Connect Jira if tasks carry the requirements ([Jira](JIRA.md)).
8. Review a handful of pull requests without posting (the dry run in the review
   CLI) and look at the findings before letting the reviewer comment.

### Judging a dry run

Read each finding against the code. Count the ones you would act on (useful),
the ones that are true but trivial (noise), and the ones that are wrong. A
review is worth keeping when most findings are useful and the wrong ones are
rare. If one rule produces most of the noise, suppress that rule for the
repository; if one agent does, disable that agent; if a project convention
is missed, teach it with `@celmis remember:`.
