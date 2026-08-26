"""Unit tests для git_providers.py — parse, slug, URL building, redaction."""

from __future__ import annotations

import pytest

from src.sync.git_providers import (
    GitProvider,
    ParsedRepo,
    build_authenticated_url,
    build_clone_url,
    detect_provider,
    parse_repo_url,
    strip_credentials,
)

# ─── detect_provider ────────────────────────────────────────────────


class TestDetectProvider:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://github.com/foo/bar", GitProvider.GITHUB),
            ("https://github.com/foo/bar.git", GitProvider.GITHUB),
            ("https://github.com/foo/bar/tree/main", GitProvider.GITHUB),
            ("https://www.github.com/foo/bar", GitProvider.GITHUB),
            ("git@github.com:foo/bar.git", GitProvider.GITHUB),
            ("https://gitlab.com/foo/bar", GitProvider.GITLAB),
            ("https://gitlab.com/group/sub/repo", GitProvider.GITLAB),
            ("git@gitlab.com:group/repo.git", GitProvider.GITLAB),
            ("https://bitbucket.org/foo/bar", GitProvider.BITBUCKET),
            ("https://bitbucket.org/acme/frontend/src/dev/", GitProvider.BITBUCKET),
            ("git@bitbucket.org:foo/bar.git", GitProvider.BITBUCKET),
            ("github:foo/bar", GitProvider.GITHUB),
            ("gitlab:group/repo", GitProvider.GITLAB),
            ("bitbucket:foo/bar", GitProvider.BITBUCKET),
            # No prefix, no scheme — legacy default Bitbucket
            ("acme/frontend", GitProvider.BITBUCKET),
            # Unknown host → generic
            ("https://example.com/foo/bar", GitProvider.GENERIC),
            ("git@example.com:foo/bar.git", GitProvider.GENERIC),
        ],
    )
    def test_provider_detection(self, url: str, expected: GitProvider) -> None:
        assert detect_provider(url) == expected


# ─── parse_repo_url ─────────────────────────────────────────────────


class TestParseRepoUrl:
    def test_github_https_clone(self) -> None:
        r = parse_repo_url("https://github.com/pallets/click.git")
        assert r.provider == GitProvider.GITHUB
        assert r.owner == "pallets"
        assert r.name == "click"
        assert r.branch_hint is None

    def test_github_browser_url_with_branch(self) -> None:
        r = parse_repo_url("https://github.com/pallets/click/tree/main/src")
        assert r.provider == GitProvider.GITHUB
        assert r.owner == "pallets"
        assert r.name == "click"
        assert r.branch_hint == "main"

    def test_github_blob_url(self) -> None:
        r = parse_repo_url("https://github.com/pallets/click/blob/8.x/setup.py")
        assert r.branch_hint == "8.x"

    def test_github_ssh(self) -> None:
        r = parse_repo_url("git@github.com:pallets/click.git")
        assert r.provider == GitProvider.GITHUB
        assert r.owner == "pallets"
        assert r.name == "click"

    def test_github_explicit_prefix(self) -> None:
        r = parse_repo_url("github:pallets/click")
        assert r.provider == GitProvider.GITHUB
        assert r.owner == "pallets"
        assert r.name == "click"

    def test_bitbucket_browser_url_extracts_branch(self) -> None:
        r = parse_repo_url("https://bitbucket.org/acme/frontend/src/dev/path/file.ts")
        assert r.provider == GitProvider.BITBUCKET
        assert r.owner == "acme"
        assert r.name == "frontend"
        assert r.branch_hint == "dev"

    def test_bitbucket_legacy_slug(self) -> None:
        """Backward compat: 'owner/repo' без scheme → Bitbucket."""
        r = parse_repo_url("acme/frontend")
        assert r.provider == GitProvider.BITBUCKET
        assert r.owner == "acme"
        assert r.name == "frontend"

    def test_gitlab_simple(self) -> None:
        r = parse_repo_url("https://gitlab.com/foo/bar")
        assert r.provider == GitProvider.GITLAB
        assert r.owner == "foo"
        assert r.name == "bar"

    def test_gitlab_subgroups(self) -> None:
        """GitLab підтримує subgroups: group/subgroup/repo."""
        r = parse_repo_url("https://gitlab.com/mygroup/sub1/sub2/repo")
        assert r.provider == GitProvider.GITLAB
        assert r.owner == "mygroup/sub1/sub2"
        assert r.name == "repo"

    def test_gitlab_browser_url_with_dash_separator(self) -> None:
        """GitLab convention: /-/tree/branch separator."""
        r = parse_repo_url("https://gitlab.com/group/subgroup/repo/-/tree/main/src")
        assert r.owner == "group/subgroup"
        assert r.name == "repo"
        assert r.branch_hint == "main"

    def test_gitlab_ssh_subgroups(self) -> None:
        r = parse_repo_url("git@gitlab.com:mygroup/subgroup/repo.git")
        assert r.provider == GitProvider.GITLAB
        assert r.owner == "mygroup/subgroup"
        assert r.name == "repo"

    def test_invalid_slug_raises(self) -> None:
        with pytest.raises(ValueError, match="at least owner/name"):
            parse_repo_url("just-one-part")


# ─── slug ────────────────────────────────────────────────────────────


class TestSlug:
    def test_github_slug(self) -> None:
        r = ParsedRepo(provider=GitProvider.GITHUB, owner="pallets", name="click")
        assert r.slug == "github_pallets-click"

    def test_bitbucket_slug_no_prefix_for_backward_compat(self) -> None:
        """Bitbucket — legacy format без prefix (existing склоновані repos)."""
        r = ParsedRepo(provider=GitProvider.BITBUCKET, owner="acme", name="frontend")
        assert r.slug == "acme-frontend"

    def test_gitlab_subgroup_slug_flattens(self) -> None:
        """Subgroups flattened через '-' у локальному slug."""
        r = ParsedRepo(provider=GitProvider.GITLAB, owner="group/sub", name="repo")
        assert r.slug == "gitlab_group-sub-repo"

    def test_slug_is_collision_safe_across_providers(self) -> None:
        """pallets/click існує на github + (гіпотетично) на bitbucket — slug-и різні."""
        gh = ParsedRepo(provider=GitProvider.GITHUB, owner="pallets", name="click")
        bb = ParsedRepo(provider=GitProvider.BITBUCKET, owner="pallets", name="click")
        assert gh.slug != bb.slug


# ─── build_clone_url ─────────────────────────────────────────────────


class TestBuildCloneUrl:
    def test_github(self) -> None:
        r = ParsedRepo(provider=GitProvider.GITHUB, owner="pallets", name="click")
        assert build_clone_url(r) == "https://github.com/pallets/click.git"

    def test_bitbucket(self) -> None:
        r = ParsedRepo(provider=GitProvider.BITBUCKET, owner="acme", name="frontend")
        assert build_clone_url(r) == "https://bitbucket.org/acme/frontend.git"

    def test_gitlab_subgroups_preserved(self) -> None:
        r = ParsedRepo(provider=GitProvider.GITLAB, owner="group/sub", name="repo")
        assert build_clone_url(r) == "https://gitlab.com/group/sub/repo.git"


# ─── build_authenticated_url ─────────────────────────────────────────


class TestBuildAuthenticatedUrl:
    def test_github_token(self) -> None:
        r = ParsedRepo(provider=GitProvider.GITHUB, owner="foo", name="bar")
        url = build_authenticated_url(r, token="ghp_abc123")
        assert url == "https://x-access-token:ghp_abc123@github.com/foo/bar.git"

    def test_github_no_creds_falls_back_to_public(self) -> None:
        r = ParsedRepo(provider=GitProvider.GITHUB, owner="foo", name="bar")
        assert build_authenticated_url(r) == "https://github.com/foo/bar.git"

    def test_bitbucket_token_uses_atlassian_user(self) -> None:
        r = ParsedRepo(provider=GitProvider.BITBUCKET, owner="foo", name="bar")
        url = build_authenticated_url(r, token="ATATT3xFfGF0_token")
        assert url.startswith("https://x-bitbucket-api-token-auth:")
        assert "@bitbucket.org/foo/bar.git" in url

    def test_bitbucket_legacy_app_password(self) -> None:
        r = ParsedRepo(provider=GitProvider.BITBUCKET, owner="foo", name="bar")
        url = build_authenticated_url(r, username="user", password="apppwd")
        assert url == "https://user:apppwd@bitbucket.org/foo/bar.git"

    def test_gitlab_token_uses_oauth2(self) -> None:
        r = ParsedRepo(provider=GitProvider.GITLAB, owner="grp", name="repo")
        url = build_authenticated_url(r, token="glpat_abc")
        assert url == "https://oauth2:glpat_abc@gitlab.com/grp/repo.git"

    def test_special_chars_in_token_encoded(self) -> None:
        """Tokens з '/' or '+' мають url-encode'итись."""
        r = ParsedRepo(provider=GitProvider.GITHUB, owner="foo", name="bar")
        url = build_authenticated_url(r, token="abc/def+ghi")
        assert "abc%2Fdef%2Bghi" in url


# ─── strip_credentials ───────────────────────────────────────────────


class TestStripCredentials:
    def test_basic_auth_redacted(self) -> None:
        s = strip_credentials("https://user:pass@github.com/foo/bar.git")
        assert s == "https://[REDACTED]@github.com/foo/bar.git"

    def test_token_user_redacted(self) -> None:
        s = strip_credentials(
            "https://x-access-token:ghp_secret@github.com/foo/bar.git"
        )
        assert s == "https://[REDACTED]@github.com/foo/bar.git"

    def test_no_creds_unchanged(self) -> None:
        url = "https://github.com/foo/bar.git"
        assert strip_credentials(url) == url

    def test_log_message_with_creds(self) -> None:
        msg = "git clone failed url=https://user:secret@bitbucket.org/foo/bar.git stderr=auth"
        s = strip_credentials(msg)
        assert "secret" not in s
        assert "[REDACTED]" in s
