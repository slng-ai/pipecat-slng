#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Shared test fixtures: an in-process fake WebSocket.

The fake mimics the subset of the ``websockets`` client API that the SLNG
services use: ``state``, ``send``, ``close``, and async iteration yielding
server frames. Patch it over ``websocket_connect`` in the service module to
drive the receive loop deterministically.
"""

import asyncio

import pytest
from websockets.protocol import State

_SENTINEL = object()


class FakeWebSocket:
	"""Async-iterable stand-in for a ``websockets`` client connection."""

	def __init__(self, server_messages=None):
		"""Initialize with an optional list of pre-queued server frames.

		Args:
			server_messages: Frames (str JSON or bytes) the server "sends"
				to the client, delivered in order via async iteration.
		"""
		self.state = State.OPEN
		self.sent: list = []
		self.connect_url: str | None = None
		self.connect_headers: dict | None = None
		self._incoming: asyncio.Queue = asyncio.Queue()
		for msg in server_messages or []:
			self._incoming.put_nowait(msg)

	async def send(self, data):
		"""Record a client→server payload."""
		self.sent.append(data)

	async def close(self):
		"""Mark the socket closed and stop iteration."""
		self.state = State.CLOSED
		await self._incoming.put(_SENTINEL)

	async def feed(self, msg):
		"""Push an additional server→client frame at runtime."""
		await self._incoming.put(msg)

	def __aiter__(self):
		return self

	async def __anext__(self):
		msg = await self._incoming.get()
		if msg is _SENTINEL:
			raise StopAsyncIteration
		return msg


@pytest.fixture
def patch_ws(monkeypatch):
	"""Return a factory that patches ``websocket_connect`` in a service module.

	Usage::

		ws = patch_ws("pipecat_slng.stt", [json.dumps({"type": "ready"})])
	"""

	def _patch(module_path: str, server_messages=None) -> FakeWebSocket:
		fake = FakeWebSocket(server_messages)

		async def _connect(url, **kwargs):
			fake.connect_url = url
			fake.connect_headers = kwargs.get("additional_headers")
			return fake

		monkeypatch.setattr(f"{module_path}.websocket_connect", _connect)
		return fake

	return _patch
