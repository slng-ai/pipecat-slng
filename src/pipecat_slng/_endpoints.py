#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Constructor-time endpoint selection shared by the three SLNG services.

``world_part`` is required and names the destination hostname prefix: ``"gb"``
routes to ``gb.api.slng.ai``. It is validated for syntax only — a well-formed
prefix SLNG has not provisioned yet still composes a URL, and normal connection
errors apply. There is no runtime destination catalog and no DNS lookup here.
"""

import ipaddress
import re
from urllib.parse import urlsplit

_SLNG_HOST_SUFFIX = "api.slng.ai"

# One hostname label: 1-63 lowercase letters/digits/hyphens, alphanumeric ends.
_WORLD_PART = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_DNS_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_DNS_HOST = re.compile(rf"{_DNS_LABEL}(?:\.{_DNS_LABEL})*\.?")
# urlsplit() silently strips these, so reject them on the raw text first.
_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f]")

# Pipecat's base classes silently accept unknown keyword arguments, so removing
# these parameters is not enough — an old call would route to the default host
# with its setting quietly dropped.
_REMOVED = ("region_override", "world_part_override")

_WORLD_PART_HELP = (
    "world_part must be one lowercase hostname prefix of 1-63 letters, digits, "
    "or hyphens (e.g. 'eu-west') — not a full hostname, URL, or region group"
)


def resolve_base_url(
    *, world_part: str, base_url: str | None, websocket: bool, extra: dict
) -> str:
    """Validate the routing configuration and return the base URL to use.

    Args:
        world_part: Required destination hostname prefix.
        base_url: Advanced host override, or None for world-part routing.
        websocket: True for the ``ws``/``wss`` services, False for HTTP.
        extra: The caller's remaining kwargs, checked for removed settings.

    Returns:
        A full base URL with a scheme and no trailing path separator.

    Raises:
        TypeError: A removed regional override setting was supplied.
        ValueError: ``world_part`` or an explicit ``base_url`` is invalid.
    """
    removed = [name for name in _REMOVED if name in extra]
    if removed:
        raise TypeError(
            f"{' and '.join(removed)} {'have' if len(removed) > 1 else 'has'} been "
            "removed; pass world_part=<hostname prefix> instead (routing now lives "
            "in the hostname)"
        )

    if not isinstance(world_part, str) or not _WORLD_PART.fullmatch(world_part):
        raise ValueError(f"{_WORLD_PART_HELP} (got {world_part!r})")

    # Only None means "route by world part". An explicit base — including the
    # legacy unprefixed host — overrides it, so never test truthiness or compare
    # against the old default host.
    if base_url is None:
        scheme = "wss" if websocket else "https"
        return f"{scheme}://{world_part}.{_SLNG_HOST_SUFFIX}"

    return _explicit_base(base_url, websocket=websocket)


def _explicit_base(base_url: str, *, websocket: bool) -> str:
    """Validate an explicit base and return it with its host unchanged."""

    def bad(reason: str) -> ValueError:
        # Never echo the value: an explicit base can embed credentials.
        return ValueError(
            f"base_url is invalid ({reason}); omit it to route via world_part"
        )

    if not isinstance(base_url, str) or not base_url:
        raise bad("expected a non-empty string")
    if _UNSAFE.search(base_url) or "?" in base_url or "#" in base_url:
        raise bad("whitespace, control characters, query, and fragment are not allowed")

    schemes = ("ws", "wss") if websocket else ("http", "https")
    if "://" not in base_url:
        if not websocket:
            raise bad(f"expected a full {schemes[1]}:// URL")
        base_url = f"wss://{base_url}"  # bare hosts default to TLS

    try:
        parts = urlsplit(base_url)
    except ValueError:
        raise bad("malformed URL") from None
    if parts.scheme not in schemes:
        raise bad(f"scheme must be {' or '.join(schemes)}")
    if "@" in parts.netloc:
        raise bad("credentials are not allowed")
    if parts.netloc.endswith(":"):
        raise bad("port is empty")
    try:
        port = parts.port
    except ValueError:
        raise bad("port must be a number in 1-65535") from None
    if port == 0:
        raise bad("port must be a number in 1-65535")
    host = parts.hostname
    if not host or not _DNS_HOST.fullmatch(host):
        raise bad("host must be a DNS name")
    try:
        ipaddress.ip_address(host.rstrip("."))
    except ValueError:
        pass  # not an IP literal, which is what we want
    else:
        raise bad("host must be a DNS name, not an IP address")

    # Keep the authority verbatim (casing, port spelling, root dot) and the path
    # with its percent escapes; only the trailing separator goes, so appending
    # the bridge route cannot double it.
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
