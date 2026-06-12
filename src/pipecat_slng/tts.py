#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""SLNG text-to-speech service."""

import asyncio
import io
import json
import wave
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiohttp
from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.services.settings import NOT_GIVEN, TTSSettings, _NotGiven, is_given
from pipecat.services.tts_service import TTSService, WebsocketTTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.tracing.service_decorators import traced_tts

from websockets.asyncio.client import connect as websocket_connect
from websockets.protocol import State

_DEFAULT_TTS_MODEL = "slng/deepgram/aura:2-en"


@dataclass
class SlngTTSSettings(TTSSettings):
    """Settings for SlngTTSService.

    Parameters:
        voice: Voice identifier for speech synthesis.
        language: Language for speech synthesis.
        speed: Speech speed multiplier. When not given, the server default is used.
    """

    speed: float | None | _NotGiven = field(default_factory=lambda: NOT_GIVEN)


def _build_tts_config(
    settings: SlngTTSSettings, encoding: str, sample_rate: int
) -> dict[str, Any]:
    """Build the SLNG TTS bridge ``config`` object.

    Shared by the WebSocket and HTTP TTS services so the wire format stays in
    one place. ``encoding``/``sample_rate`` are always included; ``language``
    and ``speed`` only when given and not None.
    """
    config: dict[str, Any] = {"encoding": encoding, "sample_rate": sample_rate}
    if is_given(settings.language) and settings.language is not None:
        config["language"] = str(settings.language)
    if is_given(settings.speed) and settings.speed is not None:
        config["speed"] = float(settings.speed)
    return config


class SlngTTSService(WebsocketTTSService):
    """Text-to-speech service using the SLNG Unmute TTS bridge WebSocket API.

    Provides real-time speech synthesis through a persistent WebSocket
    connection to ``wss://api.slng.ai/v1/bridges/unmute/tts/{model}``:

    - Connection-level config (``voice``, ``encoding``, ``sample_rate``,
      ``speed``, ``language``) is sent in an ``init`` text message.
    - Text to synthesise is sent as ``{"type": "text", "text": "..."}``.
    - ``{"type": "flush"}`` signals end of an utterance.
    - ``{"type": "clear"}`` cancels in-flight audio on interruption.
    - ``{"type": "close"}`` gracefully closes the connection.
    - Audio arrives as raw binary WebSocket frames; ``ready``, ``flushed``,
      ``audio_end``, and ``error`` arrive as JSON text frames.
    """

    Settings = SlngTTSSettings
    _settings: Settings

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_TTS_MODEL,
        voice: str | None = None,
        base_url: str = "api.slng.ai",
        encoding: str = "linear16",
        sample_rate: int | None = None,
        region_override: str | None = None,
        world_part_override: str | None = None,
        provider_key: str | None = None,
        language: Language | _NotGiven = NOT_GIVEN,
        speed: float | None | _NotGiven = NOT_GIVEN,
        settings: Settings | None = None,
        **kwargs,
    ):
        """Initialize SlngTTSService.

        Args:
            api_key: Authentication key for the SLNG API.
            model: The TTS model to use. Defaults to "slng/deepgram/aura:2-en".
            voice: Voice identifier for synthesis (e.g. "aura-2-thalia-en").
            base_url: The API host. Defaults to "api.slng.ai".
            encoding: Audio encoding format. One of ``"linear16"``, ``"mp3"``,
                ``"opus"``, ``"mulaw"``, or ``"alaw"``. Defaults to ``"linear16"``.
            sample_rate: Audio sample rate in Hz. If None, uses the pipeline sample rate.
            region_override: Pin requests to a specific datacenter. One of
                ``"ap-southeast-2"``, ``"eu-north-1"``, ``"us-east-1"``. Sets the
                ``X-Region-Override`` header (takes precedence over ``world_part_override``).
            world_part_override: Constrain routing to a broad geographic zone.
                One of ``"ap"``, ``"eu"``, ``"na"``. Sets the ``X-World-Part-Override``
                header.
            provider_key: Your own upstream provider API key (BYOK). Sent as the
                ``X-Slng-Provider-Key`` header on the WebSocket upgrade, so the
                provider bills your account directly. Only supported on external
                catalog routes (no ``slng/`` prefix), e.g. ``deepgram/aura:2``;
                SLNG-hosted ``slng/...`` routes reject it with a 400. A rejected
                key surfaces as a ``backend_connection_failed`` error frame with
                the upstream 401/403 detail. See
                https://docs.slng.ai/execution-layer/byok.
            language: Synthesis language. Defaults to ``Language.EN`` when not given.
            speed: Speech speed multiplier. ``None`` (default) keeps the server default.
            settings: Runtime-updatable settings override. Merged on top of any
                explicit kwargs above.
            **kwargs: Additional arguments passed to parent WebsocketTTSService.
        """
        default_settings = self.Settings(
            model=model,
            voice=voice,
            language=language if is_given(language) else Language.EN,
            speed=speed if is_given(speed) else None,
        )

        if settings is not None:
            default_settings.apply_update(settings)

        super().__init__(
            sample_rate=sample_rate,
            push_stop_frames=False,
            push_start_frame=True,
            settings=default_settings,
            **kwargs,
        )

        self._api_key = api_key
        self._base_url = base_url
        self._encoding = encoding
        self._region_override = region_override
        self._world_part_override = world_part_override
        self._provider_key = provider_key
        self._receive_task = None
        self._ready_event = asyncio.Event()
        self._ready_timeout = 5.0
        # Some upstreams (e.g. rime) close the WebSocket right after
        # audio_end/flushed; that close is part of the utterance lifecycle,
        # not a failure (V15).
        self._expect_server_close = False

    def can_generate_metrics(self) -> bool:
        """Check if the service can generate processing metrics.

        Returns:
            True, indicating metrics are supported.
        """
        return True

    async def start(self, frame: StartFrame):
        """Start the TTS service and establish the WebSocket connection.

        Args:
            frame: Frame indicating service should start.
        """
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        """Stop the TTS service and close the WebSocket connection.

        Args:
            frame: Frame indicating service should stop.
        """
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        """Cancel the TTS service and close the WebSocket connection.

        Args:
            frame: Frame indicating service should be cancelled.
        """
        await super().cancel(frame)
        await self._disconnect()

    async def _connect(self):
        await super()._connect()
        await self._connect_websocket()
        if self._websocket and not self._receive_task:
            self._receive_task = self.create_task(
                self._receive_task_handler(self._report_error)
            )

    async def _disconnect(self):
        await super()._disconnect()

        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None

        await self._disconnect_websocket()

    def _build_config(self) -> dict[str, Any]:
        """Build the inner ``config`` object of the init message.

        Per the Unmute TTS bridge spec, ``voice`` is a top-level field on the
        init message — it is not part of ``config``.
        """
        return _build_tts_config(self._settings, self._encoding, self.sample_rate)

    async def _connect_websocket(self):
        """Establish the WebSocket connection and send the initial ``init`` message.

        The SLNG TTS bridge requires an ``init`` text message before any
        ``text``/``flush`` messages are accepted; otherwise the server replies
        with an error and ignores subsequent messages. The server responds with
        a ``ready`` message once the session is established.
        """
        try:
            if self._websocket and self._websocket.state is State.OPEN:
                return

            model = self._settings.model
            if not is_given(model) or not model:
                model = _DEFAULT_TTS_MODEL
            logger.debug(f"Connecting to SLNG TTS ({model})")

            model_path = quote(model, safe="/:")
            if "://" in self._base_url:
                ws_url = f"{self._base_url}/v1/bridges/unmute/tts/{model_path}"
            else:
                ws_url = f"wss://{self._base_url}/v1/bridges/unmute/tts/{model_path}"

            headers: dict[str, str] = {"Authorization": f"Bearer {self._api_key}"}
            if self._region_override:
                headers["X-Region-Override"] = self._region_override
            if self._world_part_override:
                headers["X-World-Part-Override"] = self._world_part_override
            if self._provider_key:
                headers["X-Slng-Provider-Key"] = self._provider_key
            self._ready_event.clear()
            self._websocket = await websocket_connect(
                ws_url, additional_headers=headers
            )

            init_msg: dict[str, Any] = {"type": "init", "config": self._build_config()}
            if self._settings.voice:
                init_msg["voice"] = str(self._settings.voice)
            await self._websocket.send(json.dumps(init_msg))

            await self._call_event_handler("on_connected")
        except Exception as e:
            self._websocket = None
            # Community-integration guide (V4): push_error AND raise so the
            # PipelineRunner surfaces the failure instead of dribbling silent
            # send-after-disconnect errors.
            await self.push_error(
                error_msg=f"Unable to connect to SLNG TTS: {e}", exception=e
            )
            raise

    async def _disconnect_websocket(self):
        """Send a ``Close`` message and shut down the WebSocket."""
        ws = self._websocket
        try:
            if ws and ws.state is State.OPEN:
                logger.debug("Disconnecting from SLNG TTS")
                await ws.send(json.dumps({"type": "close"}))
                await ws.close()
        except Exception as e:
            await self.push_error(
                error_msg=f"Error closing SLNG TTS websocket: {e}", exception=e
            )
        finally:
            await self.stop_all_metrics()
            await self.remove_active_audio_context()
            if self._websocket is ws:
                self._websocket = None
            await self._call_event_handler("on_disconnected")

    def _get_websocket(self):
        if self._websocket:
            return self._websocket
        raise Exception("SLNG TTS websocket not connected")

    async def _maybe_try_reconnect(self, error_message, report_error, error=None):
        """Handle a server-initiated close, distinguishing expected from failure.

        Some upstreams close the WebSocket (code 1000) right after
        ``audio_end``/``flushed`` — a per-utterance lifecycle, not an error.
        Reconnect quietly so the close neither logs reconnect warnings nor
        feeds the base class quick-failure counter (which would otherwise shut
        the service down after 3 short utterances in a row). Runs inside the
        receive task, so it must not cancel ``_receive_task`` — hence
        ``_disconnect_websocket``/``_connect_websocket`` rather than
        ``_disconnect``/``_connect``. A reconnect failure propagates to the
        receive handler, which retries via the base machinery with the flag
        already consumed.

        Unexpected closes (flag unset) keep the full base class behavior.
        """
        if self._expect_server_close and not self._disconnecting:
            self._expect_server_close = False
            logger.debug(f"{self}: expected per-utterance server close, reconnecting")
            await self._disconnect_websocket()
            await self._connect_websocket()
            return True  # receive loop continues on the new socket
        return await super()._maybe_try_reconnect(error_message, report_error, error)

    async def on_audio_context_interrupted(self, context_id: str):
        """Send a ``Clear`` message to the server when the bot is interrupted.

        Args:
            context_id: The ID of the interrupted audio context.
        """
        await self.stop_all_metrics()
        if self._websocket and self._websocket.state is State.OPEN:
            try:
                await self._websocket.send(json.dumps({"type": "clear"}))
            except Exception as e:
                logger.warning(f"{self}: failed to send clear on interruption: {e}")
        await super().on_audio_context_interrupted(context_id)

    async def flush_audio(self, context_id: str | None = None):
        """Flush pending audio for the current utterance.

        Sends a ``Flush`` message to the server, which will respond with a
        ``Flushed`` message when all audio has been sent.

        Args:
            context_id: The specific context to flush. If None, falls back to
                the currently active context.
        """
        if not self._websocket or self._websocket.state is not State.OPEN:
            return
        logger.trace(f"{self}: flushing audio")
        try:
            await self._websocket.send(json.dumps({"type": "flush"}))
        except Exception as e:
            logger.warning(f"{self}: failed to send flush: {e}")

    async def _receive_messages(self):
        """Receive and dispatch incoming WebSocket messages.

        Binary frames carry audio (PCM in the configured encoding); text
        frames are JSON control messages (``Metadata``/``Flushed``/``Cleared``/
        ``Warning``).
        """
        async for message in self._get_websocket():
            if isinstance(message, bytes):
                await self._handle_audio_bytes(message)
                continue
            try:
                data = json.loads(message)
                await self._process_message(data)
            except json.JSONDecodeError:
                logger.warning(f"{self}: received non-JSON message: {message!r}")
            except Exception as e:
                logger.error(f"{self}: error processing message: {e}")

    async def _handle_audio_bytes(self, audio: bytes):
        """Append a binary audio chunk to the active audio context."""
        if not audio:
            return
        ctx_id = self.get_active_audio_context_id()
        frame = TTSAudioRawFrame(
            audio=audio,
            sample_rate=self.sample_rate,
            num_channels=1,
            context_id=ctx_id,
        )
        await self.stop_ttfb_metrics()
        await self.append_to_audio_context(ctx_id, frame)

    async def _process_message(self, data: dict[str, Any]):
        """Dispatch a decoded server text message (case-insensitive).

        Args:
            data: Decoded JSON payload from the server.
        """
        msg_type = data.get("type") or ""
        type_lc = msg_type.lower() if isinstance(msg_type, str) else ""

        if type_lc == "ready":
            session_id = data.get("session_id", "")
            logger.debug(f"{self}: SLNG TTS session ready (id={session_id})")
            self._ready_event.set()

        elif type_lc == "metadata":
            logger.trace(f"{self}: SLNG TTS metadata: {data}")

        elif type_lc == "flushed":
            self._expect_server_close = True
            ctx_id = self.get_active_audio_context_id()
            if ctx_id:
                await self.append_to_audio_context(
                    ctx_id, TTSStoppedFrame(context_id=ctx_id)
                )
                await self.remove_audio_context(ctx_id)

        elif type_lc == "cleared":
            pass

        elif type_lc == "audio_end":
            logger.trace(f"{self}: SLNG TTS audio_end: {data}")
            self._expect_server_close = True

        elif type_lc == "error":
            raw = data.get("data")
            err = raw if isinstance(raw, dict) else {}
            error_msg = (
                err.get("message")
                or data.get("message")
                or err.get("code")
                or data.get("code")
                or f"Unknown SLNG TTS error (payload: {data})"
            )
            logger.error(f"{self}: SLNG TTS error: {error_msg}")
            await self.push_error(error_msg=str(error_msg))
            await self.stop_all_metrics()

        else:
            logger.debug(f"{self}: unknown message: {data}")

    @traced_tts
    async def run_tts(
        self, text: str, context_id: str
    ) -> AsyncGenerator[Frame | None, None]:
        """Generate speech from text using the SLNG TTS API.

        Sends a ``text`` message over the WebSocket. Waits for the server
        ``ready`` acknowledgement before sending; this prevents synthesis
        messages from racing the ``init`` handshake on reconnect. Audio arrives
        asynchronously via the receive task as binary frames.

        Args:
            text: The text to synthesise into speech.
            context_id: The context ID for tracking audio frames.

        Yields:
            None — audio frames are delivered via the receive task.
        """
        logger.debug(f"{self}: Generating TTS [{text}]")

        try:
            if not self._websocket or self._websocket.state is not State.OPEN:
                await self._connect()

            if not self._websocket:
                error_msg = "SLNG TTS websocket not connected"
                await self.push_error(error_msg=error_msg)
                yield ErrorFrame(error=error_msg)
                return

            if not self._ready_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._ready_event.wait(), timeout=self._ready_timeout
                    )
                except TimeoutError:
                    logger.warning(f"{self}: init ack timed out, sending Speak anyway")

            try:
                # New utterance starting: any stale expected-close flag from a
                # server that did NOT close after audio_end must not mask a
                # later real failure (V15).
                self._expect_server_close = False
                await self._websocket.send(json.dumps({"type": "text", "text": text}))
                await self.start_tts_usage_metrics(text)
            except Exception as e:
                error_msg = f"SLNG TTS send error: {e}"
                await self.push_error(error_msg=error_msg, exception=e)
                yield ErrorFrame(error=error_msg)
                yield TTSStoppedFrame(context_id=context_id)
                await self._disconnect()
                await self._connect()
                return

            yield None

        except Exception as e:
            error_msg = f"Unknown error occurred: {e}"
            await self.push_error(error_msg=error_msg, exception=e)
            yield ErrorFrame(error=error_msg)

    async def _update_settings(self, delta: TTSSettings) -> dict[str, Any]:
        """Apply a settings delta and reconnect to re-run the init handshake.

        The TTS bridge takes connection-level config (voice/speed/language) in
        the ``init`` message, so changed settings only take effect after
        reconnecting. ``WebsocketTTSService`` has no ``_request_reconnect``, so
        we disconnect and reconnect explicitly.

        Args:
            delta: A settings delta to apply.

        Returns:
            Dict mapping changed field names to their previous values.
        """
        changed = await super()._update_settings(delta)
        if not changed:
            return changed
        await self._disconnect()
        await self._connect()
        return changed


def _extract_pcm(data: bytes, default_sample_rate: int) -> tuple[int, bytes | None]:
    """Extract raw PCM and its sample rate from an HTTP TTS response body.

    The SLNG HTTP bridge returns ``audio/*`` without documenting the codec, so
    the format is detected from the bytes:

    - A WAV/RIFF container is parsed to raw PCM at its embedded sample rate.
    - Plain PCM (no recognised container or compressed magic) is returned as-is
      at ``default_sample_rate``.
    - Compressed formats (MP3/Ogg) return ``(default_sample_rate, None)`` so the
      caller can surface an error — pipecat needs raw PCM, not an encoded codec.
    """
    if not data:
        return default_sample_rate, b""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        with wave.open(io.BytesIO(data), "rb") as wf:
            return wf.getframerate(), wf.readframes(wf.getnframes())
    if (
        data[:3] == b"ID3"
        or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")  # MP3 frame sync
        or data[:4] == b"OggS"  # Ogg/Opus
    ):
        return default_sample_rate, None
    return default_sample_rate, data


class SlngHttpTTSService(TTSService):
    """Text-to-speech service using the SLNG Unified TTS bridge HTTP API.

    Performs non-streaming (request/response) synthesis via
    ``POST https://api.slng.ai/v1/bridges/unmute/tts/{model}``. Each
    ``run_tts`` call issues a single HTTP request and returns the full audio
    body as one ``TTSAudioRawFrame``. Prefer the streaming WebSocket
    :class:`SlngTTSService` for low-latency, interruptible conversational
    audio; use this for simpler batch/non-streaming synthesis.

    The bridge accepts only ``{text, voice}`` in the body and returns
    ``audio/*`` without a documented codec, so responses are auto-detected: a
    WAV/RIFF container is decoded to raw PCM at the file's sample rate, and
    anything else is treated as raw PCM at the pipeline sample rate. Compressed
    formats (MP3/Ogg) are rejected with an error.
    """

    Settings = SlngTTSSettings
    _settings: Settings

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_TTS_MODEL,
        voice: str | None = None,
        base_url: str = "https://api.slng.ai",
        aiohttp_session: aiohttp.ClientSession | None = None,
        sample_rate: int | None = None,
        region_override: str | None = None,
        world_part_override: str | None = None,
        provider_key: str | None = None,
        language: Language | _NotGiven = NOT_GIVEN,
        speed: float | None | _NotGiven = NOT_GIVEN,
        settings: Settings | None = None,
        **kwargs,
    ):
        """Initialize SlngHttpTTSService.

        Args:
            api_key: Authentication key for the SLNG API.
            model: The TTS model to use. Defaults to "slng/deepgram/aura:2-en".
            voice: Voice identifier for synthesis (e.g. "aura-2-thalia-en").
            base_url: Full base URL (including scheme) of the SLNG API.
                Defaults to "https://api.slng.ai".
            aiohttp_session: Optional aiohttp ClientSession. If None, one is
                created in ``start()`` and closed in ``stop()``/``cancel()``.
            sample_rate: Audio sample rate in Hz. If None, uses the pipeline rate.
                Applied to non-container (raw PCM) responses; WAV responses use
                their own embedded sample rate.
            region_override: Pin requests to a specific datacenter. Sent as the
                ``region`` query parameter.
            world_part_override: Constrain routing to a broad geographic zone.
                Sent as the ``world-part`` query parameter.
            provider_key: Your own upstream provider API key (BYOK). Sent as the
                ``X-Slng-Provider-Key`` header on each request, so the provider
                bills your account directly. Only supported on external catalog
                routes (no ``slng/`` prefix), e.g. ``deepgram/aura:2``;
                SLNG-hosted ``slng/...`` routes reject it with a 400. A rejected
                key returns the upstream 401/403 with the
                ``X-Slng-Auth-Source: client_key`` response header. See
                https://docs.slng.ai/execution-layer/byok.
            language: Kept for API parity with the WebSocket service; the SLNG
                HTTP bridge body is ``{text, voice}`` only and does NOT accept
                a ``config`` object, so this value is not sent over the wire.
            speed: Kept for API parity with the WebSocket service; not sent over
                the wire for the same reason as ``language``.
            settings: Runtime-updatable settings override. Merged on top of any
                explicit kwargs above.
            **kwargs: Additional arguments passed to parent TTSService.
        """
        default_settings = self.Settings(
            model=model,
            voice=voice,
            language=language if is_given(language) else Language.EN,
            speed=speed if is_given(speed) else None,
        )

        if settings is not None:
            default_settings.apply_update(settings)

        super().__init__(
            sample_rate=sample_rate,
            push_start_frame=True,
            push_stop_frames=True,
            settings=default_settings,
            **kwargs,
        )

        self._api_key = api_key
        self._base_url = base_url
        self._region_override = region_override
        self._world_part_override = world_part_override
        self._provider_key = provider_key
        self._session = aiohttp_session
        self._owns_session = aiohttp_session is None

    def can_generate_metrics(self) -> bool:
        """Check if the service can generate processing metrics.

        Returns:
            True, indicating metrics are supported.
        """
        return True

    async def start(self, frame: StartFrame):
        """Start the service, creating an HTTP session if none was provided.

        Args:
            frame: Frame indicating service should start.
        """
        await super().start(frame)
        if self._owns_session and self._session is None:
            self._session = aiohttp.ClientSession()

    async def _close_session(self):
        """Close the HTTP session if this service owns it."""
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def stop(self, frame: EndFrame):
        """Stop the service and close an owned HTTP session.

        Args:
            frame: Frame indicating service should stop.
        """
        await super().stop(frame)
        await self._close_session()

    async def cancel(self, frame: CancelFrame):
        """Cancel the service and close an owned HTTP session.

        Args:
            frame: Frame indicating service should be cancelled.
        """
        await super().cancel(frame)
        await self._close_session()

    @traced_tts
    async def run_tts(
        self, text: str, context_id: str
    ) -> AsyncGenerator[Frame | None, None]:
        """Generate speech from text via a single SLNG HTTP request.

        Args:
            text: The text to synthesise into speech.
            context_id: The context ID for tracking audio frames.

        Yields:
            A single ``TTSAudioRawFrame`` with the synthesised audio, or an
            ``ErrorFrame`` on failure. ``TTSStartedFrame``/``TTSStoppedFrame``
            are emitted by the base class.
        """
        logger.debug(f"{self}: Generating HTTP TTS [{text}]")

        try:
            if self._session is None:
                raise RuntimeError(
                    "HTTP session is not initialized; call start() before run_tts()"
                )

            model = self._settings.model
            if not is_given(model) or not model:
                model = _DEFAULT_TTS_MODEL
            model_path = quote(model, safe="/:")
            url = f"{self._base_url}/v1/bridges/unmute/tts/{model_path}"

            headers: dict[str, str] = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }
            if self._provider_key:
                headers["X-Slng-Provider-Key"] = self._provider_key

            # The HTTP bridge body accepts only {text, voice}; region/world-part
            # are query parameters (the WebSocket service uses headers instead).
            params: dict[str, str] = {}
            if self._region_override:
                params["region"] = self._region_override
            if self._world_part_override:
                params["world-part"] = self._world_part_override

            payload: dict[str, Any] = {"text": text}
            if self._settings.voice:
                payload["voice"] = str(self._settings.voice)

            async with self._session.post(
                url, json=payload, headers=headers, params=params or None
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    error_msg = (
                        f"SLNG HTTP TTS error (status {response.status}): {error_text}"
                    )
                    await self.push_error(error_msg=error_msg)
                    yield ErrorFrame(error=error_msg)
                    return
                content_type = response.headers.get("Content-Type", "")
                audio = await response.read()

            sample_rate, pcm = _extract_pcm(audio, self.sample_rate)
            if pcm is None:
                logger.error(
                    f"{self}: unsupported audio format from HTTP bridge "
                    f"(content-type={content_type!r}, first bytes={audio[:4]!r})"
                )
                error_msg = (
                    "SLNG HTTP TTS returned an unsupported audio format "
                    f"(content-type={content_type!r}); expected raw PCM or WAV. "
                    "Use the WebSocket SlngTTSService for streaming PCM."
                )
                await self.push_error(error_msg=error_msg)
                yield ErrorFrame(error=error_msg)
                return

            await self.start_tts_usage_metrics(text)

            yield TTSAudioRawFrame(
                audio=pcm,
                sample_rate=sample_rate,
                num_channels=1,
                context_id=context_id,
            )

        except Exception as e:
            error_msg = f"Unknown error occurred: {e}"
            await self.push_error(error_msg=error_msg, exception=e)
            yield ErrorFrame(error=error_msg)
        finally:
            await self.stop_ttfb_metrics()
