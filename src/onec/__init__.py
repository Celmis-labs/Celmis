"""1C:Enterprise files that are not text: the container format and what is in it.

An external report (`.erf`), an external data processor (`.epf`) and the
`Form.bin` of an ordinary form in a configuration dump are all one binary
format, a "container": a table of named entries, each a chain of blocks, the
top level raw-deflated, entries nesting further containers. The code of an
ordinary form lives in its `Form.bin`, so without reading the container that
code is invisible to search and to the graph.

    container.py   read a container, with limits (size, depth, entries, inflation)
    brace.py       the brace-list text 1C writes its descriptions in
    external.py    what a container holds: object, forms, modules, templates, dates
    bsl_text.py    procedures, parameters with defaults, strings and comments of BSL text
    config.py      a configuration dump to compare with (a directory or a git revision)
    compare.py     procedure diff against the configuration, change markers, staleness
    checks.py      static compatibility checks against the configuration
    report.py      the text report the MCP tool and the agent session show
    store.py       short-lived storage of uploaded files (tickets, handles, TTL)
    unpack.py      an agent session's attachment unpacked beside itself, with REPORT.md

Everything read from a container is untrusted data: it is shown, never run.
"""
