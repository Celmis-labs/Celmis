"""Workspace dependency audit — the whole sweep, synchronous (job-thread).

For every registered repo in the workspace that has a local clone:

    scan manifests
      → native auditors (pip-audit / npm audit / govulncheck / …) where the
        tool exists in the image — these resolve the FULL tree, so they are
        the primary source
      → osv-scanner over the whole checkout — one binary for every ecosystem
        OSV knows (Java, .NET, Dart, Elixir, Swift, Conan, distro packages),
        which is how the languages without a native tool get audited at all
      → lock files for the transitive packages of any ecosystem neither of
        the above covered, fed to OSV as a fallback
      → hygiene checks (lock drift, install scripts, non-registry sources,
        lookalike names)
      → latest versions (concurrent)
      → per-package finding rows + a run summary

Three separations the summary keeps deliberately clean:

* **vulnerabilities vs version drift.** A CVE and "two minors behind" are
  different problems with different urgency, so `groups` (what to do) and
  `drift` (how far behind) are counted independently.
* **hygiene vs CVEs.** A `postinstall` script or a git-URL dependency is a
  provenance question, not an advisory — it never touches the vuln counters.
* **"nothing found" vs "not checked".** Every ecosystem we could not audit is
  listed with the reason. A silent zero is the one output this audit must
  never produce.

Recommendation rules (deterministic, explained in the UI):
    update_now   — a known vulnerability with a fixed release exists
    update_safe  — behind by patch/minor, no known vulnerability
    plan_major   — behind by a major version (breaking-change territory)
    ok           — current, no known vulnerabilities
    (a vulnerability with NO fixed release keeps update_now — the fixed_in
    column just stays empty so the human sees there's nothing to jump to yet)
"""

from __future__ import annotations

import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from src.deps.hygiene import check_repo as hygiene_check_repo
from src.deps.locks import mark_transitive, scan_locks
from src.deps.native import audit_repo as native_audit_repo
from src.deps.native import detect_tools
from src.deps.osv_scanner import (
    DEFAULT_TIMEOUT as _OSV_SCANNER_DEFAULT_TIMEOUT,
)
from src.deps.osv_scanner import (
    audit_repo as osv_scanner_audit_repo,
)
from src.deps.registries import (
    fetch_latest,
    fetch_vulns_batch,
    make_client,
    outdated_level,
    worst_severity,
)
from src.deps.scanner import scan_repo

logger = logging.getLogger(__name__)

_LATEST_CONCURRENCY = 12
# Heartbeat cadence. The registry sweep and the OSV sweep are the only steps
# that can run for minutes without changing phase — without a periodic write
# the run's updated_at goes stale and the UI cannot tell "slow" from "stuck".
_VERSION_HEARTBEAT_EVERY = 20
_VULN_CHUNK = 100

# Native auditors shell out to third-party tooling inside a cloned repo. That
# is exactly what makes them valuable and exactly why there is an off switch.
_NATIVE_ENABLED = os.getenv("DEPS_NATIVE_AUDIT", "1").lower() not in ("0", "false", "no")
_NATIVE_TIMEOUT = int(os.getenv("DEPS_NATIVE_TIMEOUT", "300") or 300)
# osv-scanner reads files and queries OSV — it never builds the project — so
# unlike the native auditors it is safe by default on an untrusted clone.
_OSV_SCANNER_ENABLED = os.getenv("DEPS_OSV_SCANNER", "1").lower() not in (
    "0", "false", "no")
# One invocation per repo, not per subproject: a monorepo with Maven modules
# spends most of it resolving remote POMs.
_OSV_SCANNER_TIMEOUT = int(
    os.getenv("DEPS_OSV_SCANNER_TIMEOUT", "") or _OSV_SCANNER_DEFAULT_TIMEOUT)
# Transitive packages sent to OSV per repo when no native tool covered the
# ecosystem. A big monorepo lock can hold 5 000 entries; the cap keeps one
# repo from monopolising the whole run.
_MAX_TRANSITIVE_PER_REPO = 600
_MAX_CHECK_ROWS = 200
_MAX_HYGIENE_ITEMS = 150


def run_audit(run_id: str, workspace_id: str, *,
              repo_slugs: list[str] | None = None,
              owner: str | None = None,
              branch: str | None = None,
              cancel_check=None) -> None:
    """Execute the audit and persist results. Raises on fatal errors only
    (per-repo and per-package problems degrade gracefully)."""
    from sqlalchemy import create_engine, insert, select, update
    from sqlalchemy.orm import Session

    from src.api.auto_review import get_auto_review_store
    from src.config import get_settings
    from src.db.models import DepAuditRun, DepFinding
    from src.db.session import get_database_url
    from src.sync.queue import JobCancelled

    settings = get_settings()
    engine = create_engine(
        get_database_url().replace("postgresql+asyncpg://", "postgresql+psycopg://"),
        pool_pre_ping=True,
    )

    def _set_run(**values) -> None:
        with Session(engine) as s:
            s.execute(update(DepAuditRun).where(DepAuditRun.id == run_id).values(
                updated_at=datetime.now(UTC), **values))
            s.commit()

    def _checkpoint() -> None:
        """Cooperative cancellation — two independent signals.

        1) the queue flag (cancel_check);
        2) the run row itself: the API sets its status='error' on «Cancel» /
           «Restart». The second signal is critical — the job could have been
           cancelled or evicted outside the queue (the flag gets reset in that
           case), and without it a zombie worker would have appended results
           to an already closed run.
        """
        if cancel_check is not None and cancel_check():
            _set_run(status="error", error="cancelled by user")
            raise JobCancelled("dependency audit cancelled by user")
        with Session(engine) as s:
            row = s.execute(
                select(DepAuditRun.status).where(DepAuditRun.id == run_id)
            ).first()
        if row is None:
            raise JobCancelled("dependency audit run row is gone")
        if row[0] not in ("queued", "running"):
            raise JobCancelled(f"dependency audit cancelled (run is {row[0]})")

    def _phase(phase: str, **extra) -> None:
        # Every phase change is a natural checkpoint for cooperative cancellation.
        _checkpoint()
        _set_run(status="running", summary={"phase": phase, **extra})

    try:
        _phase("starting")

        repos = [c for c in get_auto_review_store().list_all()
                 if c.workspace_id == workspace_id]
        # Optional scoping: explicit slugs and/or an owner ("owner/…") prefix —
        # e.g. audit every repo of one Bitbucket user in one click.
        if repo_slugs:
            wanted = set(repo_slugs)
            repos = [c for c in repos if c.repo_slug in wanted]
        if owner:
            low = owner.lower().rstrip("/")
            repos = [c for c in repos if c.full_name.lower().startswith(low + "/")]
        scanned: list[str] = []
        audited_commits: dict[str, str] = {}
        skipped: list[str] = []
        skip_reasons: dict[str, str] = {}
        all_deps: list[tuple[str, object]] = []            # (repo_slug, DeclaredDep)
        native_hits: list[tuple[str, object]] = []         # (repo_slug, NativeFinding)
        native_checks: list[dict] = []
        transitive_candidates: list[tuple[str, object]] = []  # (repo_slug, LockEntry)
        hygiene_items: list[dict] = []
        hygiene_by_kind: dict[str, int] = {}

        for i, cfg in enumerate(repos):
            _phase("scanning", repos_done=i, repos_total=len(repos), current=cfg.repo_slug)
            repo_path = settings.repo_path(cfg.repo_slug)
            wants_branch = (branch or "").strip()
            if not repo_path.exists():
                # Registered but never indexed — clone on demand (shallow) so
                # the audit "just works" instead of silently skipping the repo.
                _phase("cloning", repos_done=i, repos_total=len(repos), current=cfg.repo_slug)
                try:
                    _clone_for_audit(cfg, workspace_id, branch=branch)
                except Exception as exc:  # noqa: BLE001
                    reason = str(exc).strip().splitlines()[0][:200] or "clone failed"
                    logger.warning("dep_audit_clone_failed repo=%s err=%s",
                                   cfg.repo_slug, reason)
                    skipped.append(cfg.repo_slug)
                    skip_reasons[cfg.repo_slug] = reason
                    continue
            elif wants_branch:
                # An existing clone sits on whatever branch it was last synced
                # to, and the manifests are read straight off disk — so without
                # this the override was inert for every indexed repo, which is
                # all of them after the first audit. The run would report on one
                # branch while the UI named another, and nothing would say so.
                #
                # Only for an EXPLICIT per-run override: the fetch costs a round
                # trip per repo, and a plain audit must stay as cheap as it is.
                _phase("cloning", repos_done=i, repos_total=len(repos), current=cfg.repo_slug)
                try:
                    _clone_for_audit(cfg, workspace_id, branch=wants_branch)
                except Exception as exc:  # noqa: BLE001
                    # Skipping is the honest outcome. Auditing the branch that
                    # happens to be checked out, under a heading that says
                    # "develop", is the silent wrong answer this exists to stop.
                    reason = (str(exc).strip().splitlines()[0][:200]
                              or f"could not switch to branch {wants_branch}")
                    logger.warning("dep_audit_branch_switch_failed repo=%s branch=%s err=%s",
                                   cfg.repo_slug, wants_branch, reason)
                    skipped.append(cfg.repo_slug)
                    skip_reasons[cfg.repo_slug] = reason
                    continue
                if not repo_path.exists():
                    skipped.append(cfg.repo_slug)
                    skip_reasons[cfg.repo_slug] = "clone failed (no path after clone)"
                    continue
            deps = scan_repo(repo_path)
            scanned.append(cfg.repo_slug)
            # The sha of the tree we just READ, not the one the graph was
            # indexed at. The SBOM derives its serialNumber from the commit
            # precisely so two code states are two documents, and it was being
            # handed `last_indexed_sha` — a different fact about a different
            # process. Audit a branch and you got a document describing the
            # branch, stamped with main's sha and main's serial: two
            # contradictory SBOMs that a consumer de-duplicating by serial
            # treats as one. Observed with a fix branch that had zero
            # vulnerabilities colliding with the pre-fix document that had 38.
            audited_commits[cfg.repo_slug] = _head_sha(repo_path)
            all_deps.extend((cfg.repo_slug, d) for d in deps)
            logger.info("dep_audit_scanned repo=%s deps=%d", cfg.repo_slug, len(deps))

            # ── Native auditors: the real tree, where the tool exists.
            covered: set[str] = set()
            if _NATIVE_ENABLED:
                _phase("native_audit", repos_done=i, repos_total=len(repos),
                       current=cfg.repo_slug)
                try:
                    native = native_audit_repo(
                        repo_path, declared=deps, timeout=_NATIVE_TIMEOUT,
                        # A heartbeat per tool invocation: `pip-audit -r` can
                        # resolve for minutes, and without a progress write the
                        # UI would declare a perfectly healthy run stuck. This
                        # is also the cancellation checkpoint (it raises).
                        on_progress=lambda tool, sub, _i=i, _cfg=cfg: _phase(
                            "native_audit", repos_done=_i, repos_total=len(repos),
                            current=f"{_cfg.repo_slug}{'/' + sub if sub else ''}",
                            tool=tool),
                    )
                except JobCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("dep_audit_native_failed repo=%s err=%s",
                                   cfg.repo_slug, exc)
                else:
                    covered = native.covered()
                    native_hits.extend((cfg.repo_slug, f) for f in native.findings)
                    native_checks.extend(
                        {"repo": cfg.repo_slug, **c.as_dict()} for c in native.checks)
                    logger.info("dep_audit_native repo=%s findings=%d checked=%s",
                                cfg.repo_slug, len(native.findings), sorted(covered))

            # ── osv-scanner: one binary, every ecosystem OSV knows. This is
            # what audits the languages no native tool here covers — Java,
            # .NET, Dart, Elixir, Swift, Conan, distro packages — and it
            # double-covers the rest, where the shared-identifier merge folds
            # the overlap back into one advisory per problem.
            if _OSV_SCANNER_ENABLED:
                _phase("osv_scanner", repos_done=i, repos_total=len(repos),
                       current=cfg.repo_slug)
                try:
                    scanned_by_osv = osv_scanner_audit_repo(
                        repo_path, declared=deps, timeout=_OSV_SCANNER_TIMEOUT,
                        on_progress=lambda tool, sub, _i=i, _cfg=cfg: _phase(
                            "osv_scanner", repos_done=_i, repos_total=len(repos),
                            current=_cfg.repo_slug, tool=tool),
                    )
                except JobCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("dep_audit_osv_scanner_failed repo=%s err=%s",
                                   cfg.repo_slug, exc)
                else:
                    covered |= scanned_by_osv.covered()
                    native_hits.extend((cfg.repo_slug, f) for f in scanned_by_osv.findings)
                    native_checks.extend(
                        {"repo": cfg.repo_slug, **c.as_dict()} for c in scanned_by_osv.checks)

            # ── Hygiene: provenance problems no CVE feed reports.
            _phase("hygiene", repos_done=i, repos_total=len(repos), current=cfg.repo_slug)
            try:
                for item in hygiene_check_repo(repo_path):
                    hygiene_by_kind[item.kind] = hygiene_by_kind.get(item.kind, 0) + 1
                    if len(hygiene_items) < _MAX_HYGIENE_ITEMS:
                        hygiene_items.append({"repo": cfg.repo_slug, **item.as_dict()})
            except Exception as exc:  # noqa: BLE001
                logger.warning("dep_audit_hygiene_failed repo=%s err=%s", cfg.repo_slug, exc)

            # ── Lock files: transitive packages for the ecosystems that had
            # no native tool. This is the gap manifests alone cannot close —
            # a CVE in a dependency-of-a-dependency was previously invisible.
            try:
                direct_names: dict[str, set[str]] = {}
                for d in deps:
                    direct_names.setdefault(d.ecosystem, set()).add(d.package.lower())
                entries = mark_transitive(scan_locks(repo_path), direct_names)
            except Exception as exc:  # noqa: BLE001
                logger.warning("dep_audit_locks_failed repo=%s err=%s", cfg.repo_slug, exc)
                entries = []
            picked: set[tuple[str, str, str]] = set()
            for entry in entries:
                if entry.ecosystem in covered or not entry.transitive or not entry.version:
                    continue
                key = (entry.ecosystem, entry.package, entry.version)
                if key in picked:
                    continue
                picked.add(key)
                transitive_candidates.append((cfg.repo_slug, entry))
                if len(picked) >= _MAX_TRANSITIVE_PER_REPO:
                    break

        # ── Latest versions: one lookup per unique (eco, pkg), concurrent.
        unique: dict[tuple[str, str], str] = {}
        for _, d in all_deps:
            unique.setdefault((d.ecosystem, d.package), d.version)

        _phase("fetching_versions", packages=len(unique), done=0, total=len(unique))
        latest: dict[tuple[str, str], str | None] = {}
        with make_client() as client:
            with ThreadPoolExecutor(max_workers=_LATEST_CONCURRENCY) as pool:
                futures = {
                    pool.submit(fetch_latest, client, eco, pkg): (eco, pkg)
                    for (eco, pkg) in unique
                }
                done = 0
                try:
                    for fut in as_completed(futures):
                        key = futures[fut]
                        try:
                            latest[key] = fut.result()
                        except Exception:  # noqa: BLE001
                            latest[key] = None
                        done += 1
                        if done % _VERSION_HEARTBEAT_EVERY == 0 or done == len(futures):
                            _phase("fetching_versions", packages=len(unique),
                                   done=done, total=len(futures))
                except BaseException:
                    # Cancelled (or crashed) mid-sweep: don't let the pool's
                    # __exit__ block on the queued-but-unstarted lookups.
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise

            # ── Vulnerabilities: OSV batch over unique (eco, pkg, version)
            # triples — different repos may pin different versions. Chunked
            # here (not just inside the client) so each chunk is a checkpoint
            # and a progress heartbeat.
            vuln_keys = sorted(
                {(d.ecosystem, d.package, d.version) for _, d in all_deps}
                | {(e.ecosystem, e.package, e.version)
                   for _, e in transitive_candidates})
            _phase("checking_vulnerabilities", packages=len(vuln_keys),
                   done=0, total=len(vuln_keys))
            vulns: dict[tuple[str, str, str], list[dict]] = {}
            vuln_failed_batches = 0
            for i in range(0, len(vuln_keys), _VULN_CHUNK):
                chunk = vuln_keys[i:i + _VULN_CHUNK]
                part, failed = fetch_vulns_batch(client, chunk)
                vulns.update(part)
                vuln_failed_batches += failed
                _phase("checking_vulnerabilities", packages=len(vuln_keys),
                       done=min(i + len(chunk), len(vuln_keys)), total=len(vuln_keys))

        # ── Build finding rows.
        #
        # One row per (repo, ecosystem, package) — a package can be reported by
        # more than one source (declared in the manifest, found by pip-audit,
        # found again via the lock file), and three rows for one package is a
        # worse answer than one row carrying three advisories.
        rows_by_key: dict[tuple[str, str, str], dict] = {}

        def _row(repo_slug: str, ecosystem: str, package: str, version: str,
                 *, is_dev: bool, transitive: bool) -> dict:
            key = (repo_slug, ecosystem, package)
            row = rows_by_key.get(key)
            if row is None:
                row = {
                    "id": str(uuid.uuid4()),
                    "run_id": run_id,
                    "repo_slug": repo_slug,
                    "ecosystem": ecosystem,
                    "package": package,
                    "current_version": version or "?",
                    "latest_version": None,
                    # "unknown", never "none". A row nobody resolved a latest
                    # version for used to default to "none", which the table
                    # and the exported document both render as "up to date":
                    # `idna 2.10 -> ? (PyPI, none)` on a package two majors
                    # behind. The aggregate histogram was already honest —
                    # its own comment says "'unknown' is not 'up to date'" —
                    # and the row it was built from was not.
                    "outdated": "unknown",
                    "is_dev": is_dev,
                    "vulns": [],
                    "severity": "none",
                    "recommendation": "ok",
                    "_transitive": transitive,
                }
                rows_by_key[key] = row
                return row
            if row["current_version"] in ("", "?") and version:
                row["current_version"] = version
            # Declared beats derived on both flags: a package that is a direct
            # production dependency anywhere is not "dev-only" or "transitive".
            row["is_dev"] = row["is_dev"] and is_dev
            row["_transitive"] = row["_transitive"] and transitive
            return row

        # 1. Declared (direct) dependencies — drift + OSV advisories.
        for repo_slug, d in all_deps:
            row = _row(repo_slug, d.ecosystem, d.package, d.version,
                       is_dev=d.is_dev, transitive=False)
            lat = latest.get((d.ecosystem, d.package))
            row["latest_version"] = lat
            row["outdated"] = outdated_level(d.version, lat)
            for vuln in vulns.get((d.ecosystem, d.package, d.version), []):
                merge_vuln(row["vulns"], {**vuln, "transitive": False, "is_dev": d.is_dev})

        # 2. Transitive packages from lock files (only where OSV had to stand
        #    in for a missing native tool) — reported only when vulnerable,
        #    otherwise a monorepo would drown the table in clean rows.
        for repo_slug, entry in transitive_candidates:
            found = vulns.get((entry.ecosystem, entry.package, entry.version), [])
            if not found:
                continue
            row = _row(repo_slug, entry.ecosystem, entry.package, entry.version,
                       is_dev=entry.is_dev, transitive=True)
            for vuln in found:
                merge_vuln(row["vulns"], {**vuln, "transitive": True, "is_dev": entry.is_dev})

        # 3. Native auditor + osv-scanner findings — authoritative, and the
        #    only sources that see transitive packages for the ecosystems they
        #    cover.
        for repo_slug, finding in native_hits:
            row = _row(repo_slug, finding.ecosystem, finding.package, finding.version,
                       is_dev=finding.is_dev, transitive=finding.transitive)
            merge_vuln(row["vulns"], finding.to_vuln())

        rows: list[dict] = []
        n_outdated = 0
        n_vulnerable = 0
        n_transitive = 0
        n_transitive_vulnerable = 0
        by_sev = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        # Version drift, counted independently of vulnerabilities: a vulnerable
        # package that is ALSO three majors behind used to show up only as
        # "update now", hiding the upgrade cost from whoever plans the work.
        drift = {"major": 0, "minor": 0, "patch": 0, "none": 0, "unknown": 0}
        groups = {"update_now": 0, "plan_major": 0, "update_safe": 0,
                  "ok": 0, "unknown": 0}
        sources: dict[str, int] = {}

        for row in rows_by_key.values():
            transitive = bool(row.pop("_transitive"))
            vlist = row["vulns"]
            row["severity"] = worst_severity(vlist)
            # Every advisory carried without its full record: OSV confirmed
            # they apply, but nothing read their severity, and `worst_severity`
            # grades an unreadable one as "none". Reporting "none" here would
            # be the silent zero this whole module exists to refuse — the same
            # mistake as calling an unscanned ecosystem clean. Say unknown.
            if vlist and all(v.get("detail_unavailable") for v in vlist):
                row["severity"] = "unknown"
            # patch/minor/major only. `!= "none"` was right while "none" was
            # the sole non-drift value; now that an unresolvable row says
            # "unknown", counting it as outdated would inflate the headline
            # with packages nobody could look up — the mirror of the bug just
            # fixed, and the drift histogram keeps them in their own bucket.
            if row["outdated"] in ("patch", "minor", "major"):
                n_outdated += 1
            if vlist:
                n_vulnerable += 1
                if row["severity"] in by_sev:
                    by_sev[row["severity"]] += 1
                for vuln in vlist:
                    src = str(vuln.get("source") or "osv")
                    sources[src] = sources.get(src, 0) + 1
            if transitive:
                n_transitive += 1
                if vlist:
                    n_transitive_vulnerable += 1
            if vlist:
                row["recommendation"] = "update_now"
            elif row["outdated"] == "major":
                row["recommendation"] = "plan_major"
            elif row["outdated"] in ("minor", "patch"):
                row["recommendation"] = "update_safe"
            elif row["outdated"] == "unknown":
                # Not "ok". "ok" reads as nothing to do, and this is a package
                # whose latest version nobody could resolve — the reader has
                # to decide, and cannot if the row says it is fine.
                row["recommendation"] = "unknown"
            else:
                row["recommendation"] = "ok"
            groups[row["recommendation"]] += 1
            # "unknown" is not "up to date": a transitive package has no latest
            # lookup, and pretending it is current would be the same silent
            # zero this audit exists to avoid.
            drift["unknown" if row["latest_version"] is None else row["outdated"]] += 1
            rows.append(row)

        _phase("saving", findings=len(rows))
        with Session(engine) as s:
            if rows:
                s.execute(insert(DepFinding), rows)
            s.commit()

        # Last checkpoint: a cancel that landed while we were saving must not
        # be overwritten by a "done" a moment later.
        _checkpoint()
        not_checked = [c for c in native_checks if c.get("status") != "checked"]
        _set_run(status="done", summary={
            "repos_total": len(repos),
            "repos_scanned": len(scanned),
            # The slugs, not just how many. The delta needs to tell "somebody
            # fixed this" from "we stopped looking at that repository", and
            # the count cannot answer it. Findings cannot either: a repo that
            # is still in scope and now perfectly clean produces none, so
            # inferring scope from findings files its genuine fixes under "no
            # longer audited". Without this list a real run reported "493
            # resolved" when the true number was zero.
            "repos_scanned_slugs": sorted(scanned),
            #: slug → the commit this run actually read. Consumed by the SBOM
            #: and the evidence pack so both describe the state that was
            #: audited rather than the state that was last indexed.
            "audited_commits": dict(audited_commits),
            "repos_skipped": skipped,
            "skip_reasons": skip_reasons,
            "packages": len(rows),
            "unique_packages": len(unique),
            "outdated": n_outdated,
            "vulnerable": n_vulnerable,
            "by_severity": by_sev,
            # OSV availability: non-zero means "vulnerable=0" may be a lie —
            # the UI shows a warning instead of a clean bill of health.
            "vuln_check_errors": vuln_failed_batches,
            # ── Provenance and coverage ─────────────────────────────
            # Which tool produced each advisory, which ecosystems a native
            # auditor actually covered, and — the important half — which ones
            # nobody checked and why.
            "sources": sources,
            "native_enabled": _NATIVE_ENABLED,
            "native_tools": {name: bool(path) for name, path in detect_tools().items()},
            "native_checks": native_checks[:_MAX_CHECK_ROWS],
            "not_checked": not_checked[:_MAX_CHECK_ROWS],
            "transitive": n_transitive,
            "transitive_vulnerable": n_transitive_vulnerable,
            # ── Version drift, kept apart from vulnerabilities ──────
            "drift": drift,
            "groups": groups,
            # ── Supply-chain hygiene, kept apart from CVEs ──────────
            "hygiene": {
                "total": sum(hygiene_by_kind.values()),
                "by_kind": hygiene_by_kind,
                "items": hygiene_items,
            },
        })
        logger.info(
            "dep_audit_done run=%s ws=%s repos=%d pkgs=%d outdated=%d vulnerable=%d "
            "transitive=%d hygiene=%d sources=%s",
            run_id, workspace_id, len(scanned), len(rows), n_outdated, n_vulnerable,
            n_transitive, sum(hygiene_by_kind.values()), sources,
        )
    except JobCancelled:
        # Not a failure: keep the human-readable reason the canceller wrote
        # instead of overwriting it with the internal exception text.
        logger.info("dep_audit_cancelled run=%s ws=%s", run_id, workspace_id)
        raise
    except Exception as exc:
        logger.exception("dep_audit_failed run=%s", run_id)
        _set_run(status="error", error=str(exc)[:1000])
        raise
    finally:
        engine.dispose()


def vuln_idents(vuln: dict) -> set[str]:
    """Every id one advisory answers to, lowercased."""
    ids = [vuln.get("id"), vuln.get("cve"), *(vuln.get("aliases") or ())]
    return {str(v).lower() for v in ids if v}


def merge_vuln(vulns: list[dict], vuln: dict) -> None:
    """Add an advisory to a row's list, deduplicated across sources.

    Two engines routinely describe the same advisory under different primary
    ids (pip-audit says PYSEC-2023-192, OSV says GHSA-g4mx-q9vg-27p4,
    osv-scanner may say either) while agreeing on the CVE or listing each
    other's id as an alias. Matching on *any* shared identifier is what keeps
    one problem from being counted — and displayed, and reported to the
    model — twice.
    """
    incoming = vuln_idents(vuln)
    for existing in vulns:
        if not (incoming & vuln_idents(existing)):
            continue
        # Keep whichever record actually knows the useful facts.
        for field in ("fixed_in", "cve", "url", "summary"):
            if not existing.get(field) and vuln.get(field):
                existing[field] = vuln[field]
        # The union keeps the merged record matchable by a third source that
        # only knows one of the ids.
        merged = {str(a) for a in
                  (*(existing.get("aliases") or ()), *(vuln.get("aliases") or ()),
                   existing.get("id"), existing.get("cve"),
                   vuln.get("id"), vuln.get("cve")) if a}
        merged.discard(str(existing.get("id") or ""))
        existing["aliases"] = sorted(merged)
        return
    vulns.append(vuln)



def _head_sha(repo_path) -> str:
    """HEAD of the clone as it stands on disk right now, or "".

    Best effort: an SBOM with an empty commit is what shipped for months, so a
    failure here degrades to that rather than failing the audit.
    """
    import subprocess
    try:
        r = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _clone_for_audit(cfg, workspace_id: str, *, branch: str | None = None) -> None:
    """Put the local clone on the branch this run should read.

    Clones when there is no local copy, and switches an existing one when a
    per-run branch was named — `clone_or_update` handles both, including the
    extra fetch a `--single-branch` clone needs before it can check out a
    branch it has never seen.

    Uses the same credential chain as the rest of the app (workspace slot
    first). `branch` overrides the registration's branch for this run only.
    """
    from src.credentials.git_keys import resolve_git_credential
    from src.sync.clone import RepoSync

    kwargs: dict[str, str] = {}
    creds = resolve_git_credential(
        cfg.provider, user_id=cfg.user_id, workspace_id=workspace_id,
    )
    if creds is not None:
        from src.credentials.git_auth import git_auth_kwargs
        kwargs = git_auth_kwargs(cfg.provider, creds.secret, creds.metadata)

    # Audit the branch the repo is configured for — otherwise the manifest
    # scan reads the default branch while everything else indexes `dev`. A
    # per-run override wins, so "check develop before we merge" needs no
    # permanent change to the registration.
    wanted = (branch or "").strip() or getattr(cfg, "branch", None) or None
    RepoSync().clone_or_update(
        cfg.url, branch=wanted, user_id=cfg.user_id, **kwargs,
    )

    # Verify rather than assume. A branch that no longer exists on the remote
    # does not fail the sync — clone_or_update deliberately keeps whatever the
    # clone is already on, so a stale registration cannot break indexing. For
    # an audit that same leniency is a silent wrong answer: the report would be
    # headed "develop" and computed from main. Raising here routes the repo
    # into the run's skip list, where it is visible.
    if wanted:
        from src.config import get_settings as _settings
        on = _checked_out_branch(_settings().repo_path(cfg.repo_slug))
        if on is not None and on != wanted:
            raise RuntimeError(f"branch {wanted!r} not available (clone is on {on!r})")


def _checked_out_branch(path) -> str | None:
    """Branch name from .git/HEAD, or None when it cannot be read.

    None means "do not judge" — a detached HEAD or an unreadable file is not
    evidence that the wrong branch is checked out.
    """
    try:
        head = (path / ".git" / "HEAD").read_text().strip()
    except OSError:
        return None
    return head[16:] if head.startswith("ref: refs/heads/") else None


__all__ = ["merge_vuln", "run_audit", "vuln_idents"]
