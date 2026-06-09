import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable
from uuid import uuid4

from src.config import Settings
from src.realtime.audio import AudioValidationError, ValidatedAudioChunk, validate_audio_bytes, validate_audio_chunk
from src.realtime.event_map import normalize_qwen_event
from src.realtime.events import (
    PublicBackend,
    RuntimeGatewayEvent,
    SessionReadyEvent,
)
from src.realtime.metrics import SessionMetrics
from src.realtime.qwen_chat_client import QwenChatClient
from src.realtime.qwen_client import (
    QwenEvent,
    QwenRealtimeClient,
    is_qwen_connection_retryable_error,
)

logger = logging.getLogger(__name__)
_QWEN_REOPEN_RETRY_INTERVAL_SECONDS = 0.5
MAX_BUFFERED_ASSISTANT_EVENTS = 64

EventSink = Callable[[RuntimeGatewayEvent], Awaitable[None]]
BufferedAssistantEvent = tuple[int, QwenEvent]


@dataclass(slots=True)
class RealtimeSessionConfig:
    speaker: str
    modalities: list[str]
    input_sample_rate: int
    output_audio: bool


class RealtimeSession:
    def __init__(
        self,
        *,
        settings: Settings,
        emit_event: EventSink | None,
        participant_identity: str | None = None,
        room: str | None = None,
    ) -> None:
        if emit_event is None:
            raise ValueError("RealtimeSession requires an event sink.")
        self.emit_event = emit_event
        self.settings = settings
        self.participant_identity = participant_identity
        self.room = room
        self.session_id = str(uuid4())
        self.session_config: RealtimeSessionConfig | None = None
        self.qwen_client: QwenRealtimeClient | None = None
        self.qwen_chat_client: QwenChatClient | None = None
        self.qwen_reader_task: asyncio.Task | None = None
        self.audio_response_task: asyncio.Task | None = None
        self.text_response_task: asyncio.Task | None = None
        self.first_audio_timeout_task: asyncio.Task | None = None
        self.total_response_timeout_task: asyncio.Task | None = None
        self.response_watch_generation = 0
        self.metrics = SessionMetrics()
        self.last_metrics_snapshot: dict | None = None
        self.metrics_emit_requested = False
        self.send_lock = asyncio.Lock()
        self.qwen_session_lock = asyncio.Lock()
        self.closed = False
        self.ignore_model_audio = False
        self.output_version = 0
        self.buffered_audio_bytes = bytearray()
        self.assistant_output_gate_open = True
        self.buffered_assistant_events: list[BufferedAssistantEvent] = []
        self.precommit_assistant_output_started = False
        self.input_audio_chunk_count = 0
        self.input_audio_total_bytes = 0
        self.assistant_audio_chunk_count = 0
        self.upstream_response_pending = False
        self.turn_audio_chunks: list[str] = []
        self.turn_audio_total_bytes = 0
        self.max_turn_audio_buffer_bytes = max(
            1,
            int((self.settings.qwen_input_sample_rate * 2 * self.settings.livekit_max_turn_ms) / 1000),
        )
        self.qwen_reconnect_counts: dict[str, int] = {}
        self.qwen_last_reconnect_reason: str | None = None
        self.current_turn_id: str | None = None

    async def send_event(self, payload: RuntimeGatewayEvent) -> None:
        if self.closed:
            return
        async with self.send_lock:
            await self.emit_event(payload)

    async def send_error(self, *, code: str, message: str) -> None:
        await self.send_event(
            {
                "type": "error",
                "code": code,
                "message": message,
            }
        )

    def _backend_name(self) -> str:
        if self.session_config is None:
            return "inactive"
        if not self.session_config.output_audio:
            return "text_only"
        if self._uses_realtime_audio_backend():
            return "qwen_realtime_audio"
        return "qwen_chat_stream_audio"

    def _log_context(
        self,
        *,
        turn_id: str | None = None,
        reason: str | None = None,
    ) -> str:
        fields = [
            f"session_id={self.session_id}",
            f"participant={self.participant_identity}",
            f"room={self.room}",
            f"turn_id={turn_id or self.current_turn_id}",
            f"output_version={self.output_version}",
            f"backend={self._backend_name()}",
        ]
        if reason is not None:
            fields.append(f"reason={reason}")
        return " ".join(fields)

    async def close(self) -> None:
        if self.closed:
            return
        self.mark_turn_closed()
        await self._send_metrics_update()
        self._record_qwen_session_reason("shutdown")
        self.closed = True
        await self._reset_runtime()

    async def _reset_runtime(self) -> None:
        await self._cancel_response_watchdogs()
        await self._close_qwen_realtime_session()

        if self.audio_response_task is not None:
            self.audio_response_task.cancel()
            try:
                await self.audio_response_task
            except asyncio.CancelledError:
                pass
            self.audio_response_task = None

        if self.text_response_task is not None:
            self.text_response_task.cancel()
            try:
                await self.text_response_task
            except asyncio.CancelledError:
                pass
            self.text_response_task = None

        if self.qwen_client is not None:
            await self.qwen_client.close()
            self.qwen_client = None

        if self.qwen_chat_client is not None:
            await self.qwen_chat_client.close()
            self.qwen_chat_client = None

        self.buffered_audio_bytes.clear()
        self.buffered_assistant_events.clear()
        self.assistant_output_gate_open = True
        self.precommit_assistant_output_started = False
        self.input_audio_chunk_count = 0
        self.input_audio_total_bytes = 0
        self.assistant_audio_chunk_count = 0
        self.upstream_response_pending = False
        self._clear_turn_audio_buffer()

    def _clear_turn_audio_buffer(self) -> None:
        self.buffered_audio_bytes.clear()
        self.turn_audio_chunks.clear()
        self.turn_audio_total_bytes = 0

    def _request_metrics_emit(self) -> None:
        self.metrics_emit_requested = True

    async def _cancel_response_watchdogs(self) -> None:
        current_task = asyncio.current_task()
        tasks_to_await: list[asyncio.Task] = []
        for task_name in ("first_audio_timeout_task", "total_response_timeout_task"):
            task = getattr(self, task_name)
            if task is None:
                continue
            if task is not current_task:
                task.cancel()
                tasks_to_await.append(task)
            setattr(self, task_name, None)
        for task in tasks_to_await:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _arm_response_watchdogs(self, *, expect_first_audio: bool) -> None:
        await self._cancel_response_watchdogs()
        self.response_watch_generation += 1
        generation = self.response_watch_generation
        if expect_first_audio:
            self.first_audio_timeout_task = asyncio.create_task(
                self._run_first_audio_timeout_watchdog(generation),
                name=f"qwen-first-audio-timeout:{self.session_id}",
            )
        self.total_response_timeout_task = asyncio.create_task(
            self._run_total_response_timeout_watchdog(generation),
            name=f"qwen-total-response-timeout:{self.session_id}",
        )

    async def _run_first_audio_timeout_watchdog(self, generation: int) -> None:
        try:
            await asyncio.sleep(self.settings.qwen_first_audio_timeout_seconds)
            if generation != self.response_watch_generation:
                return
            if self.metrics.qwen_first_audio is not None:
                return
            await self._handle_response_timeout(
                code="QWEN_FIRST_AUDIO_TIMEOUT",
                message=(
                    "Timed out waiting for the first assistant audio chunk after commit."
                ),
                reason="first_audio_timeout",
            )
        except asyncio.CancelledError:
            return

    async def _run_total_response_timeout_watchdog(self, generation: int) -> None:
        try:
            await asyncio.sleep(self.settings.qwen_total_response_timeout_seconds)
            if generation != self.response_watch_generation:
                return
            await self._handle_response_timeout(
                code="QWEN_RESPONSE_TIMEOUT",
                message="Timed out waiting for the assistant response to finish.",
                reason="response_timeout",
            )
        except asyncio.CancelledError:
            return

    async def _handle_response_timeout(
        self,
        *,
        code: str,
        message: str,
        reason: str,
    ) -> None:
        if self.closed or not self.has_active_response():
            return

        logger.warning(
            "Timing out active response %s",
            self._log_context(reason=reason),
        )
        await self._cancel_response_watchdogs()

        if self.audio_response_task is not None and not self.audio_response_task.done():
            self.audio_response_task.cancel()
            try:
                await self.audio_response_task
            except asyncio.CancelledError:
                pass
            self.audio_response_task = None

        if self.text_response_task is not None and not self.text_response_task.done():
            self.text_response_task.cancel()
            try:
                await self.text_response_task
            except asyncio.CancelledError:
                pass
            self.text_response_task = None

        self.upstream_response_pending = False
        self.ignore_model_audio = True
        self.buffered_assistant_events.clear()
        self._clear_turn_audio_buffer()
        self.mark_turn_closed()
        self._request_metrics_emit()

        if (
            self.session_config is not None
            and self.session_config.output_audio
            and self._uses_realtime_audio_backend()
            and self.qwen_client is not None
        ):
            try:
                await self._restart_qwen_realtime_session(
                    advance_output_version=True,
                    reason=reason,
                )
            except Exception as exc:
                logger.exception(
                    "Failed to restart Qwen realtime session after timeout %s",
                    self._log_context(reason=reason),
                )
                await self.send_error(
                    code=code,
                    message=f"{message} Restart failed: {exc}",
                )
                await self._send_metrics_update()
                return

        await self.send_error(code=code, message=message)
        await self._send_metrics_update()

    def begin_turn(self, *, turn_id: str | None = None) -> None:
        self.metrics.reset_turn()
        self.metrics_emit_requested = False
        self.current_turn_id = turn_id

    def mark_vad_speech_start(self) -> None:
        self.metrics.mark_once("vad_speech_start")

    def mark_vad_speech_end(self) -> None:
        self.metrics.mark_once("vad_speech_end")

    def mark_turn_closed(self) -> None:
        if self.metrics.mark_once("turn_closed"):
            self._request_metrics_emit()

    def mark_browser_first_audio_played(self, *, latency_ms: float | None) -> None:
        if latency_ms is None:
            return
        rounded_latency_ms = round(max(float(latency_ms), 0.0), 2)
        if self.metrics.set_value_once("first_audio_played_client_ms", rounded_latency_ms):
            self._request_metrics_emit()

    async def flush_metrics_update(self) -> None:
        await self._send_metrics_update()

    async def mark_livekit_egress_started_with_queue_depth(
        self,
        *,
        queue_depth_ms: float | None,
    ) -> None:
        if not self.metrics.mark_once("first_livekit_frame_captured"):
            return
        rounded_queue_depth_ms = None
        if queue_depth_ms is not None:
            rounded_queue_depth_ms = round(max(float(queue_depth_ms), 0.0), 2)
        self.metrics.set_value_once(
            "queue_depth_at_first_frame_ms",
            rounded_queue_depth_ms,
        )
        self._request_metrics_emit()
        await self._send_metrics_update()

    def _build_qwen_realtime_client(self) -> QwenRealtimeClient:
        return QwenRealtimeClient(
            model=self.settings.qwen_model,
            url=self.settings.qwen_realtime_url,
            request_timeout_seconds=self.settings.qwen_request_timeout_seconds,
            response_timeout_seconds=self.settings.qwen_response_timeout_seconds,
            output_sample_rate=self.settings.qwen_output_sample_rate,
            max_ws_message_bytes=self.settings.max_ws_message_bytes,
            debug_raw_events=self.settings.qwen_debug_raw_events,
            instructions=self.settings.default_system_prompt,
        )

    def _build_qwen_chat_client(self) -> QwenChatClient:
        return QwenChatClient(
            model=self.settings.qwen_model,
            url=self.settings.qwen_chat_url,
            request_timeout_seconds=self.settings.qwen_response_timeout_seconds,
            system_prompt=self.settings.default_system_prompt,
            max_completion_tokens=self.settings.text_max_completion_tokens,
        )

    def _uses_realtime_audio_backend(self) -> bool:
        return self.settings.qwen_audio_backend == "realtime"

    async def _close_qwen_realtime_session(self) -> None:
        async with self.qwen_session_lock:
            await self._close_qwen_realtime_session_unlocked()

    async def _close_qwen_realtime_session_unlocked(self) -> None:
        if self.qwen_reader_task is not None:
            self.qwen_reader_task.cancel()
            try:
                await self.qwen_reader_task
            except asyncio.CancelledError:
                pass
            self.qwen_reader_task = None

        if self.qwen_client is not None:
            await self.qwen_client.close()
            self.qwen_client = None

    async def _open_qwen_realtime_session(self, *, reason: str = "startup") -> None:
        async with self.qwen_session_lock:
            await self._open_qwen_realtime_session_unlocked(reason=reason)

    async def _open_qwen_realtime_session_unlocked(self, *, reason: str = "startup") -> None:
        assert self.session_config is not None
        assert self.session_config.output_audio

        client = self._build_qwen_realtime_client()
        try:
            await client.connect()
            self.metrics.mark_latest("qwen_ws_connected")
            await client.start_session(
                speaker=self.session_config.speaker,
                modalities=self.session_config.modalities,
                input_sample_rate=self.session_config.input_sample_rate,
                output_audio=self.session_config.output_audio,
            )
            self.metrics.mark_latest("qwen_session_ready")
        except Exception:
            await client.close()
            raise
        self.qwen_client = client
        self._record_qwen_session_reason(reason)
        reader_output_version = self.output_version
        self.qwen_reader_task = asyncio.create_task(
            self._pump_qwen_events(reader_output_version)
        )

    async def _open_qwen_realtime_session_with_retry_unlocked(
        self,
        *,
        reason: str,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + self.settings.qwen_request_timeout_seconds
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._open_qwen_realtime_session_unlocked(reason=reason)
            except Exception as exc:
                await self._close_qwen_realtime_session_unlocked()
                remaining_seconds = deadline - asyncio.get_running_loop().time()
                if not is_qwen_connection_retryable_error(exc) or remaining_seconds <= 0:
                    raise
                retry_delay_seconds = min(
                    _QWEN_REOPEN_RETRY_INTERVAL_SECONDS,
                    remaining_seconds,
                )
                logger.warning(
                    "Waiting for Qwen realtime reopen attempt=%d retry_in=%.2fs error=%s %s",
                    attempt,
                    retry_delay_seconds,
                    exc,
                    self._log_context(reason=reason),
                )
                await asyncio.sleep(retry_delay_seconds)
                continue

            if attempt > 1:
                logger.info(
                    "Reopened Qwen realtime session attempts=%d %s",
                    attempt,
                    self._log_context(reason=reason),
                )
            return

    async def _restart_qwen_realtime_session(
        self,
        *,
        preserve_turn_audio: bool = False,
        advance_output_version: bool = True,
        reason: str = "unspecified",
    ) -> None:
        async with self.qwen_session_lock:
            await self._restart_qwen_realtime_session_unlocked(
                preserve_turn_audio=preserve_turn_audio,
                advance_output_version=advance_output_version,
                reason=reason,
            )

    async def _restart_qwen_realtime_session_unlocked(
        self,
        *,
        preserve_turn_audio: bool,
        advance_output_version: bool = True,
        reason: str,
    ) -> None:
        assert self.session_config is not None
        assert self.session_config.output_audio

        logger.warning(
            "Restarting Qwen realtime session preserve_turn_audio=%s buffered_turn_chunks=%d buffered_turn_bytes=%d %s",
            preserve_turn_audio,
            len(self.turn_audio_chunks),
            self.turn_audio_total_bytes,
            self._log_context(reason=reason),
        )
        if advance_output_version:
            self._bump_output_version(reason)
        await self._close_qwen_realtime_session_unlocked()
        self.ignore_model_audio = False
        self.buffered_assistant_events.clear()
        self.assistant_output_gate_open = False
        self.precommit_assistant_output_started = False
        self.assistant_audio_chunk_count = 0
        self.upstream_response_pending = False
        if not preserve_turn_audio:
            self._clear_turn_audio_buffer()
        await self._open_qwen_realtime_session_with_retry_unlocked(reason=reason)

    async def _ensure_qwen_session_for_buffered_turn(self, *, reason: str) -> None:
        if self.qwen_client is not None:
            return
        if self.session_config is None or not self.session_config.output_audio:
            return
        if not self.turn_audio_chunks:
            return

        logger.warning(
            "Reopening Qwen session before buffered turn replay buffered_turn_chunks=%d buffered_turn_bytes=%d %s",
            len(self.turn_audio_chunks),
            self.turn_audio_total_bytes,
            self._log_context(reason=reason),
        )
        async with self.qwen_session_lock:
            if self.qwen_client is None:
                await self._open_qwen_realtime_session_with_retry_unlocked(reason=reason)
                await self._replay_turn_audio_unlocked()

    async def _replay_turn_audio_unlocked(self) -> None:
        if not self.turn_audio_chunks:
            return
        assert self.qwen_client is not None

        for audio_base64 in self.turn_audio_chunks:
            await self.qwen_client.append_audio(audio_base64)

        logger.info(
            "Replayed buffered turn audio chunks=%d bytes=%d %s",
            len(self.turn_audio_chunks),
            self.turn_audio_total_bytes,
            self._log_context(),
        )

    async def _recover_qwen_turn_after_send_failure(
        self,
        *,
        failed_action: str,
        exc: Exception,
        commit_after_replay: bool,
    ) -> bool:
        if self.session_config is None or not self.session_config.output_audio:
            return False
        if not self.turn_audio_chunks:
            return False
        if not is_qwen_connection_retryable_error(exc):
            return False

        logger.warning(
            "Recovering Qwen turn after send failure action=%s buffered_turn_chunks=%d buffered_turn_bytes=%d error=%s %s",
            failed_action,
            len(self.turn_audio_chunks),
            self.turn_audio_total_bytes,
            exc,
            self._log_context(reason=f"{failed_action}_failure"),
        )
        try:
            async with self.qwen_session_lock:
                await self._restart_qwen_realtime_session_unlocked(
                    preserve_turn_audio=True,
                    advance_output_version=True,
                    reason=f"{failed_action}_failure",
                )
                if commit_after_replay:
                    self.assistant_output_gate_open = True
                await self._replay_turn_audio_unlocked()
                if commit_after_replay:
                    assert self.qwen_client is not None
                    await self.qwen_client.commit_audio()
                    self.upstream_response_pending = True
        except Exception:
            logger.exception(
                "Failed to recover Qwen turn after send failure action=%s %s",
                failed_action,
                self._log_context(reason=f"{failed_action}_failure"),
            )
            await self._close_qwen_realtime_session()
            return False

        logger.info(
            "Recovered Qwen turn after send failure action=%s %s",
            failed_action,
            self._log_context(reason=f"{failed_action}_recovered"),
        )
        return True

    async def _start_session(self, event: dict) -> None:
        speaker = event.get("speaker", self.settings.supported_speakers[0])
        if speaker not in self.settings.supported_speakers:
            await self.send_error(
                code="UNSUPPORTED_SPEAKER",
                message=f"Unsupported speaker '{speaker}'.",
            )
            return

        modalities = event.get("modalities") or list(self.settings.default_modalities)
        if not isinstance(modalities, list) or not all(
            modality in {"text", "audio"} for modality in modalities
        ):
            await self.send_error(
                code="INVALID_MODALITIES",
                message="Modalities must be a list containing 'text' and/or 'audio'.",
            )
            return

        input_sample_rate = int(event.get("input_sample_rate", self.settings.qwen_input_sample_rate))
        if input_sample_rate != self.settings.qwen_input_sample_rate:
            await self.send_error(
                code="UNSUPPORTED_SAMPLE_RATE",
                message="Input audio must be 16 kHz PCM16 mono.",
            )
            return

        output_audio = bool(event.get("output_audio", "audio" in modalities))
        await self._reset_runtime()
        self._bump_output_version("session_start")
        self.session_config = RealtimeSessionConfig(
            speaker=speaker,
            modalities=modalities,
            input_sample_rate=input_sample_rate,
            output_audio=output_audio,
        )
        self.metrics = SessionMetrics()
        self.last_metrics_snapshot = None
        self.metrics_emit_requested = True
        self.ignore_model_audio = False
        self.assistant_output_gate_open = not output_audio
        self.precommit_assistant_output_started = False
        self.input_audio_chunk_count = 0
        self.input_audio_total_bytes = 0
        self.assistant_audio_chunk_count = 0
        self.upstream_response_pending = False
        self._clear_turn_audio_buffer()
        self.current_turn_id = None
        self.qwen_reconnect_counts.clear()
        self.qwen_last_reconnect_reason = None

        if output_audio and self._uses_realtime_audio_backend():
            try:
                await self._open_qwen_realtime_session(reason="startup")
            except Exception as exc:
                logger.exception("Failed to establish Qwen session %s", self._log_context(reason="startup"))
                await self.send_error(
                    code="QWEN_UNAVAILABLE",
                    message=f"Qwen3-Omni realtime server is not available: {exc}",
                )
                return
        else:
            self.qwen_chat_client = self._build_qwen_chat_client()
        if not output_audio:
            backend: PublicBackend = "text_only"
        elif self._uses_realtime_audio_backend():
            backend = "qwen_realtime_audio"
        else:
            backend = "qwen_chat_stream_audio"
        logger.info(
            "Started realtime session speaker=%s modalities=%s output_audio=%s backend=%s %s",
            speaker,
            ",".join(modalities),
            output_audio,
            backend,
            self._log_context(),
        )
        ready_payload = SessionReadyEvent(
            type="session.ready",
            session_id=self.session_id,
            backend=backend,
        )
        if backend != "qwen_realtime_audio":
            ready_payload["degraded_reason"] = "non_realtime_audio_backend"
        await self.send_event(ready_payload)
        await self._send_metrics_update()

    async def start_session(self, event: dict) -> None:
        await self._start_session(event)

    async def _append_audio(self, event: dict) -> None:
        if self.session_config is None:
            await self.send_error(
                code="SESSION_NOT_STARTED",
                message="Start a session before streaming audio.",
            )
            return

        try:
            validated = validate_audio_chunk(
                audio_base64=event.get("audio_base64", ""),
                sample_rate=int(event.get("sample_rate", self.settings.qwen_input_sample_rate)),
                channels=int(event.get("channels", 1)),
                audio_format=event.get("format", ""),
                allowed_chunk_ms=self.settings.allowed_chunk_ms,
            )
        except AudioValidationError as exc:
            await self.send_error(code=exc.code, message=exc.message)
            return

        await self._append_validated_audio(validated)

    async def _append_validated_audio(self, validated: ValidatedAudioChunk) -> None:
        if self.session_config is None:
            await self.send_error(
                code="SESSION_NOT_STARTED",
                message="Start a session before streaming audio.",
            )
            return

        is_first_chunk_of_turn = self.input_audio_chunk_count == 0
        if (
            self.session_config.output_audio
            and self.qwen_client is not None
            and is_first_chunk_of_turn
            and self.upstream_response_pending
        ):
            logger.info(
                "Rotating stale upstream Qwen session before new turn %s",
                self._log_context(reason="stale_upstream_before_new_turn"),
            )
            try:
                await self._restart_qwen_realtime_session(reason="stale_upstream_before_new_turn")
            except Exception as exc:
                await self.send_error(
                    code="QWEN_STALE_SESSION_RESET_FAILED",
                    message=f"Failed to reset stale Qwen session before new turn: {exc}",
                )
                return
        if is_first_chunk_of_turn and not self.upstream_response_pending:
            self._clear_turn_audio_buffer()
        next_turn_audio_total_bytes = self.turn_audio_total_bytes + len(validated.audio_bytes)
        if next_turn_audio_total_bytes > self.max_turn_audio_buffer_bytes:
            await self.send_error(
                code="TURN_AUDIO_LIMIT_EXCEEDED",
                message=(
                    "Buffered turn audio exceeded the configured maximum turn length of "
                    f"{self.settings.livekit_max_turn_ms} ms."
                ),
            )
            return
        if not self.session_config.output_audio or not self._uses_realtime_audio_backend():
            self.buffered_audio_bytes.extend(validated.audio_bytes)
            if self.metrics.mark_once("first_user_audio_uploaded"):
                logger.info(
                    "Buffered first fallback input audio chunk duration_ms=%d bytes=%d %s",
                    validated.duration_ms,
                    len(validated.audio_bytes),
                    self._log_context(),
                )
            self.metrics.mark_latest("last_user_audio_uploaded")

        encoded_audio_base64 = validated.audio_base64
        if encoded_audio_base64 is None:
            encoded_audio_base64 = base64.b64encode(validated.audio_bytes).decode("ascii")

        self.turn_audio_chunks.append(encoded_audio_base64)
        self.turn_audio_total_bytes = next_turn_audio_total_bytes
        self.input_audio_chunk_count += 1
        self.input_audio_total_bytes += len(validated.audio_bytes)
        if self.input_audio_chunk_count == 1:
            logger.info(
                "Received first input audio chunk duration_ms=%d bytes=%d %s",
                validated.duration_ms,
                len(validated.audio_bytes),
                self._log_context(),
            )

        if self.qwen_client is None:
            await self._send_metrics_update()
            return

        try:
            await self.qwen_client.append_audio(encoded_audio_base64)
        except Exception as exc:
            recovered = await self._recover_qwen_turn_after_send_failure(
                failed_action="append",
                exc=exc,
                commit_after_replay=False,
            )
            if recovered:
                self.metrics.mark_once("first_user_audio_uploaded")
                self.metrics.mark_latest("last_user_audio_uploaded")
                await self._send_metrics_update()
                return
            await self._close_qwen_realtime_session()
            await self.send_error(
                code="QWEN_APPEND_FAILED",
                message=f"Failed to forward audio to Qwen: {exc}",
            )
            return

        self.metrics.mark_once("first_user_audio_uploaded")
        self.metrics.mark_latest("last_user_audio_uploaded")

        await self._send_metrics_update()

    async def append_audio_chunk(
        self,
        *,
        audio_base64: str,
        sample_rate: int,
        channels: int = 1,
        audio_format: str = "pcm16",
    ) -> None:
        await self._append_audio(
            {
                "audio_base64": audio_base64,
                "sample_rate": sample_rate,
                "channels": channels,
                "format": audio_format,
            }
        )

    async def append_audio_bytes(
        self,
        *,
        audio_bytes: bytes | bytearray | memoryview,
        sample_rate: int,
        channels: int = 1,
        audio_format: str = "pcm16",
    ) -> None:
        if self.session_config is None:
            await self.send_error(
                code="SESSION_NOT_STARTED",
                message="Start a session before streaming audio.",
            )
            return

        try:
            validated = validate_audio_bytes(
                audio_bytes=audio_bytes,
                sample_rate=sample_rate,
                channels=channels,
                audio_format=audio_format,
                allowed_chunk_ms=self.settings.allowed_chunk_ms,
            )
        except AudioValidationError as exc:
            await self.send_error(code=exc.code, message=exc.message)
            return

        await self._append_validated_audio(validated)

    async def _commit_audio(self) -> None:
        if self.session_config is None:
            await self.send_error(
                code="SESSION_NOT_STARTED",
                message="Start a session before committing audio.",
            )
            return

        self.ignore_model_audio = False
        self.metrics.mark_once("commit_sent")

        if not self.session_config.output_audio:
            if self.text_response_task is not None and not self.text_response_task.done():
                await self.send_error(
                    code="RESPONSE_IN_PROGRESS",
                    message="Wait for the active text response to finish before committing more audio.",
                )
                return
            if not self.buffered_audio_bytes:
                await self.send_error(
                    code="EMPTY_AUDIO",
                    message="No audio buffered for text-only completion.",
                )
                return
            self.text_response_task = asyncio.create_task(self._run_text_only_completion())
            await self._arm_response_watchdogs(expect_first_audio=False)
            await self._send_metrics_update()
            return

        if not self._uses_realtime_audio_backend():
            if self.audio_response_task is not None and not self.audio_response_task.done():
                await self.send_error(
                    code="RESPONSE_IN_PROGRESS",
                    message="Wait for the active audio response to finish before committing more audio.",
                )
                return
            audio_pcm16 = bytes(self.buffered_audio_bytes)
            if not audio_pcm16:
                await self.send_error(
                    code="EMPTY_AUDIO",
                    message="No audio buffered for realtime commit.",
                )
                return
            assert self.qwen_chat_client is not None
            self.assistant_output_gate_open = True
            self.precommit_assistant_output_started = False
            logger.info(
                "Session %s streaming %d audio chunks (%d bytes) through Qwen chat completions",
                self.session_id,
                self.input_audio_chunk_count,
                self.input_audio_total_bytes,
            )
            self.audio_response_task = asyncio.create_task(
                self._run_streaming_audio_completion(
                    audio_pcm16=audio_pcm16,
                    sample_rate=self.session_config.input_sample_rate,
                    speaker=self.session_config.speaker,
                    output_version=self.output_version,
                )
            )
            self.upstream_response_pending = True
            await self._arm_response_watchdogs(expect_first_audio=True)
            self.input_audio_chunk_count = 0
            self.input_audio_total_bytes = 0
            self._clear_turn_audio_buffer()
            await self._send_metrics_update()
            return

        if self.input_audio_chunk_count == 0:
            await self.send_error(
                code="EMPTY_AUDIO",
                message="No audio buffered for realtime commit.",
            )
            return

        try:
            await self._ensure_qwen_session_for_buffered_turn(reason="commit")
            assert self.qwen_client is not None
            response_already_started = self.precommit_assistant_output_started
            if response_already_started:
                self.upstream_response_pending = True
            self.assistant_output_gate_open = True
            await self._flush_buffered_assistant_events()
            self.precommit_assistant_output_started = False
            if response_already_started:
                await self._arm_response_watchdogs(expect_first_audio=False)
                logger.info(
                    "Skipping explicit commit because upstream response already started %s",
                    self._log_context(),
                )
                self.input_audio_chunk_count = 0
                self.input_audio_total_bytes = 0
                await self._send_metrics_update()
                return
            logger.info(
                "Committing audio to Qwen input_chunks=%d input_bytes=%d %s",
                self.input_audio_chunk_count,
                self.input_audio_total_bytes,
                self._log_context(),
            )
            await self.qwen_client.commit_audio()
            self.upstream_response_pending = True
            await self._arm_response_watchdogs(expect_first_audio=True)
            self.input_audio_chunk_count = 0
            self.input_audio_total_bytes = 0
        except Exception as exc:
            recovered = await self._recover_qwen_turn_after_send_failure(
                failed_action="commit",
                exc=exc,
                commit_after_replay=True,
            )
            if recovered:
                self.input_audio_chunk_count = 0
                self.input_audio_total_bytes = 0
                await self._arm_response_watchdogs(expect_first_audio=True)
                await self._send_metrics_update()
                return
            await self._close_qwen_realtime_session()
            await self.send_error(
                code="QWEN_COMMIT_FAILED",
                message=f"Failed to commit audio to Qwen: {exc}",
            )
            return

        await self._send_metrics_update()

    async def commit_audio(self) -> None:
        await self._commit_audio()

    async def _cancel_response(self) -> None:
        if self.audio_response_task is not None and not self.audio_response_task.done():
            self._bump_output_version("client_interrupt")
            await self._cancel_response_watchdogs()
            self.audio_response_task.cancel()
            try:
                await self.audio_response_task
            except asyncio.CancelledError:
                pass
            self.audio_response_task = None
            self.upstream_response_pending = False
            self.buffered_assistant_events.clear()
            self._clear_turn_audio_buffer()
            self.metrics.mark_once("barge_in")
            self.mark_turn_closed()
            self._request_metrics_emit()
            await self._send_metrics_update()
            return

        if self.text_response_task is not None and not self.text_response_task.done():
            self._bump_output_version("client_interrupt")
            await self._cancel_response_watchdogs()
            self.text_response_task.cancel()
            self.text_response_task = None
            self.metrics.mark_once("barge_in")
            self.mark_turn_closed()
            self._request_metrics_emit()
            await self.send_event({"type": "assistant.done"})
            await self._send_metrics_update()
            return

        if self.qwen_client is None:
            return

        self._bump_output_version("client_interrupt")
        await self._cancel_response_watchdogs()
        self.ignore_model_audio = True
        self.buffered_assistant_events.clear()
        self._clear_turn_audio_buffer()
        self.metrics.mark_once("barge_in")
        self.mark_turn_closed()
        self._request_metrics_emit()
        try:
            await self.qwen_client.cancel_response()
        except Exception:
            logger.exception("Failed to cancel Qwen response %s", self._log_context(reason="client_interrupt"))
        try:
            await self._restart_qwen_realtime_session(
                advance_output_version=False,
                reason="client_interrupt",
            )
        except Exception as exc:
            await self.send_error(
                code="QWEN_CANCEL_FAILED",
                message=f"Failed to restart Qwen session after cancel: {exc}",
            )
            return

        await self._send_metrics_update()

    async def cancel_response(self) -> None:
        await self._cancel_response()

    def mark_livekit_egress_started(self) -> None:
        if self.metrics.mark_once("first_livekit_frame_captured"):
            self._request_metrics_emit()

    def _bump_output_version(self, reason: str) -> int:
        self.output_version += 1
        self.buffered_assistant_events.clear()
        logger.info(
            "Advanced output version to %d %s",
            self.output_version,
            self._log_context(reason=reason),
        )
        return self.output_version

    def _is_current_output_version(self, output_version: int | None) -> bool:
        return output_version is None or output_version == self.output_version

    def _log_stale_qwen_event(self, qwen_event: QwenEvent, output_version: int | None) -> None:
        if qwen_event.kind == "assistant_audio_delta":
            logger.info(
                "Dropped stale assistant audio delta event_version=%s current_output_version=%d %s",
                output_version,
                self.output_version,
                self._log_context(),
            )
            return
        logger.debug(
            "Dropped stale Qwen event kind=%s event_version=%s current_output_version=%d %s",
            qwen_event.kind,
            output_version,
            self.output_version,
            self._log_context(),
        )

    def _record_qwen_session_reason(self, reason: str) -> None:
        normalized_reason = self._normalize_qwen_session_reason(reason)
        if not normalized_reason:
            return
        self.qwen_reconnect_counts[normalized_reason] = (
            self.qwen_reconnect_counts.get(normalized_reason, 0) + 1
        )
        self.qwen_last_reconnect_reason = normalized_reason

    @staticmethod
    def _normalize_qwen_session_reason(reason: str) -> str:
        normalized_reason = reason.strip().lower()
        return {
            "startup": "startup",
            "client_interrupt": "interrupt",
            "stale_upstream_before_new_turn": "stale_previous_response",
            "append_failure": "append_recovery",
            "commit_failure": "commit_recovery",
            "first_audio_timeout": "first_audio_timeout",
            "response_timeout": "response_timeout",
            "upstream_closed": "upstream_closed",
            "shutdown": "shutdown",
        }.get(normalized_reason, normalized_reason)

    async def _pump_qwen_events(self, output_version: int) -> None:
        assert self.qwen_client is not None
        async for qwen_event in self.qwen_client.iter_events():
            if self.closed:
                return
            await self._handle_qwen_event(qwen_event, output_version=output_version)

    async def _handle_qwen_event(
        self,
        qwen_event: QwenEvent,
        *,
        output_version: int | None = None,
    ) -> None:
        if not self._is_current_output_version(output_version):
            self._log_stale_qwen_event(qwen_event, output_version)
            return

        event_output_version = self.output_version if output_version is None else output_version
        if qwen_event.kind == "transcript_delta":
            self.metrics.mark_once("qwen_first_transcript")
            await self._forward_qwen_event(qwen_event, output_version=event_output_version)
            return

        if self._should_buffer_assistant_event(qwen_event):
            self.precommit_assistant_output_started = True
            self._buffer_assistant_event(event_output_version, qwen_event)
            return

        await self._forward_qwen_event(qwen_event, output_version=event_output_version)

    def _should_buffer_assistant_event(self, qwen_event: QwenEvent) -> bool:
        if self.session_config is None or not self.session_config.output_audio:
            return False
        if self.assistant_output_gate_open:
            return False
        return qwen_event.kind in {
            "assistant_text_delta",
            "assistant_audio_delta",
            "response_done",
        }

    def _buffer_assistant_event(self, output_version: int, qwen_event: QwenEvent) -> None:
        if len(self.buffered_assistant_events) >= MAX_BUFFERED_ASSISTANT_EVENTS:
            dropped_index = self._select_buffered_assistant_drop_index()
            dropped_output_version, dropped_event = self.buffered_assistant_events.pop(
                dropped_index
            )
            logger.warning(
                "Dropping buffered precommit assistant event kind=%s event_output_version=%s pending_count=%d max_pending=%d %s",
                dropped_event.kind,
                dropped_output_version,
                len(self.buffered_assistant_events),
                MAX_BUFFERED_ASSISTANT_EVENTS,
                self._log_context(),
            )
        self.buffered_assistant_events.append((output_version, qwen_event))

    def _select_buffered_assistant_drop_index(self) -> int:
        preferred_kinds = (
            "assistant_text_delta",
            "assistant_audio_delta",
            "response_done",
        )
        for preferred_kind in preferred_kinds:
            for index, (_output_version, buffered_event) in enumerate(
                self.buffered_assistant_events
            ):
                if buffered_event.kind == preferred_kind:
                    return index
        return 0

    async def _flush_buffered_assistant_events(self) -> None:
        if not self.buffered_assistant_events:
            return
        pending_events = self.buffered_assistant_events
        self.buffered_assistant_events = []
        for output_version, qwen_event in pending_events:
            await self._forward_qwen_event(qwen_event, output_version=output_version)

    async def _forward_qwen_event(
        self,
        qwen_event: QwenEvent,
        *,
        output_version: int | None,
    ) -> None:
        if not self._is_current_output_version(output_version):
            self._log_stale_qwen_event(qwen_event, output_version)
            return

        if qwen_event.kind == "assistant_text_delta":
            if self.metrics.mark_once("qwen_first_text"):
                logger.info(
                    "Received first assistant text delta %s",
                    self._log_context(),
                )
        elif qwen_event.kind == "assistant_audio_delta":
            if self.first_audio_timeout_task is not None:
                self.first_audio_timeout_task.cancel()
                self.first_audio_timeout_task = None
            if self.metrics.mark_once("qwen_first_audio"):
                logger.info(
                    "Received first assistant audio delta %s",
                    self._log_context(),
                )
            self.assistant_audio_chunk_count += 1
            if self.ignore_model_audio:
                return
        elif qwen_event.kind == "response_done":
            await self._cancel_response_watchdogs()
            self.upstream_response_pending = False
            self.metrics.mark_once("assistant_done")
            self.mark_turn_closed()
            self._request_metrics_emit()
            logger.info(
                "Assistant response done assistant_audio_chunks=%d %s",
                self.assistant_audio_chunk_count,
                self._log_context(),
            )
            self.assistant_audio_chunk_count = 0
            self._clear_turn_audio_buffer()
        elif qwen_event.kind == "error":
            await self._cancel_response_watchdogs()
            self.upstream_response_pending = False
            self._clear_turn_audio_buffer()
            self.mark_turn_closed()
            self._request_metrics_emit()
            if qwen_event.code == "QWEN_CONNECTION_CLOSED":
                self._record_qwen_session_reason("upstream_closed")
            logger.error(
                "Upstream Qwen error code=%s message=%s %s",
                qwen_event.code,
                qwen_event.message,
                self._log_context(),
            )

        normalized_event = normalize_qwen_event(qwen_event)
        if normalized_event is not None:
            if qwen_event.kind in {
                "assistant_text_delta",
                "assistant_audio_delta",
                "response_done",
            }:
                normalized_event["output_version"] = output_version
            await self.send_event(normalized_event)
            await self._send_metrics_update()

    async def _send_metrics_update(self) -> None:
        if self.last_metrics_snapshot is not None and not self.metrics_emit_requested:
            return
        snapshot = self.metrics.snapshot()
        payload: RuntimeGatewayEvent = {
            "type": "metrics.update",
            "metrics": snapshot["metrics"],
            "timestamps": snapshot["timestamps"],
        }
        reconnects = dict(sorted(self.qwen_reconnect_counts.items()))
        if reconnects:
            payload["reconnects"] = reconnects
            payload["last_reconnect_reason"] = self.qwen_last_reconnect_reason
        if payload == self.last_metrics_snapshot:
            self.metrics_emit_requested = False
            return
        self.last_metrics_snapshot = payload
        self.metrics_emit_requested = False
        await self.send_event(payload)

    async def _run_streaming_audio_completion(
        self,
        *,
        audio_pcm16: bytes,
        sample_rate: int,
        speaker: str,
        output_version: int,
    ) -> None:
        assert self.qwen_chat_client is not None
        try:
            async for qwen_event in self.qwen_chat_client.stream_audio_response(
                audio_pcm16=audio_pcm16,
                sample_rate=sample_rate,
                speaker=speaker,
            ):
                if self.closed:
                    return
                await self._handle_qwen_event(qwen_event, output_version=output_version)
        except asyncio.CancelledError:
            self.upstream_response_pending = False
            raise
        except Exception as exc:
            logger.exception("Failed streaming audio chat completion %s", self._log_context())
            await self._cancel_response_watchdogs()
            self.upstream_response_pending = False
            self._clear_turn_audio_buffer()
            self.mark_turn_closed()
            self._request_metrics_emit()
            await self.send_error(
                code="QWEN_AUDIO_COMPLETION_FAILED",
                message=f"Failed streaming audio completion: {exc}",
            )
            await self._send_metrics_update()
        finally:
            self.audio_response_task = None

    async def _run_text_only_completion(self) -> None:
        assert self.session_config is not None
        assert self.qwen_chat_client is not None
        audio_pcm16 = bytes(self.buffered_audio_bytes)
        self.buffered_audio_bytes.clear()

        try:
            text = await self.qwen_chat_client.respond_text_only(
                audio_pcm16=audio_pcm16,
                sample_rate=self.session_config.input_sample_rate,
            )
        except asyncio.CancelledError:
            self.text_response_task = None
            raise
        except Exception as exc:
            logger.exception("Failed text-only chat completion %s", self._log_context())
            await self._cancel_response_watchdogs()
            self.text_response_task = None
            self.mark_turn_closed()
            self._request_metrics_emit()
            await self.send_error(
                code="QWEN_TEXT_COMPLETION_FAILED",
                message=f"Failed text-only completion: {exc}",
            )
            await self._send_metrics_update()
            return

        self.metrics.mark_once("qwen_first_text")
        await self._cancel_response_watchdogs()
        await self.send_event({"type": "assistant.text.delta", "text": text})
        self.metrics.mark_once("assistant_done")
        self.mark_turn_closed()
        self._request_metrics_emit()
        await self.send_event({"type": "assistant.done"})
        await self._send_metrics_update()
        self.text_response_task = None

    def has_active_response(self) -> bool:
        if self.audio_response_task is not None and not self.audio_response_task.done():
            return True
        if self.text_response_task is not None and not self.text_response_task.done():
            return True
        return self.upstream_response_pending
