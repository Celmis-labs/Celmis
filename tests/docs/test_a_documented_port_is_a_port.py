"""A documented value made `docker compose config` refuse the file.

`docker-compose.yml` writes the loopback address itself:

    - "127.0.0.1:${API_HOST_PORT:-8000}:8000"

so the variable holds a PORT. `docs/ORACLE_CICD.md` said to set
`API_HOST_PORT=127.0.0.1:8000`, which substitutes to
`127.0.0.1:127.0.0.1:8000:8000` and fails with

    invalid IP address: 127.0.0.1:127.0.0.1

Anybody following that page could not start the stack at all. Keyed on the
property: where compose supplies the address, the docs supply only the port.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"

DOCS = [ROOT / ".env.example", *sorted((ROOT / "docs").glob("*.md")), ROOT / "README.md"]


def _address_prefixed_vars() -> set[str]:
    """Variables compose already puts an IP in front of."""
    text = COMPOSE.read_text()
    return set(re.findall(r'"\d+\.\d+\.\d+\.\d+:\$\{([A-Z][A-Z0-9_]*)', text))


def test_compose_really_does_prefix_them() -> None:
    """If this is empty the tests below assert nothing."""
    found = _address_prefixed_vars()
    assert found, (
        "no compose port mapping writes an address in front of a variable any "
        "more; this test no longer describes the file"
    )


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_no_doc_puts_an_address_in_a_port_variable(doc: Path) -> None:
    if not doc.exists():
        pytest.skip(f"{doc.name} is not in this tree")
    variables = _address_prefixed_vars()
    for line_no, line in enumerate(doc.read_text().splitlines(), 1):
        stripped = line.strip()
        # A line that quotes the mistake in order to correct it is prose, not
        # an instruction. Assignments are what a reader copies.
        if stripped.startswith(("#", ">", "*", "-")) or "`" in stripped:
            continue
        for var in variables:
            match = re.match(rf"^{var}=(.+)$", stripped)
            if match and not match.group(1).strip().isdigit():
                raise AssertionError(
                    f"{doc.name}:{line_no} sets {var}={match.group(1)!r}. "
                    f"docker-compose.yml already writes the address in front "
                    f"of this variable, so the value must be a bare port; "
                    f"anything else makes compose refuse the file."
                )
