# Team memories and learning from feedback

Two ways the reviewers get better at a repository without anyone editing a
prompt. Memories are facts a person tells them. Learning is what the reviewers
work out from how people react to their comments.

## Team memories

A memory is one short sentence (up to 800 characters), for example "amounts are
integer cents, never floats" or "everything under `legacy/` is frozen". It is
put in front of every agent that reviews a file the memory covers. The rules
library says what to enforce; a memory says what is true.

Scope is derived from where the memory is attached:

| Attached to | Scope | Reaches |
| --- | --- | --- |
| nothing | workspace | every repository of the workspace |
| a repository | repository | that repository |
| a repository and a path | directory | files under that path |

Each scope holds at most 200 memories, and the prompt takes at most
`REVIEW_MEMORY_PROMPT_CHARS` characters (3000) of them, most relevant first.

### Where they come from

- The `/memories` page (editor, admin and owner roles): add, edit, approve,
  reject and delete, with a preview of what a review of a set of paths would be
  told.
- A comment on a pull request: `@celmis remember: <rule>`, with `--org` or
  `--dir` for another scope (see [PR commands](PR_COMMANDS.md)).
- Proposals from the reviewer itself: a correction in a reply to a finding, and
  the "rules from history" job (several pull requests that agree).

### Who is trusted

A memory is active at once only when a trusted person wrote it: a Celmis user with
the editor, admin or owner role, the owner of the provider token (on a human
token, whoever types through it), or a name on `memory_trusted_commenters`.
Anything else, and every machine proposal while `knowledge_approval` is on (the
built-in), is stored as pending and waits for approval on the page. This is the
second line of defence against text a stranger can put in a comment.

### Settings

All three inherit repository over workspace over built-in.

| Setting | Built-in | Meaning |
| --- | --- | --- |
| `memories_enabled` | on | Memories are read into reviews and `remember` is accepted. |
| `knowledge_approval` | on | Machine proposals wait for a person. |
| `memory_trusted_commenters` | none | Extra people whose `remember` is active at once. |

### Who sees what

The page and its API are for editors and above. A repository's memories are
shown, counted and previewed only to somebody who may read that repository
(owners and admins hold every repository of the workspace), and the repository a
memory was taught in is shown only to people who may read it too.

## Learning from feedback

Every verdict a person gives a finding becomes a signal in an append-only table.
Signals are keyed by the finding's fingerprint (rule, file and title), which does
not depend on the pull request or the line, so a dismissal on one pull request is
found again on the next.

Where signals come from:

- a reply in the finding's thread (a thumb, `@celmis dismiss`, short words like
  "false positive", or free text read by one cheap model call);
- a thumb reaction on the comment, read at the next review of the same pull
  request (not available on Bitbucket);
- a resolved thread, which is weak, and which is upgraded when a following commit
  implements the finding, or turned into a weak dismissal when the pull request
  merges with the code unchanged;
- the verdict on the reviews page, and what the issues ledger sees (fixed, or
  merged unfixed).

Threads the reviewer resolves itself (outdated findings after a push) are not
feedback. Who may teach the reviewer follows `command_permission`
([PR commands](PR_COMMANDS.md)): a stranger's reply, thumb or resolved thread
counts for nothing and costs no model call.

### The learned filter

A review stage between the deterministic prefilter and the model's veto
compares each finding with the repository's signals (the same fingerprint, a
near-identical title in the same directory, or an embedding at 0.90 or more). A
finding is left out when enough people dismissed it: one dismissal for an exact
repeat, two distinct (pull request, person) pairs otherwise, so one pull request
cannot train the filter alone. Signals fade with a 90-day half-life, a person who
said "this was right" blocks the hiding, and a critical finding, or one proven by
a rule that matched text, is never hidden.

| Setting | Built-in | Meaning |
| --- | --- | --- |
| `learning_suppression` | `shadow` | `shadow` keeps every finding and reports what it would have left out; `on` leaves them out; `off` disables the stage. |
| `learning_excluded_reviewers` | none | People whose feedback teaches nothing (a bot, a lead who dismisses everything). |

Start in `shadow`: the review summary and the run page say how many findings
feedback would have hidden, so the effect can be judged before switching on.

### The Learning view

`/memories` has a Learning view (editors and above, only repositories one can
read) with the counts, the most dismissed findings, the rate at which findings
are implemented, and a way to forget a signal. Signals and the comment map leave
with their repository and with the person on erasure.

### Rules from history

A "from history" job on the rules page, and an optional weekly tick
(`REVIEW_LEARNING_RULES_SCHEDULE=weekly`), propose rules where several pull
requests agree. They arrive as pending, with the origin "learned".
