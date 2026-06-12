#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Shared error-formatting helpers for the SLNG services."""


def connect_error_detail(e: Exception) -> str:
    """Describe a WebSocket connect failure, including the server's reason.

    A rejected upgrade raises ``websockets.exceptions.InvalidStatus``, whose
    ``str()`` carries only the HTTP status ("server rejected WebSocket
    connection: HTTP 400"). The body explaining *why* (e.g. "BYOK is only
    supported for external STT/TTS routes") lives in ``e.response.body`` and
    would otherwise be dropped (V19).
    """
    body = getattr(getattr(e, "response", None), "body", None)
    if body:
        detail = bytes(body).decode("utf-8", errors="replace").strip()
        if detail:
            return f"{e} — {detail}"
    return str(e)
