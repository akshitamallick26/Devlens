import pytest

from app.ingestion.url_policy import (
    EmbeddedCredentialsError,
    MalformedRepositoryPathError,
    PrivateNetworkHostError,
    RepositoryIdentity,
    RepositoryURLError,
    UnsupportedHostError,
    UnsupportedSchemeError,
    UnsupportedURLComponentError,
    normalize_repository_url,
)


def test_canonical_github_url() -> None:
    identity = normalize_repository_url("https://github.com/octocat/Hello-World")
    assert identity == RepositoryIdentity(
        host="github.com",
        owner="octocat",
        name="Hello-World",
        canonical_url="https://github.com/octocat/Hello-World",
        clone_url="https://github.com/octocat/Hello-World.git",
    )


def test_dot_git_suffix_normalizes_to_the_same_identity() -> None:
    with_suffix = normalize_repository_url("https://github.com/octocat/Hello-World.git")
    without_suffix = normalize_repository_url("https://github.com/octocat/Hello-World")
    assert with_suffix == without_suffix


def test_trailing_slash_normalizes_to_the_same_identity() -> None:
    with_slash = normalize_repository_url("https://github.com/octocat/Hello-World/")
    without_slash = normalize_repository_url("https://github.com/octocat/Hello-World")
    assert with_slash == without_slash


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/octocat/Hello-World",
        "ftp://github.com/octocat/Hello-World",
        "git://github.com/octocat/Hello-World",
        "ssh://github.com/octocat/Hello-World",
        "file:///etc/passwd",
    ],
)
def test_unsupported_scheme_is_rejected(url: str) -> None:
    with pytest.raises(UnsupportedSchemeError):
        normalize_repository_url(url)


def test_embedded_credentials_are_rejected() -> None:
    with pytest.raises(EmbeddedCredentialsError):
        normalize_repository_url("https://user:pass@github.com/octocat/Hello-World")


def test_embedded_username_only_is_rejected() -> None:
    with pytest.raises(EmbeddedCredentialsError):
        normalize_repository_url("https://token@github.com/octocat/Hello-World")


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/octocat/Hello-World",
        "https://127.0.0.1/octocat/Hello-World",
        "https://0.0.0.0/octocat/Hello-World",
        "https://[::1]/octocat/Hello-World",
        "https://169.254.169.254/octocat/Hello-World",  # cloud metadata endpoint
        "https://10.0.0.5/octocat/Hello-World",
        "https://192.168.1.1/octocat/Hello-World",
    ],
)
def test_localhost_and_private_network_hosts_are_rejected(url: str) -> None:
    with pytest.raises(PrivateNetworkHostError):
        normalize_repository_url(url)


def test_unsupported_host_is_rejected() -> None:
    with pytest.raises(UnsupportedHostError):
        normalize_repository_url("https://gitlab.com/octocat/Hello-World")


def test_unsupported_port_is_rejected() -> None:
    with pytest.raises(UnsupportedHostError):
        normalize_repository_url("https://github.com:8443/octocat/Hello-World")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octocat/Hello-World?ref=main",
        "https://github.com/octocat/Hello-World#readme",
    ],
)
def test_query_and_fragment_are_rejected(url: str) -> None:
    with pytest.raises(UnsupportedURLComponentError):
        normalize_repository_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octocat",  # missing repo segment
        "https://github.com/octocat/Hello/World",  # extra segment
        "https://github.com/",  # no path at all
        "https://github.com",
        "https://github.com/../../etc/passwd",
        "https://github.com/oct..at/repo",
        "https://github.com/octocat/re..po",
        "https://github.com/-octocat/repo",  # owner can't start with a hyphen
        "https://github.com/octocat-/repo",  # owner can't end with a hyphen
        "https://github.com/octocat/repo with spaces",
    ],
)
def test_malformed_repository_paths_are_rejected(url: str) -> None:
    with pytest.raises(MalformedRepositoryPathError):
        normalize_repository_url(url)


@pytest.mark.parametrize("url", ["", "   "])
def test_empty_input_is_rejected(url: str) -> None:
    with pytest.raises(MalformedRepositoryPathError):
        normalize_repository_url(url)


def test_garbage_input_is_rejected() -> None:
    """Not a URL at all — rejected (as a missing/unsupported scheme), just
    not necessarily under the same subtype as a well-formed-but-empty path."""
    with pytest.raises(RepositoryURLError):
        normalize_repository_url("not a url at all")


def test_repository_names_with_dots_are_accepted() -> None:
    identity = normalize_repository_url("https://github.com/octocat/my.repo")
    assert identity.name == "my.repo"


def test_host_is_case_insensitive() -> None:
    identity = normalize_repository_url("https://GitHub.com/octocat/Hello-World")
    assert identity.host == "github.com"
