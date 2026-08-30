"""What the verifier must say, against a pack the producer actually made.

The fixture is `build_evidence_pack()`'s own output, committed rather than
hand-written: a hand-made zip would test this file against itself.
"""

from __future__ import annotations

import io
import json
import pathlib
import zipfile

import pytest

from celmis.cli import EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, main
from celmis.verify import MANIFEST_VERSION, PackError, read_manifest, verify_pack

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "evidence-pack-v1.zip"


def _pack() -> bytes:
    return FIXTURE.read_bytes()


def _rebuild(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(entries):
            zf.writestr(name, entries[name])
    return buf.getvalue()


def _entries(blob: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


# ─── the three verdicts ──────────────────────────────────────────────


def test_an_untouched_pack_verifies() -> None:
    ok, problems = verify_pack(_pack())
    assert ok is True, problems


def test_a_changed_byte_is_caught_and_the_file_named() -> None:
    entries = _entries(_pack())
    victim = "findings.json"
    entries[victim] = entries[victim] + b" "
    ok, problems = verify_pack(_rebuild(entries))
    assert ok is False
    assert any(victim in p and "mismatch" in p for p in problems), problems


def test_a_missing_file_is_not_the_same_as_a_changed_one() -> None:
    entries = _entries(_pack())
    del entries["summary.md"]
    ok, problems = verify_pack(_rebuild(entries))
    assert ok is False
    assert any("summary.md" in p and "absent" in p for p in problems), problems


def test_an_added_file_is_a_problem_too() -> None:
    """Content the manifest does not vouch for is content nobody signed."""
    entries = _entries(_pack())
    entries["extra.txt"] = b"slipped in\n"
    ok, problems = verify_pack(_rebuild(entries))
    assert ok is False
    assert any("extra.txt" in p and "not in the manifest" in p for p in problems)


def test_a_pack_with_no_manifest_says_so() -> None:
    entries = _entries(_pack())
    del entries["MANIFEST.json"]
    ok, problems = verify_pack(_rebuild(entries))
    assert ok is False
    assert problems == ["MANIFEST.json is missing"]


def test_something_that_is_not_a_zip_is_not_a_crash() -> None:
    ok, problems = verify_pack(b"this is not a zip file")
    assert ok is False
    assert problems and problems[0].startswith("unreadable archive")


# ─── too old is not the same as tampered ─────────────────────────────


def test_a_newer_format_asks_for_an_upgrade_rather_than_accusing() -> None:
    entries = _entries(_pack())
    manifest = json.loads(entries["MANIFEST.json"])
    manifest["manifest_version"] = MANIFEST_VERSION + 1
    entries["MANIFEST.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()

    ok, problems = verify_pack(_rebuild(entries))
    assert ok is False
    joined = " ".join(problems)
    assert "upgrade the verifier" in joined
    for accusation in ("mismatch", "altered", "not in the manifest"):
        assert accusation not in joined, (
            f"a newer format was reported as tampering: {problems}"
        )


def test_a_pack_predating_the_field_is_version_one() -> None:
    entries = _entries(_pack())
    manifest = json.loads(entries["MANIFEST.json"])
    del manifest["manifest_version"]
    entries["MANIFEST.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()
    ok, problems = verify_pack(_rebuild(entries))
    assert ok is True, problems


def test_a_version_that_is_not_a_number_is_refused_clearly() -> None:
    entries = _entries(_pack())
    manifest = json.loads(entries["MANIFEST.json"])
    manifest["manifest_version"] = "yesterday"
    entries["MANIFEST.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()
    ok, problems = verify_pack(_rebuild(entries))
    assert ok is False
    assert "not a version number" in " ".join(problems)


# ─── the command surface, including its exit codes ───────────────────


def test_verify_exits_zero_on_a_good_pack(capsys) -> None:
    assert main(["verify", str(FIXTURE)]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("OK — ")
    assert "run-fixture-0001" in out


def test_verify_exits_one_on_a_bad_pack(tmp_path, capsys) -> None:
    """1 is "I checked and found problems". 2 would say "I could not check"."""
    entries = _entries(_pack())
    entries["findings.json"] = entries["findings.json"] + b" "
    bad = tmp_path / "bad.zip"
    bad.write_bytes(_rebuild(entries))

    assert main(["verify", str(bad)]) == EXIT_PROBLEMS
    err = capsys.readouterr().err
    assert "findings.json" in err, err


def test_a_missing_file_exits_two(capsys) -> None:
    assert main(["verify", "/nonexistent/pack.zip"]) == EXIT_USAGE
    assert "cannot read" in capsys.readouterr().err


def test_json_output_is_machine_readable(capsys) -> None:
    assert main(["verify", "--json", str(FIXTURE)]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["problems"] == []
    assert payload["algorithm"] == "sha256"
    assert payload["manifest_version"] == MANIFEST_VERSION
    assert payload["run_id"] == "run-fixture-0001"
    assert payload["files"] == len(read_manifest(_pack())["files"])


def test_json_output_survives_a_bad_pack(tmp_path, capsys) -> None:
    entries = _entries(_pack())
    entries["findings.json"] = entries["findings.json"] + b" "
    bad = tmp_path / "bad.zip"
    bad.write_bytes(_rebuild(entries))

    assert main(["verify", "--json", str(bad)]) == EXIT_PROBLEMS
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert any("findings.json" in p for p in payload["problems"])


def test_show_prints_the_manifest(capsys) -> None:
    assert main(["show", str(FIXTURE)]) == EXIT_OK
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["algorithm"] == "sha256"
    assert "files" in manifest


def test_show_summary_prints_the_cover_sheet(capsys) -> None:
    assert main(["show", "--summary", str(FIXTURE)]) == EXIT_OK
    assert capsys.readouterr().out.strip(), "summary.md came out empty"


def test_show_summary_on_a_pack_without_one_is_a_usage_error(tmp_path, capsys) -> None:
    entries = _entries(_pack())
    del entries["summary.md"]
    stripped = tmp_path / "nosummary.zip"
    stripped.write_bytes(_rebuild(entries))
    assert main(["show", "--summary", str(stripped)]) == EXIT_USAGE


def test_no_arguments_says_this_is_not_the_platform(capsys) -> None:
    """The first thing somebody who typed `pip install celmis` has to learn."""
    assert main([]) == EXIT_OK
    out = capsys.readouterr().out
    assert "NOT THE CELMIS PLATFORM" in out
    assert "docker compose up -d" in out


def test_version_names_the_format_it_understands(capsys) -> None:
    with pytest.raises(SystemExit) as exit_:
        main(["--version"])
    assert exit_.value.code == 0
    out = capsys.readouterr().out
    assert "manifest version {0}".format(MANIFEST_VERSION) in out


def test_read_manifest_raises_on_rubbish() -> None:
    with pytest.raises(PackError):
        read_manifest(b"not a zip")
