"""Endpoint normalization helpers for local team experiments.

When workers advertise their AgentCard URLs as ``http://0.0.0.0:<port>/``,
those endpoints are unusable for remote/self peer-to-peer connections
because 0.0.0.0 is not routable.

The coordinator normalises wildcard host entries to a reachable host
(typically ``localhost``) when building the team roster so each peer
can reach the others via a real network address.

Security note: this helper is designed for local experiments where all
workers share a loopback interface.  A production multi-host deployment
would use a proper service-discovery layer instead.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

_LOCAL_WILDCARD_HOSTS = frozenset({"0.0.0.0", "127.0.0.1", "localhost"})


def normalize_endpoint(
    endpoint: str,
    *,
    target_host: str = "localhost",
) -> str:
    """Replace a wildcard host ``0.0.0.0`` with *target_host*.

    Uses ``urllib.parse`` for structured URL handling.  Only ``http`` and
    ``https`` schemes are supported.  URLs with embedded credentials are
    rejected (returned unchanged with a warning).  If the endpoint has a
    path or query component, they are preserved.

    Args:
        endpoint: The raw endpoint URL.
        target_host: The hostname to substitute (default ``"localhost"``).

    Returns:
        Normalised endpoint string, or the original if no substitution
        was needed or the URL is malformed.

    Examples:
        >>> normalize_endpoint("http://0.0.0.0:8191/")
        'http://localhost:8191/'
        >>> normalize_endpoint("http://0.0.0.0:8191/path?q=1")
        'http://localhost:8191/path?q=1'
        >>> normalize_endpoint("https://0.0.0.0:8191/")
        'https://localhost:8191/'
    """
    try:
        parsed = urlparse(endpoint)
    except Exception:
        return endpoint

    if parsed.hostname not in _LOCAL_WILDCARD_HOSTS:
        return endpoint
    if parsed.scheme not in ("http", "https"):
        logger.warning("Unsupported scheme %r in %s", parsed.scheme, endpoint)
        return endpoint
    if parsed.username is not None or parsed.password is not None:
        logger.warning("Credentials in endpoint %s -- not normalising", endpoint)
        return endpoint
    if parsed.hostname is None:
        return endpoint

    netloc = parsed.netloc.replace(parsed.hostname, target_host, 1)
    result = urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )
    logger.debug("Normalised endpoint %s -> %s", endpoint, result)
    return result


def normalize_roster_endpoints(
    members: dict[str, str],
    *,
    target_host: str = "localhost",
) -> dict[str, str]:
    """Normalise all ``0.0.0.0`` hosts in a member-endpoints dict.

    Args:
        members: ``{worker_id: endpoint_url}`` mapping.
        target_host: Substitute hostname (default ``"localhost"``).

    Returns:
        A new dict with the same keys and normalised values.
    """
    return {
        wid: normalize_endpoint(ep, target_host=target_host)
        for wid, ep in members.items()
    }


def is_local_wildcard(endpoint: str) -> bool:
    """Return True if the endpoint's host is a local wildcard address.

    Checks the parsed hostname against ``0.0.0.0``, ``127.0.0.1``,
    and ``localhost``.
    """
    try:
        parsed = urlparse(endpoint)
        return parsed.hostname in _LOCAL_WILDCARD_HOSTS
    except Exception:
        return False


def strip_trailing_slash(endpoint: str) -> str:
    """Remove a trailing ``/`` from an endpoint URL if present."""
    return endpoint.rstrip("/")
