#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""SLNG speech-to-text services."""

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.settings import NOT_GIVEN, STTSettings, _NotGiven, is_given
from pipecat.services.stt_service import WebsocketSTTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601
from pipecat.utils.tracing.service_decorators import traced_stt

from websockets.asyncio.client import connect as websocket_connect
from websockets.protocol import State

_DEFAULT_STT_MODEL = "slng/deepgram/nova:3-en"


@dataclass
class SlngSTTSettings(STTSettings):
    """Settings for SlngSTTService.

    Parameters:
        language: Language for speech recognition.
        enable_vad: Whether to enable server-side VAD.
        enable_partials: Whether to receive partial (interim) transcriptions.
    """

    enable_vad: bool | _NotGiven = field(default_factory=lambda: NOT_GIVEN)
    enable_partials: bool | _NotGiven = field(default_factory=lambda: NOT_GIVEN)


class SlngSTTService(WebsocketSTTService):
    """Speech-to-text service using the SLNG Unmute STT bridge WebSocket API.

    Provides real-time speech transcription through a persistent WebSocket
    connection to ``wss://api.slng.ai/v1/bridges/unmute/stt/{model}``:

    - Audio is sent as raw binary WebSocket frames (no JSON wrapping).
    - Connection-level config (``sample_rate``, ``encoding``, ``language``,
      ``enable_partials``, ``enable_vad``) is sent in an ``init`` text message.
    - Client control messages use lowercase ``type``: ``keepalive``,
      ``finalize``, ``close``.
    - Server emits ``ready``, ``partial_transcript``, ``final_transcript``,
      ``utterance_end``, and ``error`` JSON text frames.
    """

    Settings = SlngSTTSettings
    _settings: Settings

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_STT_MODEL,
        base_url: str = "api.slng.ai",
        encoding: str = "linear16",
        sample_rate: int | None = None,
        region_override: str | None = None,
        world_part_override: str | None = None,
        provider_key: str | None = None,
        language: Language | _NotGiven = NOT_GIVEN,
        enable_vad: bool | _NotGiven = NOT_GIVEN,
        enable_partials: bool | _NotGiven = NOT_GIVEN,
        settings: Settings | None = None,
        **kwargs,
    ):
        """Initialize SlngSTTService.

        Args:
            api_key: Authentication key for the SLNG API.
            model: The transcription model to use. Defaults to "slng/deepgram/nova:3-en".
            base_url: The API host (without scheme). Defaults to "api.slng.ai".
            encoding: Audio encoding format. One of ``"linear16"``, ``"mp3"``,
                or ``"opus"``. Defaults to ``"linear16"``.
            sample_rate: Audio sample rate in Hz. If None, uses the pipeline sample rate.
            region_override: Pin requests to a specific datacenter.
            world_part_override: Constrain routing to a broad geographic zone.
            provider_key: Your own upstream provider API key (BYOK). Sent as the
                ``X-Slng-Provider-Key`` header on the WebSocket upgrade, so the
                provider bills your account directly. Only supported on external
                catalog routes (no ``slng/`` prefix), e.g. ``deepgram/nova:3``;
                SLNG-hosted ``slng/...`` routes reject it with a 400. A rejected
                key surfaces as a ``backend_connection_failed`` error frame with
                the upstream 401/403 detail. See
                https://docs.slng.ai/execution-layer/byok.
            language: Recognition language. Defaults to ``Language.EN`` when not given.
            enable_vad: Enable server-side VAD. Defaults to ``True`` when not given.
            enable_partials: Stream partial (interim) transcripts. Defaults to
                ``True`` when not given.
            settings: Runtime-updatable settings override. Merged on top of any
                explicit kwargs above.
            **kwargs: Additional arguments passed to parent WebsocketSTTService.
        """
        default_settings = self.Settings(
            model=model,
            language=language if is_given(language) else Language.EN,
            enable_vad=enable_vad if is_given(enable_vad) else True,
            enable_partials=enable_partials if is_given(enable_partials) else True,
        )

        if settings is not None:
            default_settings.apply_update(settings)

        super().__init__(
            sample_rate=sample_rate,
            keepalive_timeout=120,
            keepalive_interval=30,
            settings=default_settings,
            **kwargs,
        )

        self._api_key = api_key
        self._base_url = base_url
        self._encoding = encoding
        self._receive_task = None
        self._region_override = region_override
        self._world_part_override = world_part_override
        self._provider_key = provider_key
        self._ready_event = asyncio.Event()
        self._ready_timeout = 5.0

    def can_generate_metrics(self) -> bool:
        """Check if the service can generate processing metrics.

        Returns:
            True, indicating metrics are supported.
        """
        return True

    async def start(self, frame: StartFrame):
        """Start the STT service and establish the WebSocket connection.

        Args:
            frame: Frame indicating service should start.
        """
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        """Stop the STT service and close the WebSocket connection.

        Args:
            frame: Frame indicating service should stop.
        """
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        """Cancel the STT service and close the WebSocket connection.

        Args:
            frame: Frame indicating service should be cancelled.
        """
        await super().cancel(frame)
        await self._disconnect()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process incoming frames and handle speech events.

        Args:
            frame: The frame to process.
            direction: Direction of frame flow in the pipeline.
        """
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            await self.start_processing_metrics()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            if self._websocket and self._websocket.state is State.OPEN:
                await self._websocket.send(json.dumps({"type": "finalize"}))

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:  # ty: ignore[invalid-method-override]
        """Process audio data for speech-to-text transcription.

        Sends raw PCM audio bytes as a binary WebSocket frame. Waits for the
        server ``ready`` acknowledgement before sending; audio arriving before
        the server is ready causes the connection to close with WebSocket policy
        violation 1008.

        Args:
            audio: Raw PCM audio bytes to transcribe.

        Yields:
            None — transcription results are delivered via WebSocket responses.
        """
        if not self._websocket or self._websocket.state is not State.OPEN:
            await self._connect()

        if self._websocket is None:
            logger.warning(
                f"{self}: websocket unavailable after reconnect, dropping audio"
            )
            yield None
            return

        if not self._ready_event.is_set():
            try:
                await asyncio.wait_for(
                    self._ready_event.wait(), timeout=self._ready_timeout
                )
            except TimeoutError:
                logger.warning(f"{self}: init ack timed out, sending audio anyway")

        try:
            await self._websocket.send(audio)
        except Exception as e:
            error_msg = f"SLNG STT send failed: {e}"
            logger.warning(f"{self}: {error_msg}")
            await self.push_error(error_msg=error_msg, exception=e)
            yield ErrorFrame(error=error_msg)
            return
        yield None

    async def _send_keepalive(self, silence: bytes):
        """Send a ``KeepAlive`` JSON control frame.

        Args:
            silence: Silent PCM bytes (ignored; a ``KeepAlive`` JSON frame is sent instead).
        """
        if self._websocket is None:
            return
        try:
            await self._websocket.send(json.dumps({"type": "keepalive"}))
        except Exception as e:
            logger.warning(f"{self}: keepalive send failed: {e}")

    async def _connect(self):
        await self._connect_websocket()
        await super()._connect()
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
        """Build the Configure-message body from the current settings."""
        config: dict[str, Any] = {
            "sample_rate": self.sample_rate,
            "encoding": self._encoding,
        }

        if is_given(self._settings.language) and self._settings.language is not None:
            config["language"] = str(self._settings.language)

        if is_given(self._settings.enable_vad):
            config["enable_vad"] = bool(self._settings.enable_vad)

        if is_given(self._settings.enable_partials):
            config["enable_partials"] = bool(self._settings.enable_partials)

        return config

    async def _connect_websocket(self):
        """Establish the WebSocket connection and send the initial ``init`` message.

        The SLNG STT bridge requires an ``init`` text message before any audio
        bytes are accepted; otherwise the server closes the connection with
        WebSocket policy violation 1008. The server responds with a ``ready``
        message once the session is established.
        """
        try:
            if self._websocket and self._websocket.state is State.OPEN:
                return
            logger.debug(f"Connecting to SLNG STT ({self._settings.model})")

            model = self._settings.model
            if not is_given(model) or not model:
                model = _DEFAULT_STT_MODEL
            model_path = quote(model, safe="/:")
            if "://" in self._base_url:
                ws_url = f"{self._base_url}/v1/bridges/unmute/stt/{model_path}"
            else:
                ws_url = f"wss://{self._base_url}/v1/bridges/unmute/stt/{model_path}"

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

            config = self._build_config()
            await self._websocket.send(json.dumps({"type": "init", "config": config}))

            await self._call_event_handler("on_connected")
        except Exception as e:
            self._websocket = None
            # Community-integration guide (V4): push_error AND raise so the
            # PipelineRunner surfaces the failure instead of dribbling silent
            # send-after-disconnect errors.
            await self.push_error(
                error_msg=f"Unable to connect to SLNG STT: {e}", exception=e
            )
            raise

    async def _disconnect_websocket(self):
        """Send a ``CloseStream`` message and shut down the WebSocket."""
        ws = self._websocket
        try:
            if ws and ws.state is State.OPEN:
                logger.debug("Disconnecting from SLNG STT")
                await ws.send(json.dumps({"type": "close"}))
                await ws.close()
        except Exception as e:
            await self.push_error(
                error_msg=f"Error closing SLNG STT websocket: {e}", exception=e
            )
        finally:
            if self._websocket is ws:
                self._websocket = None
            await self._call_event_handler("on_disconnected")

    def _get_websocket(self):
        if self._websocket:
            return self._websocket
        raise Exception("SLNG STT websocket not connected")

    async def _receive_messages(self):
        """Receive and dispatch incoming WebSocket messages."""
        async for message in self._get_websocket():
            if isinstance(message, bytes):
                continue
            try:
                data = json.loads(message)
                await self._process_message(data)
            except json.JSONDecodeError:
                logger.warning(f"{self}: received non-JSON message: {message}")
            except Exception as e:
                logger.error(f"{self}: error processing message: {e}")

    async def _process_message(self, data: dict[str, Any]):
        """Dispatch a decoded server message.

        Handles messages emitted by the SLNG bridge, case-insensitively. The
        bridge typically emits ``Results`` (transcription) and ``Metadata``
        text frames, plus ``error`` payloads in either case.

        Args:
            data: Decoded JSON payload from the server.
        """
        msg_type = data.get("type") or ""
        type_lc = msg_type.lower() if isinstance(msg_type, str) else ""

        if type_lc == "ready":
            session_id = data.get("session_id", "")
            logger.debug(f"{self}: SLNG STT session ready (id={session_id})")
            self._ready_event.set()

        elif type_lc == "partial_transcript":
            await self._handle_transcript(data, is_final=False)

        elif type_lc == "final_transcript":
            await self._handle_transcript(data, is_final=True)

        elif type_lc == "utterance_end":
            logger.trace(f"{self}: SLNG STT utterance_end: {data}")

        elif type_lc == "error":
            raw = data.get("data")
            err = raw if isinstance(raw, dict) else {}
            error_msg = (
                err.get("message")
                or data.get("message")
                or err.get("code")
                or data.get("code")
                or f"Unknown SLNG STT error (payload: {data})"
            )
            logger.error(f"{self}: SLNG STT error: {error_msg}")
            await self.push_error(error_msg=str(error_msg))
            await self.stop_all_metrics()

        else:
            logger.debug(f"{self}: unknown message: {data}")

    async def _handle_transcript(self, data: dict[str, Any], *, is_final: bool):
        """Handle a ``partial_transcript`` or ``final_transcript`` message.

        Per the Unmute bridge spec, ``transcript`` is at the top level. We also
        fall back to ``channel.alternatives[0].transcript`` because some
        upstream providers (e.g. Deepgram) include the full Deepgram payload
        passed through.

        When the bridge surfaces a top-level ``confidence`` score (optional in
        the AsyncAPI spec), transcripts below 0.5 are dropped per the Pipecat
        community-integration guide ("filter for values >50% confidence").
        """
        transcript = (data.get("transcript") or "").strip()
        if not transcript:
            channel = data.get("channel") or {}
            alternatives = channel.get("alternatives") or []
            if alternatives:
                transcript = (alternatives[0].get("transcript") or "").strip()
        if not transcript:
            return

        confidence = data.get("confidence")
        if (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and confidence < 0.5
        ):
            logger.trace(
                f"{self}: dropping low-confidence transcript "
                f"(confidence={confidence:.2f}, transcript={transcript!r})"
            )
            return

        language: Language | None = None
        if raw_lang := data.get("language"):
            try:
                language = Language(raw_lang)
            except ValueError:
                pass

        if is_final:
            if data.get("from_finalize"):
                self.confirm_finalize()
            await self.push_frame(
                TranscriptionFrame(
                    transcript,
                    self._user_id,
                    time_now_iso8601(),
                    language,
                    result=data,
                )
            )
            await self._handle_transcription(transcript, True, language)
            await self.stop_processing_metrics()
        else:
            await self.push_frame(
                InterimTranscriptionFrame(
                    transcript,
                    self._user_id,
                    time_now_iso8601(),
                    language,
                    result=data,
                )
            )

    @traced_stt
    async def _handle_transcription(
        self, transcript: str, is_final: bool, language: Language | None = None
    ):
        """Handle a transcription result with tracing.

        Args:
            transcript: The transcribed text.
            is_final: Whether this is a final (not interim) transcription.
            language: Detected or configured language.
        """
        pass

    async def _update_settings(self, delta: STTSettings) -> dict[str, Any]:
        """Apply a settings delta and reconnect if needed.

        Args:
            delta: A settings delta to apply.

        Returns:
            Dict mapping changed field names to their previous values.
        """
        changed = await super()._update_settings(delta)
        if not changed:
            return changed
        await self._request_reconnect()
        return changed
