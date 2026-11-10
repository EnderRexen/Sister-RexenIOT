"""Shared API authentication.

The app is an agent with tools, so reaching its API means being able to drive
those tools. Two things protect it:

* it binds to the loopback address, so only this machine can reach it at all;
* when it does not, :func:`require_api_token` demands a token.

The token comes from ``REXENIOT_API_TOKEN`` or ``server.auth_token``. Empty
means the check is skipped, which is safe exactly as long as the server is on
loopback -- :func:`assert_binding_is_safe` enforces that pairing at startup.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os

from fastapi import Header, HTTPException

log = logging.getLogger("rexeniot.api.auth")


def api_token() -> str:
    """The configured API token, or "" when none is set."""
    from . import config

    return (os.environ.get("REXENIOT_API_TOKEN", "").strip() or config.load().server.auth_token.strip())


def is_loopback(host: str) -> bool:
    """True when ``host`` only accepts connections from this machine.

    A blank host is treated as unsafe: uvicorn reads that as "all interfaces".
    """
    host = (host or "").strip()
    if not host:
        return False
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname that is not "localhost". It may resolve to a loopback
        # address, but we cannot be sure, so treat it as exposed.
        return False


def assert_binding_is_safe(host: str, port: int) -> None:
    """Refuse to start an unauthenticated server on a non-loopback address.

    This is the difference between a mistake being a nuisance and being a
    breach, so it is fatal rather than a warning.
    """
    if is_loopback(host) or api_token():
        return
    raise SystemExit(
        f"Refusing to bind {host}:{port} without an API token.\n"
        f"\n"
        f"Binding a non-loopback address makes this agent reachable from your\n"
        f"network, and every endpoint that can drive it would be open.\n"
        f"\n"
        f"Either:\n"
        f"  * keep the default loopback binding (--host 127.0.0.1), or\n"
        f"  * set a token:  export REXENIOT_API_TOKEN=$(openssl rand -hex 32)\n"
        f"                  or put it in server.auth_token in the config file.\n"
        f"\n"
        f"For remote access, prefer a private overlay (Tailscale/WireGuard) and\n"
        f"leave the server on 127.0.0.1 -- see docs/REMOTE-ACCESS.md."
    )


def require_api_token(x_api_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency: reject the request unless the token matches.

    A no-op when no token is configured. Compared in constant time so the
    check cannot be used as an oracle.
    """
    expected = api_token()
    if not expected:
        return
    if not x_api_token or not hmac.compare_digest(x_api_token, expected):
        raise HTTPException(401, "Invalid or missing API token.")
