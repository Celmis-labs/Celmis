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
    assert f"manifest version {MANIFEST_VERSION}" in out


def test_read_manifest_raises_on_rubbish() -> None:
    with pytest.raises(PackError):
        read_manifest(b"not a zip")


# ─── the limit of a manifest that does not hash itself ───────────────
#
# `MANIFEST.json` records a hash for every other file and none for itself,
# because a file cannot contain its own hash. So recomputing the listed hashes
# proves the archive is internally CONSISTENT and nothing more. Anyone who
# knows the format can edit a file, write its new sha256 into the manifest and
# repack; measured against a real production pack, that verified as OK and
# exited 0.
#
# The fix is not inside the archive — it cannot be. It is the manifest's own
# hash, obtained from somewhere the sender does not control.


def _forge(field: str = "summary.md") -> bytes:
    """Edit a file and update its entry in the manifest, exactly as an attacker would."""
    import hashlib

    entries = _entries(_pack())
    entries[field] = entries[field] + b"\nquietly edited\n"
    manifest = json.loads(entries["MANIFEST.json"])
    manifest["files"][field] = hashlib.sha256(entries[field]).hexdigest()
    entries["MANIFEST.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()
    return _rebuild(entries)


def test_a_forged_pack_passes_without_the_manifest_hash() -> None:
    """Pinned deliberately. This is the limit, and it must not be forgotten.

    If this ever starts failing, somebody has made the pack self-authenticating
    and the wording everywhere else should be revisited — that would be good
    news, not a broken test.
    """
    ok, problems = verify_pack(_forge())
    assert ok is True, problems


def test_the_same_forgery_fails_against_the_real_manifest_hash() -> None:
    """And this is why the flag exists."""
    from celmis.verify import manifest_sha256

    genuine = manifest_sha256(_pack())
    ok, problems = verify_pack(_forge(), genuine)
    assert ok is False
    assert any("MANIFEST.json" in p and "sha256" in p for p in problems), problems
    assert any("proves nothing" in p for p in problems), problems


def test_an_untouched_pack_passes_against_its_own_hash() -> None:
    from celmis.verify import manifest_sha256

    ok, problems = verify_pack(_pack(), manifest_sha256(_pack()))
    assert ok is True, problems


def test_the_hash_is_reported_so_it_can_be_published(capsys) -> None:
    """You cannot compare a number nobody printed."""
    from celmis.verify import manifest_sha256

    assert main(["verify", str(FIXTURE)]) == EXIT_OK
    out = capsys.readouterr().out
    assert manifest_sha256(_pack()) in out
    assert "internally" in out and "consistent" in out, (
        "the OK line alone reads as 'genuine'; the caveat has to travel with it"
    )


def test_supplying_the_hash_changes_what_is_printed(capsys) -> None:
    from celmis.verify import manifest_sha256

    assert main(["verify", "--manifest-sha256", manifest_sha256(_pack()),
                 str(FIXTURE)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "matches the value you supplied" in out


def test_a_truncated_hash_is_a_usage_error_not_an_accusation(capsys) -> None:
    """Exit 2, not 1. Losing characters to a line wrap is not tampering."""
    from celmis.verify import manifest_sha256

    truncated = manifest_sha256(_pack())[:56]
    assert main(["verify", "--manifest-sha256", truncated, str(FIXTURE)]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "64 hex characters" in err
    assert "rather than the pack" in err


def test_a_hash_that_is_not_hex_is_also_a_usage_error() -> None:
    assert main(["verify", "--manifest-sha256", "z" * 64, str(FIXTURE)]) == EXIT_USAGE


def test_json_carries_the_hash_and_whether_it_was_checked(capsys) -> None:
    from celmis.verify import manifest_sha256

    assert main(["verify", "--json", str(FIXTURE)]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["manifest_sha256"] == manifest_sha256(_pack())
    assert payload["manifest_sha256_checked"] is False

    assert main(["verify", "--json", "--manifest-sha256", manifest_sha256(_pack()),
                 str(FIXTURE)]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["manifest_sha256_checked"] is True


# ─── an archive is somebody else's file ──────────────────────────────
#
# The person running this got the pack from the party they are checking, and
# is running it on their own laptop. A declared member size is therefore an
# instruction from an adversary about how much memory to allocate. Measured
# against celmis 0.2.0: 200 KB on disk declaring 200 MB verified as OK with a
# 215 MB peak, and the number was the sender's to choose.


def _bomb(declared_mb: int) -> bytes:
    entries = {
        "findings.json": b"[]\n",
        "sbom/x.cdx.json": b"A" * (declared_mb * 1024 * 1024),
    }
    manifest = {
        "manifest_version": 1, "algorithm": "sha256", "run_id": "bomb",
        "files": {n: __import__("hashlib").sha256(b).hexdigest()
                  for n, b in sorted(entries.items())},
    }
    entries["MANIFEST.json"] = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in sorted(entries):
            zf.writestr(name, entries[name])
    return buf.getvalue()


def test_a_decompression_bomb_is_refused_not_verified() -> None:
    from celmis.verify import MAX_MEMBER_BYTES

    blob = _bomb(64)
    assert len(blob) < 1024 * 1024, "the point is that it is small on disk"
    with pytest.raises(PackError) as caught:
        verify_pack(blob)
    assert str(MAX_MEMBER_BYTES) in str(caught.value)


def test_the_refusal_is_could_not_check_not_found_problems(tmp_path) -> None:
    """Exit 2. Refusing to read is not the same as reading and disliking."""
    bomb = tmp_path / "bomb.zip"
    bomb.write_bytes(_bomb(64))
    assert main(["verify", str(bomb)]) == EXIT_USAGE


def test_an_ordinary_pack_is_nowhere_near_the_limits() -> None:
    """The headroom is three orders of magnitude, not a squeeze."""
    from celmis.verify import MAX_UNCOMPRESSED_BYTES

    with zipfile.ZipFile(io.BytesIO(_pack())) as zf:
        total = sum(i.file_size for i in zf.infolist())
    assert total * 1000 < MAX_UNCOMPRESSED_BYTES, (
        f"a real pack is {total} bytes; the cap is {MAX_UNCOMPRESSED_BYTES}"
    )


def test_hashing_does_not_hold_a_member_whole() -> None:
    """Chunked, so a member that slips the size check still cannot be held.

    Read with ast: the function's own comment says `zf.read`, so a substring
    search would pass on a version that had gone back to reading it whole.
    """
    import ast
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[1] / "celmis" / "verify.py"
    tree = ast.parse(source.read_text("utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_sha256_member")
    calls = {n.func.attr for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "open" in calls, "_sha256_member no longer streams the member"
    assert "read" not in {c for c in calls if c == "read"} or "update" in calls


# ─── a manifest that is valid JSON and not an object ─────────────────


def _manifest_is(raw: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("MANIFEST.json", raw)
        zf.writestr("findings.json", b"[]\n")
    return buf.getvalue()


@pytest.mark.parametrize("body", [b'["a list"]\n', b'"a string"\n', b"42\n", b"null\n"])
def test_a_manifest_that_is_not_an_object_is_refused_clearly(body: bytes) -> None:
    with pytest.raises(PackError) as caught:
        verify_pack(_manifest_is(body))
    assert "not an object" in str(caught.value)


def test_a_files_key_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(PackError):
        verify_pack(_manifest_is(b'{"files": ["a", "b"]}\n'))


def test_json_output_does_not_traceback_on_it(tmp_path, capsys) -> None:
    """`--json` printed an AttributeError traceback and exited 1.

    Exit 1 tells a CI step the pack was checked and is wrong. It was not
    checked at all.
    """
    bad = tmp_path / "listmanifest.zip"
    bad.write_bytes(_manifest_is(b'["not an object"]\n'))

    assert main(["verify", "--json", str(bad)]) == EXIT_USAGE
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "AttributeError" not in captured.err
    assert "not an object" in captured.err


def test_both_output_modes_agree_on_the_verdict(tmp_path) -> None:
    """They disagreed: plain said problems, --json crashed. Same input."""
    bad = tmp_path / "listmanifest.zip"
    bad.write_bytes(_manifest_is(b'["not an object"]\n'))
    assert main(["verify", str(bad)]) == main(["verify", "--json", str(bad)]) == EXIT_USAGE


def test_an_oversized_archive_is_not_read(tmp_path) -> None:
    from celmis.verify import MAX_ARCHIVE_BYTES

    huge = tmp_path / "huge.zip"
    huge.write_bytes(b"\x00" * 1024)
    import os as _os

    real = _os.path.getsize

    def lying(path):
        return MAX_ARCHIVE_BYTES + 1 if str(path).endswith("huge.zip") else real(path)

    _os.path.getsize = lying
    try:
        assert main(["verify", str(huge)]) == EXIT_USAGE
    finally:
        _os.path.getsize = real


def test_many_medium_files_are_refused_by_their_total() -> None:
    """What `_guard_sizes` catches and per-member checking cannot.

    Each member sits under the per-file limit; together they are over the
    total. Removing the up-front guard leaves the per-member check, which
    passes every one of these individually and then reads them all.
    """
    from celmis.verify import MAX_MEMBER_BYTES, MAX_UNCOMPRESSED_BYTES

    each = MAX_MEMBER_BYTES // 2
    count = (MAX_UNCOMPRESSED_BYTES // each) + 2
    entries = {f"sbom/{i:03d}.cdx.json": b"A" * each for i in range(count)}
    assert all(len(b) < MAX_MEMBER_BYTES for b in entries.values())
    assert sum(len(b) for b in entries.values()) > MAX_UNCOMPRESSED_BYTES

    import hashlib

    manifest = {
        "manifest_version": 1, "algorithm": "sha256", "run_id": "spread",
        "files": {n: hashlib.sha256(b).hexdigest() for n, b in sorted(entries.items())},
    }
    entries["MANIFEST.json"] = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in sorted(entries):
            zf.writestr(name, entries[name])

    with pytest.raises(PackError) as caught:
        verify_pack(buf.getvalue())
    assert "uncompressed" in str(caught.value)
