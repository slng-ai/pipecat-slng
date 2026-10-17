#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""SLNG regional gateways.

Each region runs its own gateway at ``{region}.api.slng.ai``. Requests are not
routed between regions. See https://docs.slng.ai/models/catalog/by-region.
"""

import re
import uuid
import warnings
from enum import StrEnum
from urllib.parse import quote, urlsplit

_TRACKING_ID_MAX_LENGTH = 128
_TRACKING_ID_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f,]")


class WorldPart(StrEnum):
    """An SLNG region. Each one is served at ``{value}.api.slng.ai``."""

    EU_NORTH = "eu-north"  # Netherlands
    EU_WEST = "eu-west"  # Germany
    US_EAST = "us-east"
    US_WEST = "us-west"
    AU = "au"
    BR = "br"
    GB = "gb"
    ID = "id"
    IL = "il"
    IN = "in"
    JP = "jp"
    SG = "sg"
    ZA = "za"

    @property
    def host(self) -> str:
        """The gateway host for this region, without scheme."""
        return f"{self.value}.api.slng.ai"

    def bridge_url(self, kind: str, model: str) -> str:
        """Build the Unmute bridge WebSocket URL for a model in this region.

        Args:
            kind: ``"stt"`` or ``"tts"``.
            model: The model route, e.g. ``"slng/deepgram/aura:2-en"``.

        Returns:
            The full URL, with the model path quoted.
        """
        return f"wss://{self.host}/v1/bridges/unmute/{kind}/{quote(model, safe='/:')}"

    @classmethod
    def resolve(cls, world_part: str, base_url: str | None = None) -> "WorldPart":
        """Validate ``world_part`` and check a legacy ``base_url`` against it.

        Args:
            world_part: Region name, e.g. ``"in"`` or ``"us-west"``.
            base_url: Deprecated. A regional host such as ``"in.api.slng.ai"``,
                with or without scheme. Its host must match ``world_part``.

        Returns:
            The matching ``WorldPart``.

        Raises:
            ValueError: If ``world_part`` is unknown, or ``base_url`` points to
                another host (including the old global ``api.slng.ai``).
        """
        try:
            resolved = cls(world_part)
        except ValueError:
            raise ValueError(
                f"Unknown world_part {world_part!r}. Use one of: {', '.join(cls)}."
            ) from None
        if base_url is None:
            return resolved
        host = urlsplit(base_url if "://" in base_url else f"//{base_url}").hostname
        if host != resolved.host:
            raise ValueError(
                f"base_url {base_url!r} does not match world_part {world_part!r}. "
                f"Expected host {resolved.host!r}. The global api.slng.ai gateway "
                "is no longer supported. Pass world_part only."
            )
        warnings.warn(
            "base_url is deprecated and will be removed. Pass world_part only.",
            DeprecationWarning,
            stacklevel=3,
        )
        return resolved


def _normalize_tracking_id(value: object, name: str) -> str:
    """Check one tracking ID the same way livekit-plugins-slng does.

    Args:
        value: The ID passed by the caller.
        name: The argument name, used in error messages.

    Returns:
        The ID with surrounding whitespace removed.

    Raises:
        ValueError: If the ID is not a string, is empty, is longer than 128
            characters, or holds a comma or a control character.
    """
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) > _TRACKING_ID_MAX_LENGTH:
        raise ValueError(
            f"{name} must be {_TRACKING_ID_MAX_LENGTH} characters or fewer"
        )
    if _TRACKING_ID_FORBIDDEN.search(value):
        raise ValueError(f"{name} must not contain commas or control characters")
    return value


def tracking_headers(
    external_agent_id: str | None,
    external_session_id: str | None,
    project_id: str | None = None,
) -> dict[str, str]:
    """Build the usage-tracking headers for the WebSocket upgrade.

    Args:
        external_agent_id: Sent as ``X-SLNG-Agent-Id``. ``None`` sends nothing.
        external_session_id: Sent as ``X-SLNG-Session-Id``. ``None`` sends nothing.
        project_id: Sent as ``X-SLNG-Project-Id``. ``None`` sends nothing.

    Returns:
        Zero to three headers.

    Raises:
        ValueError: If an ID fails the checks in ``_normalize_tracking_id``,
            or ``project_id`` is not a UUID.
    """
    headers: dict[str, str] = {}
    if external_agent_id is not None:
        headers["X-SLNG-Agent-Id"] = _normalize_tracking_id(
            external_agent_id, "external_agent_id"
        )
    if external_session_id is not None:
        headers["X-SLNG-Session-Id"] = _normalize_tracking_id(
            external_session_id, "external_session_id"
        )
    if project_id is not None:
        project_id = _normalize_tracking_id(project_id, "project_id")
        try:
            canonical = str(uuid.UUID(project_id))
        except ValueError:
            raise ValueError("project_id must be a UUID") from None
        headers["X-SLNG-Project-Id"] = canonical
    return headers
