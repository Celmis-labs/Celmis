# The issues backlog and automatic resolution

A review's findings are not thrown away when the pull request moves on. Each one
becomes an issue that is followed from one push to the next and, when the pull
request merges with it still open, stays on a backlog until the code shows it is
gone.

## Identity

An issue is identified by its rule, its file and a normalised title. The line
number is not part of it (a push that adds ten lines above a defect moves the
line and leaves the defect), and neither are digits in the title (models like to
write "on line 42"). Issues are per pull request: the same defect on two pull
requests is two issues with two fates.

## Fixed by a later push

When a review of a new head finishes and an open issue is not found again, it
is marked fixed only if the file really changed between the two heads and the
flagged line is gone. Every doubt keeps it open: the same head reviewed twice, an
agent that failed or was skipped, a file that was not read (too large, ignored),
a rule that was filtered out. A model that does not repeat itself is not a fix.

## The backlog

When a pull request merges with open issues, they are backlog: `merged_at` is
set and the status stays open. Backlog issues are checked only against the head
of the branch the pull request merged into. A fix that lives in an unmerged
pull request is not a fix.

The check is a ladder, cheapest first, with no model call while a deterministic
answer exists:

1. The file's hash is the one already checked: nothing to do.
2. The file is gone: only a deletion confirmed by the provider fixes the issue
   (a rename is followed, an unexplained absence changes nothing).
3. The flagged line is still in the file: still there.
4. The line is gone but the file exists: a model decides from the issue, the
   snippet and the code that is now there. Only a clear "fixed" resolves it;
   "unsure", an error or an exhausted budget leave it open.
5. There was no line to look for: the first check records a baseline, and a
   later change to the file asks the model.

An issue nobody could read the branch for is shown as unreadable, never as
fixed. If a fix made by an automatic source is later reverted (the flagged line
is back), the issue reopens. A person's decision (resolved or dismissed by hand,
or by feedback) is never touched.

### When the check runs

- Right after a merge (debounced), by the merge webhook.
- Once a day for every repository with backlog issues
  (`CELMIS_ISSUES_SWEEP_INTERVAL_HOURS`, 24; `0` turns the sweep off). It costs one
  branch-head request per repository and branch, and nothing more when the head
  has not moved since the last pass. The first pass waits
  `CELMIS_ISSUES_SWEEP_FIRST_DELAY_SECONDS` (300) after start-up, and repositories
  are spaced `CELMIS_ISSUES_SWEEP_STAGGER_SECONDS` (5) apart so a large install
  does not call the provider all at once. `CELMIS_DISABLE_ISSUES_SWEEP=1` never
  starts the sweep.
- On demand: "Recheck now" on the issues page (`POST /api/issues/recheck`).
- Inside a review: the "resolve earlier issues" stage runs the same checks on the
  pull request being reviewed, and the completed comment says what it resolved.

## Settings

Repository over workspace over built-in.

| Setting | Built-in | Meaning |
| --- | --- | --- |
| `issues_auto_resolve` | on | A later change that removes a merged pull request's open issue from the target branch resolves it. Off: only people resolve issues. |
| `issues_resolve_llm_verify` | on | Ask a model to confirm a fix when the deterministic checks cannot. Off: only the deterministic answers resolve. |
| `issues_resolve_max_llm` | 8 | Model checks per repository and pass (0 to 50). |
| `issues_announce_resolved` | on | Name the resolved issues in the next completed comment. |

## The page

The issues page lists issues with their status (open, fixed, dismissed),
whether they are backlog, who or what resolved them, and the commit that fixed
them, with a summary of counts. A person can resolve or dismiss an issue by
hand (`PATCH /api/issues/{id}`). That choice is final for the automatic checks,
and a dismissal also counts as feedback for the reviewer
([Memories and learning](MEMORIES_AND_LEARNING.md)).
