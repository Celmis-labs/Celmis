"""A sample value on a password-named schema property is a password.

Found by probing a real service with a text search for "password": an OpenAPI
decorator carried the account's example password on one line
(``password: { type: 'string', example: '...' }``) and the same value on the next
(``{ username: 'admin', password: '...' }``). The second shape was masked, the first
was not, so the search printed the value.
"""

from __future__ import annotations

import pytest

from src.security.mcp_redact import redact_for_mcp

#: Assembled at run time: no secret-looking literal in this file.
VALUE = "Qx7m" + "Pz4LwK2"


def _out(text: str) -> str:
    return redact_for_mcp(text)[0]


@pytest.mark.parametrize("text", [
    f"        password: {{ type: 'string', minLength: 1, example: '{VALUE}' }},",
    f'  "password": {{ "type": "string", "example": "{VALUE}" }},',
    f"  client_secret: {{ type: 'string', default: '{VALUE}' }}",
    f"properties:\n  password:\n    type: string\n    example: {VALUE}\n",
    f"password: {{\n  type: 'string',\n  default: '{VALUE}',\n}},\n",
    f"  apiToken:\n    type: string\n    example: \"{VALUE}\"\n",
])
def test_a_sample_on_a_secret_named_property_is_masked(text: str) -> None:
    assert VALUE not in _out(text)


@pytest.mark.parametrize("text", [
    "        username: { type: 'string', minLength: 1, example: 'admin' },",
    "limit: { type: 'number', default: 25 }",
    "sort_key: { default: 'created_at' }",
    "password: { type: 'string', example: '${DB_PASSWORD}' }",
    "password: { type: 'string', example: 'changeme' }",
    "properties:\n  name:\n    example: bob\n  password:\n    type: string\n",
    "token: { type: 'string', example: 'https://auth.example.com/token' }",
])
def test_samples_that_are_not_secrets_stay_readable(text: str) -> None:
    assert _out(text) == text


def test_the_block_form_stops_at_the_end_of_the_property() -> None:
    text = f"password:\n  type: string\nsummary:\n  example: {VALUE}\n"
    assert VALUE in _out(text), "an example under another key is not the password's"
