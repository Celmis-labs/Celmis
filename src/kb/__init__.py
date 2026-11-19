"""Imported knowledge: markdown cards that people write, kept next to the
documentation Celmis generates.

A knowledge base is a folder of markdown files with YAML front matter: process
descriptions, migration decisions, task summaries. Celmis imports it into a
repository's documentation (the vault), where each card is searched by meaning
like any generated note, read and filtered by its front matter over MCP, linked
to the metadata objects of a 1C configuration it names, and marked for review
when those objects change after the commit the card was written against.

  * :mod:`src.kb.cards`   — one file in, one checked card out;
  * :mod:`src.kb.objects` — 1C object names, the graph, staleness;
  * :mod:`src.kb.store`   — import into the vault, the binding, re-checks;
  * :mod:`src.kb.catalog` — reading a vault back with its front matter;
  * :mod:`src.kb.links`   — links between notes and back-links;
  * :mod:`src.kb.changes` — the log of notes that changed.

The format is described in docs/KNOWLEDGE_BASE.md.
"""
