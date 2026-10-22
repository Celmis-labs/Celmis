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

The review language is a repository choice (`review_language` in the policy),
else the workspace's (`review_language` on the LLM configuration page, for
example `en` or `uk`); it applies to every sentence a person reads: the
findings (the title, the one-sentence reason, the body, the fix), the summary,
the description, the answers to `@celmis` questions and the compliance verdicts.
The model can write in any language the setting offers. The few words the bot
adds itself ("Why", "Fix", "Also at lines ...") come from a message catalogue
that exists in English and Ukrainian; any other language gets the English
words around its own sentences.

Models sometimes answer one sentence, usually the reason, in the prompt's
language. After the findings are final, the comments about to be posted are
checked with a cheap rule (a sentence in the wrong script, or made of English
function words) and the ones that fail go back to the model once to be rewritten
(the run shows a "Language check" stage when that happened). No finding is
dropped for its language; one that cannot be fixed is posted as written. The
check is best-effort: it cannot tell Ukrainian from Russian.

### The shape of a comment

An inline comment is the reason, the problem, the body and the fix, in that
order, each only when there is something to say:

````
*Why:* line 42 dereferences cfg without a check
**🟠 cfg can be nil**
The call fails when the file is missing.
**Fix:**
```diff
-    use(cfg)
+    if cfg:
+        use(cfg)
```
<sub>agent: `defect` · rule: `defect.null-deref` · confidence: 0.80</sub>
````

The title is one line stating the problem, the body at most two short sentences.
The footer keys are fixed; the benchmark harness reads them.

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
(or the Resume button) resumes it and reviews it at once; The Pause button on the
pull-requests page pauses it until somebody resumes, whatever the cadence says
(there is no `@celmis pause` comment command). An
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
| `completed_comment` | `completed` | The layout of the closing comment: `completed` (a verdict line, then only what the author must act on) or `classic` (the longer form, with scope and performance). |
| `commands_guide_enabled` | on | When the closing comment has something to act on, end it with one line naming the two commands that matter (start a review, help), where the repository allows them. |
| `started_comment_enabled` | on | Post a short "review started" comment. |
| `comment_min_severity` | per workspace | Findings below this are stored for the web app but never reach the pull request: not as inline comments, and not in the counts, the breakdown, the verdict or the description either. When nothing reaches the level, the review is an approval. |
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

If the summary carries an `ADJUSTED` line saying agents gave a near-empty
reply, they returned `[]` after a handful of output tokens on a large diff.
That is a valid answer and the verdict is not changed, but a lite model without
reasoning tends to do this. Switch those agents to a model with reasoning, or
set Reasoning on the PR Review card in Settings → LLM and press **Apply to all
agents**, and run the dry run again.

### Per-agent model parameters: which value wins

Each agent's model, reasoning, temperature and max output tokens are layered.
The first layer that sets a value wins, and an empty field means "inherit":

1. **Repository agent override** (Settings → Review settings, per repository).
2. **Workspace agent override** (Settings → LLM → Review agents).
3. **Workspace base** (Settings → LLM → PR Review card): the review model, and
   its Reasoning level.
4. **The model's default** (nothing is requested).

The model is the exception in layer 1: a repository chooses it in its own
per-agent model field, and an agent with no per-repo model inherits the
workspace one. The base level covers reasoning only; temperature and the
output ceiling fall through to the install defaults.

**Apply to all agents** saves the chosen Reasoning as the base and clears the
agents' own Reasoning at the workspace layer, so they all follow it (their
model, temperature and ceiling are kept). A repository override still beats
it.

Every agent row, at both levels and on the base card, has a **Test** button.
It makes one tiny real call to the model that row would use, with the values in
the form (saved or not), through the same client a review uses, and says
whether the value was honoured (with the thinking tokens the provider
reported, when it reports them), withheld by Celmis, refused by the provider
(in its words), or timed out. A value the provider refuses is recorded as in a
real run, so the dropdown strikes it afterwards. The call needs the editor role
and is rate limited (`CELMIS_RL_LLM_TEST`, 12 per minute per client).

An accepted level with **0 thinking tokens** is shown as a warning, not a
pass. Some models read a low level as "think only if needed": on a lite model
`low` was accepted on every call yet produced no thinking on most of them,
while `medium` thought every time. A review on such a level behaves like one
without reasoning (`[]` in a second or two), so press Test a few times and pick
the lowest level that reliably reports thinking tokens.
