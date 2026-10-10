"""Repository URL normalization and validation — an SSRF and input-safety
boundary, not just a format check.

See docs/architecture/repository-ingestion.md sections 3-5 for the full
rationale. Summary: v1 accepts only public, HTTPS, github.com repository
URLs. Every rejection raises a specific `RepositoryURLError` subclass so the
API layer can map each to an appropriate response without string matching.
The normalized `RepositoryIdentity` this module returns is the only form of
the URL anything downstream (the Git adapter, the database) should ever see
— never the raw, unvalidated input.
"""

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

ALLOWED_HOST = "github.com"
_LOCALHOST_NAMES = frozenset({"localhost", "localhost.localdomain"})

# No "/" in either charset — that alone is what rules out path traversal via
# these two segments; see repository-ingestion.md section 4, point 7.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class RepositoryURLError(ValueError):
    """Base class for every repository URL rejection."""


class UnsupportedSchemeError(RepositoryURLError):
    pass


class EmbeddedCredentialsError(RepositoryURLError):
    pass


class PrivateNetworkHostError(RepositoryURLError):
    pass


class UnsupportedHostError(RepositoryURLError):
    pass


class UnsupportedURLComponentError(RepositoryURLError):
    pass


class MalformedRepositoryPathError(RepositoryURLError):
    pass


@dataclass(frozen=True)
class RepositoryIdentity:
    host: str
    owner: str
    name: str
    canonical_url: str
    clone_url: str


def _is_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def _split(raw: str) -> SplitResult:
    try:
        return urlsplit(raw)
    except ValueError as exc:
        raise MalformedRepositoryPathError(f"could not parse URL: {raw!r}") from exc


def _validate_scheme(parsed: SplitResult) -> None:
    if parsed.scheme.lower() != "https":
        raise UnsupportedSchemeError(
            f"unsupported scheme {parsed.scheme!r}; only https is accepted"
        )


def _validate_no_credentials(parsed: SplitResult) -> None:
    if parsed.username is not None or parsed.password is not None:
        raise EmbeddedCredentialsError("URLs with embedded credentials are not accepted")


def _validate_host(parsed: SplitResult) -> str:
    hostname = parsed.hostname
    if not hostname:
        raise MalformedRepositoryPathError("URL has no host")

    if hostname in _LOCALHOST_NAMES or _is_ip_literal(hostname):
        raise PrivateNetworkHostError(f"host {hostname!r} is a loopback/IP-literal target")

    if parsed.port is not None and parsed.port != 443:
        raise UnsupportedHostError(f"unsupported port {parsed.port!r}")

    if hostname != ALLOWED_HOST:
        raise UnsupportedHostError(f"unsupported host {hostname!r}; v1 only accepts {ALLOWED_HOST}")

    return hostname


def _validate_no_query_or_fragment(parsed: SplitResult) -> None:
    if parsed.query or parsed.fragment:
        raise UnsupportedURLComponentError(
            "query strings and fragments are not part of repository identity"
        )


def _validate_path(parsed: SplitResult) -> tuple[str, str]:
    path = parsed.path
    if path.endswith("/"):
        path = path[:-1]

    segments = [segment for segment in path.split("/") if segment]
    if len(segments) != 2:
        raise MalformedRepositoryPathError(f"expected /OWNER/REPO, got {parsed.path!r}")

    owner, repo = segments
    if repo.endswith(".git") and len(repo) > len(".git"):
        repo = repo[: -len(".git")]

    for name in (owner, repo):
        if ".." in name:
            raise MalformedRepositoryPathError(f"suspicious path segment {name!r}")

    if not _OWNER_RE.match(owner):
        raise MalformedRepositoryPathError(f"invalid repository owner {owner!r}")
    if not _REPO_RE.match(repo):
        raise MalformedRepositoryPathError(f"invalid repository name {repo!r}")

    return owner, repo


def normalize_repository_url(raw: str) -> RepositoryIdentity:
    """Validate and normalize a repository URL, or raise a
    `RepositoryURLError` subclass describing exactly why it was rejected."""
    if not raw or not raw.strip():
        raise MalformedRepositoryPathError("empty URL")

    parsed = _split(raw.strip())
    _validate_scheme(parsed)
    _validate_no_credentials(parsed)
    host = _validate_host(parsed)
    _validate_no_query_or_fragment(parsed)
    owner, repo = _validate_path(parsed)

    canonical_url = f"https://{host}/{owner}/{repo}"
    return RepositoryIdentity(
        host=host,
        owner=owner,
        name=repo,
        canonical_url=canonical_url,
        clone_url=f"{canonical_url}.git",
    )
