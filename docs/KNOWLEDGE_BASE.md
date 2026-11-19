# Knowledge cards

Celmis writes documentation from the code: one note per module, feature or
integration. A team often keeps knowledge that is not in the code: how a
business process runs, why a decision was made, what a term means in this
company, how a process moves to a new system. Write those as markdown files
with front matter and import them. They go into the same documentation as the
generated notes. Search, Q&A, the web page and the MCP `docs` tool then read
both, and each one shows its status.

## A card

```markdown
---
type: process
id: proc-order-reserve
status: verified
domain: sales
author: Olena
lang: uk
tags: [reserve, stock]
objects_1c: [Документ.ЗаказКлиента, РегистрНакопления.ТоварыВРезерве]
source: erp@3f2c1a9
related: [proc-order-ship]
replaces: [modules/sales]
owner: ops
migration: {target: frappe, wave: 2}
---
# Reserving goods for an order

When a customer order is posted, …
```

Celmis reads these keys. Every other key is kept as written.

| Key | What Celmis does with it |
|---|---|
| `type` | Any word: `process`, `decision`, `glossary`, `migration` and so on. Defaults to `note`. Five words belong to the generator (`module`, `feature`, `integration`, `overview`, `security`), so a card that uses one is stored as `kb-<type>` and the import warns about it. |
| `id` | The card's name in links and in `docs path=<id>`. Defaults to the file name. Two cards with the same id in one import: the second is skipped. |
| `status` | `draft`, `verified` or `needs-review`. Defaults to `draft`. Spellings such as `Verified`, `needs_review` and their Ukrainian and Russian equivalents are accepted. |
| `domain`, `author`, `lang`, `tags` | Filters. |
| `objects_1c` (or `objects`) | The 1C objects the card is about. See below. |
| `source` | `repo@<commit>` or a bare commit: the revision of the configuration the card was written against. |
| `related` | Ids or paths of other cards. These become links. |
| `replaces` | Generated notes the card supersedes, e.g. `modules/sales`. A **verified** card hides them from search answers. |

The front matter must be a YAML mapping of at most 16,000 characters. YAML
anchors and aliases are refused. Lists hold at most 200 entries and a value at
most 500 characters.

### Status is part of the answer

- Only `verified` counts as checked. Every list line, search hit and MCP answer
  carries the status, so a draft is never read as a fact.
- Search puts verified cards first, generated notes next, then cards marked
  `needs-review`, then drafts.
- A card is downgraded to `needs-review` automatically when it goes stale (see
  below). The card's file keeps the status it was written with. The web panel
  shows both.

### 1C objects

Write an object as `Type.Name` in any of these forms:

| Written | Stored as |
|---|---|
| `Документ.ЗаказКлиента` | `Documents.ЗаказКлиента` |
| `Document.ЗаказКлиента` | `Documents.ЗаказКлиента` |
| `Documents.ЗаказКлиента` | `Documents.ЗаказКлиента` |
| `РегистрНакопления.ТоварыВРезерве` | `AccumulationRegisters.ТоварыВРезерве` |
| `Справочник.Номенклатура.Форма.ФормаЭлемента` | `Catalogs.Номенклатура` (the object, not its form) |

The type word can be Russian or English, singular or plural, in any case. It is
the same list the 1C graph uses. A name without a type (`ЗаказКлиента`) is not
an object: it is kept and reported as "not a 1C object name". The stored form
is what the graph and the filters use, so `object=Document.X` finds a card
written with `Документ.X`.

At import time, choose the **1C configuration to check against**: a 1C
repository of the same workspace. Every object is looked up in its graph, and
the import report lists the objects that are not there.

### Staleness

A card with `source: <repo>@<commit>` and `objects_1c` is compared with the
configuration's history. If `git diff <commit>..HEAD` touches a file that
belongs to one of the card's objects (its XML, a module, a form), the card is
**stale**. It then reads `needs-review`, and the panel names the objects that
changed.

The check runs at import time and again after every index of the
configuration, so a push to the configuration flags its cards without anyone
re-importing them. It cannot always be done, and then the reason is shown:

- the configuration was uploaded as an archive and has no history;
- the commit is not in the clone (too old for the shallow history, or on
  another branch);
- the card has no `source` commit, or names no objects.

## Importing

On **Documentation**, pick the repository, click **Import cards**, then do one
of the following:

- **Files**: drop `.md` files or a `.zip`. Folders inside the zip are kept
  under `kb/`. By default the upload is the whole knowledge base, so imported
  cards it does not contain are removed. Untick the option to add cards
  without removing any.
- **From this repository**: the markdown under a folder of the repository
  itself (for example `docs/kb`). With **Import again after every push**,
  every index of the repository re-imports it, the same as the webhook
  re-indexes code. A card deleted from the folder disappears from the
  documentation.

Importing needs the `member` role or higher. Viewers read the cards. An import
writes only changed cards; an unchanged card is neither rewritten nor
re-embedded. The report lists what was created, changed, removed and skipped
(with the reason), the missing objects, and the stale cards.

The limits: 3,000 files, 1 MB per file, 50 MB per upload. Paths that leave the
folder, hidden folders, `__MACOSX`, links and files that are not markdown are
skipped.

Through the API, with the same roles:

| Route | |
|---|---|
| `POST /api/kb/{repo}/import` | multipart `files` (md or zip), `code_repo`, `replace` |
| `POST /api/kb/{repo}/import-repo` | `{"prefix": "docs/kb", "code_repo": "", "follow": true}` |
| `POST /api/kb/{repo}/check` | check objects and staleness again |
| `DELETE /api/kb/{repo}` | remove every imported card (generated notes stay) |
| `GET /api/kb/{repo}` | the notes, filtered by `type`, `status`, `domain`, `author`, `lang`, `object`, `tag`, `where=key=value …`, `cards_only` |
| `GET /api/kb/{repo}/note?path=` | one note: front matter, objects, staleness, links and back-links |
| `GET /api/kb/{repo}/graph` | every note and link |
| `GET /api/kb/{repo}/changes?since=7d` | the change log |

## Reading

- **Web.** The documentation list shows each card's status. The filters take
  type, status, a 1C object and any front-matter key (`where`). An open card
  shows its front matter, its objects (a missing one is marked), its staleness,
  and its links in and out. **Links** draws the graph and **Changes** lists
  what moved.
- **Search and Q&A** see cards like any note. The embedded text carries the
  title, type, status and objects, so a question about `Документ.ЗаказКлиента`
  finds the cards that name it.
- **MCP**: the `docs` tool of the developer profile (see
  [MCP_DEV.md](MCP_DEV.md#documentation-and-cards-docs)).

## Links

A link is any of these:

- an entry in `related`;
- a markdown link to another `.md` (`[text](../ship.md)`);
- a wiki link (`[[proc-order-ship]]` or `[[proc-order-ship|text]]`);
- an entry in `replaces`;
- for generated notes, their cross-references.

A link resolves by id, by relative path, by path under `kb/`, by file name, or
by title. A link that resolves to nothing is listed as dangling, on the card
and under the graph.

## The change log

Every created, changed or removed note is logged in the repository's
documentation folder. This covers generated notes as well as cards, and cards
that become stale or fresh again. The log is newest first with one entry per
note. Read it on the **Changes** tab, through `GET /api/kb/{repo}/changes`, or
with `docs changed_since=7d`. A removed generated note is listed only when the
caller may see the source path it documented.

## Not included

- **Generating documentation from your own list of processes.** The generator
  documents the code it finds. A list of processes is written as cards.
- **Limits and search inside knowledge packs.** Packs (`skills`, `brief`) are a
  separate feature, and these cards are not packs.
