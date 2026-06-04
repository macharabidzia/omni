import asyncio
import logging
from dataclasses import dataclass
from uuid import uuid4

from fastapi import WebSocket

from src.config import Settings
from src.realtime.audio import AudioValidationError, validate_audio_chunk
from src.realtime.event_map import normalize_qwen_event
from src.realtime.metrics import SessionMetrics
from src.realtime.qwen_chat_client import QwenChatClient
from src.realtime.qwen_client import QwenEvent, QwenRealtimeClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BrowserSessionConfig:
    speaker: str
    modalities: list[str]
    input_sample_rate: int
    output_audio: bool


class RealtimeSession:
    def __init__(self, *, websocket: WebSocket, settings: Settings) -> None:
        self.websocket = websocket
        self.settings = settings
        self.session_id = str(uuid4())
        self.browser_config: BrowserSessionConfig | None = None
        self.qwen_client: QwenRealtimeClient | None = None
        self.qwen_chat_client: QwenChatClient | None = None
        self.qwen_reader_task: asyncio.Task | None = None
        self.text_response_task: asyncio.Task | None = None
        self.metrics = SessionMetrics()
        self.last_metrics_snapshot: dict | None = None
        self.send_lock = asyncio.Lock()
        self.closed = False
        self.ignore_model_audio = False
        self.buffered_audio_bytes = bytearray()
        self.assistant_output_gate_open = True
        self.buffered_assistant_events: list[QwenEvent] = []
        self.browser_audio_chunk_count = 0
        self.browser_audio_total_bytes = 0
        self.assistant_audio_chunk_count = 0

    async def handle_browser_event(self, event: dict) -> bool:
        event_type = event.get("type")

        if event_type == "session.start":
            await self._start_session(event)
            return False

        if event_type == "audio.append":
            await self._append_audio(event)
            return False

        if event_type == "audio.commit":
            await self._commit_audio()
            return False

        if event_type == "response.cancel":
            await self._cancel_response()
            return False

        if event_type == "session.end":
            return True

        await self.send_error(
            code="UNSUPPORTED_EVENT",
            message=f"Unsupported browser event type: {event_type}",
        )
        return False

    async def send_event(self, payload: dict) -> None:
        if self.closed:
            return
        async with self.send_lock:
            await self.websocket.send_json(payload)

    async def send_error(self, *, code: str, message: str) -> None:
        await self.send_event(
            {
                "type": "error",
                "code": code,
                "message": message,
            }
        )

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._reset_runtime()

    async def _reset_runtime(self) -> None:
        await self._close_qwen_realtime_session()

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
        self.browser_audio_chunk_count = 0
        self.browser_audio_total_bytes = 0
        self.assistant_audio_chunk_count = 0

    def _build_qwen_realtime_client(self) -> QwenRealtimeClient:
        return QwenRealtimeClient(
            model=self.settings.qwen_model,
            url=self.settings.qwen_realtime_url,
            request_timeout_seconds=self.settings.qwen_request_timeout_seconds,
            response_timeout_seconds=self.settings.qwen_response_timeout_seconds,
            output_sample_rate=self.settings.output_sample_rate,
            max_ws_message_bytes=self.settings.max_ws_message_bytes,
            debug_raw_events=self.settings.qwen_debug_raw_events,
            instructions=self.settings.default_system_prompt,
        )

    async def _close_qwen_realtime_session(self) -> None:
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

    async def _open_qwen_realtime_session(self) -> None:
        assert self.browser_config is not None
        assert self.browser_config.output_audio

        self.qwen_client = self._build_qwen_realtime_client()
        await self.qwen_client.connect()
        await self.qwen_client.start_session(
            speaker=self.browser_config.speaker,
            modalities=self.browser_config.modalities,
            input_sample_rate=self.browser_config.input_sample_rate,
            output_audio=self.browser_config.output_audio,
        )
        self.qwen_reader_task = asyncio.create_task(self._pump_qwen_events())

    async def _restart_qwen_realtime_session(self) -> None:
        assert self.browser_config is not None
        assert self.browser_config.output_audio

        await self._close_qwen_realtime_session()
        self.ignore_model_audio = False
        self.buffered_assistant_events.clear()
        self.assistant_output_gate_open = False
        self.assistant_audio_chunk_count = 0
        await self._open_qwen_realtime_session()

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

        input_sample_rate = int(event.get("input_sample_rate", self.settings.input_sample_rate))
        if input_sample_rate != self.settings.input_sample_rate:
            await self.send_error(
                code="UNSUPPORTED_SAMPLE_RATE",
                message="Browser sessions must send 16 kHz PCM16 mono audio to the gateway.",
            )
            return

        output_audio = bool(event.get("output_audio", "audio" in modalities))
        await self._reset_runtime()
        self.browser_config = BrowserSessionConfig(
            speaker=speaker,
            modalities=modalities,
            input_sample_rate=input_sample_rate,
            output_audio=output_audio,
        )
        self.metrics = SessionMetrics()
        self.last_metrics_snapshot = None
        self.ignore_model_audio = False
        self.assistant_output_gate_open = not output_audio
        self.browser_audio_chunk_count = 0
        self.browser_audio_total_bytes = 0
        self.assistant_audio_chunk_count = 0

        if output_audio:
            try:
                await self._open_qwen_realtime_session()
            except Exception as exc:
                logger.exception("Failed to establish Qwen session")
                await self.send_error(
                    code="QWEN_UNAVAILABLE",
                    message=f"Qwen3-Omni realtime server is not available: {exc}",
                )
                return
        else:
            self.qwen_chat_client = QwenChatClient(
                model=self.settings.qwen_model,
                url=self.settings.qwen_chat_url,
                request_timeout_seconds=self.settings.qwen_response_timeout_seconds,
                system_prompt=self.settings.default_system_prompt,
                max_completion_tokens=self.settings.text_max_completion_tokens,
            )
        logger.info(
            "Session %s started speaker=%s modalities=%s output_audio=%s",
            self.session_id,
            speaker,
            ",".join(modalities),
            output_audio,
        )
        await self.send_event(
            {
                "type": "session.ready",
                "session_id": self.session_id,
            }
        )
        await self._send_metrics_update()

    async def _append_audio(self, event: dict) -> None:
        if self.browser_config is None:
            await self.send_error(
                code="SESSION_NOT_STARTED",
                message="Send session.start before audio.append.",
            )
            return

        try:
            validated = validate_audio_chunk(
                audio_base64=event.get("audio_base64", ""),
                sample_rate=int(event.get("sample_rate", self.settings.input_sample_rate)),
                channels=int(event.get("channels", 1)),
                audio_format=event.get("format", ""),
                allowed_chunk_ms=self.settings.allowed_chunk_ms,
            )
        except AudioValidationError as exc:
            await self.send_error(code=exc.code, message=exc.message)
            return

        self.metrics.mark_once("t_microphone_started")
        self.metrics.mark_once("t_first_audio_chunk_sent")
        if not self.browser_config.output_audio:
            self.buffered_audio_bytes.extend(validated.audio_bytes)
        self.browser_audio_chunk_count += 1
        self.browser_audio_total_bytes += len(validated.audio_bytes)
        if self.browser_audio_chunk_count == 1:
            logger.info(
                "Session %s received first browser audio chunk duration_ms=%d bytes=%d",
                self.session_id,
                validated.duration_ms,
                len(validated.audio_bytes),
            )

        if self.qwen_client is None:
            await self._send_metrics_update()
            return

        try:
            await self.qwen_client.append_audio(validated.audio_base64)
        except Exception as exc:
            await self.send_error(
                code="QWEN_APPEND_FAILED",
                message=f"Failed to forward audio to Qwen: {exc}",
            )
            return

        await self._send_metrics_update()

    async def _commit_audio(self) -> None:
        if self.browser_config is None:
            await self.send_error(
                code="SESSION_NOT_STARTED",
                message="Send session.start before audio.commit.",
            )
            return

        self.ignore_model_audio = False
        self.metrics.mark_once("t_audio_commit_sent")

        if not self.browser_config.output_audio:
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
            await self._send_metrics_update()
            return

        if self.browser_audio_chunk_count == 0:
            await self.send_error(
                code="EMPTY_AUDIO",
                message="No audio buffered for realtime commit.",
            )
            return

        try:
            assert self.qwen_client is not None
            self.assistant_output_gate_open = True
            await self._flush_buffered_assistant_events()
            logger.info(
                "Session %s committing %d audio chunks (%d bytes) to Qwen",
                self.session_id,
                self.browser_audio_chunk_count,
                self.browser_audio_total_bytes,
            )
            await self.qwen_client.commit_audio()
            self.browser_audio_chunk_count = 0
            self.browser_audio_total_bytes = 0
        except Exception as exc:
            await self.send_error(
                code="QWEN_COMMIT_FAILED",
                message=f"Failed to commit audio to Qwen: {exc}",
            )
            return

        await self._send_metrics_update()

    async def _cancel_response(self) -> None:
        if self.text_response_task is not None and not self.text_response_task.done():
            self.text_response_task.cancel()
            self.text_response_task = None
            self.metrics.mark_once("t_response_done")
            await self.send_event({"type": "assistant.done"})
            await self._send_metrics_update()
            return

        if self.qwen_client is None:
            return

        self.ignore_model_audio = True
        self.buffered_assistant_events.clear()
        self.metrics.mark_once("t_barge_in")
        try:
            await self._restart_qwen_realtime_session()
        except Exception as exc:
            await self.send_error(
                code="QWEN_CANCEL_FAILED",
                message=f"Failed to restart Qwen session after cancel: {exc}",
            )
            return

        await self._send_metrics_update()

    async def _pump_qwen_events(self) -> None:
        assert self.qwen_client is not None
        async for qwen_event in self.qwen_client.iter_events():
            if self.closed:
                return
            await self._handle_qwen_event(qwen_event)

    async def _handle_qwen_event(self, qwen_event: QwenEvent) -> None:
        if qwen_event.kind == "transcript_delta":
            self.metrics.mark_once("t_first_transcript_delta")
            await self._forward_qwen_event(qwen_event)
            return

        if self._should_buffer_assistant_event(qwen_event):
            self.buffered_assistant_events.append(qwen_event)
            return

        await self._forward_qwen_event(qwen_event)

    def _should_buffer_assistant_event(self, qwen_event: QwenEvent) -> bool:
        if self.browser_config is None or not self.browser_config.output_audio:
            return False
        if self.assistant_output_gate_open:
            return False
        return qwen_event.kind in {
            "assistant_text_delta",
            "assistant_audio_delta",
            "response_done",
        }

    async def _flush_buffered_assistant_events(self) -> None:
        if not self.buffered_assistant_events:
            return
        pending_events = self.buffered_assistant_events
        self.buffered_assistant_events = []
        for qwen_event in pending_events:
            await self._forward_qwen_event(qwen_event)

    async def _forward_qwen_event(self, qwen_event: QwenEvent) -> None:
        if qwen_event.kind == "assistant_text_delta":
            if self.metrics.mark_once("t_first_text_delta"):
                logger.info("Session %s received first assistant text delta", self.session_id)
        elif qwen_event.kind == "assistant_audio_delta":
            if self.metrics.mark_once("t_first_audio_delta_received"):
                logger.info("Session %s received first assistant audio delta", self.session_id)
            self.assistant_audio_chunk_count += 1
            if self.ignore_model_audio:
                return
        elif qwen_event.kind == "response_done":
            self.metrics.mark_once("t_response_done")
            logger.info(
                "Session %s response done assistant_audio_chunks=%d",
                self.session_id,
                self.assistant_audio_chunk_count,
            )
            self.assistant_audio_chunk_count = 0
        elif qwen_event.kind == "error":
            logger.error(
                "Session %s upstream Qwen error code=%s message=%s",
                self.session_id,
                qwen_event.code,
                qwen_event.message,
            )

        normalized_event = normalize_qwen_event(qwen_event)
        if normalized_event is not None:
            await self.send_event(normalized_event)
            await self._send_metrics_update()

    async def _send_metrics_update(self) -> None:
        snapshot = self.metrics.snapshot()
        if snapshot == self.last_metrics_snapshot:
            return
        self.last_metrics_snapshot = snapshot
        await self.send_event(
            {
                "type": "metrics.update",
                "metrics": snapshot["metrics"],
                "timestamps": snapshot["timestamps"],
            }
        )

    async def _run_text_only_completion(self) -> None:
        assert self.browser_config is not None
        assert self.qwen_chat_client is not None
        audio_pcm16 = bytes(self.buffered_audio_bytes)
        self.buffered_audio_bytes.clear()

        try:
            text = await self.qwen_chat_client.respond_text_only(
                audio_pcm16=audio_pcm16,
                sample_rate=self.browser_config.input_sample_rate,
            )
        except asyncio.CancelledError:
            self.text_response_task = None
            raise
        except Exception as exc:
            logger.exception("Failed text-only chat completion")
            self.text_response_task = None
            await self.send_error(
                code="QWEN_TEXT_COMPLETION_FAILED",
                message=f"Failed text-only completion: {exc}",
            )
            return

        self.metrics.mark_once("t_first_text_delta")
        await self.send_event({"type": "assistant.text.delta", "text": text})
        self.metrics.mark_once("t_response_done")
        await self.send_event({"type": "assistant.done"})
        await self._send_metrics_update()
        self.text_response_task = None
