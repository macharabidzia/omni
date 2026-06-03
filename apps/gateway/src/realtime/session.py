import asyncio
import logging
from dataclasses import dataclass
from uuid import uuid4

from fastapi import WebSocket

from src.config import Settings
from src.realtime.audio import AudioValidationError, validate_audio_chunk
from src.realtime.event_map import normalize_qwen_event
from src.realtime.metrics import SessionMetrics
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
        self.qwen_reader_task: asyncio.Task | None = None
        self.metrics = SessionMetrics()
        self.send_lock = asyncio.Lock()
        self.closed = False
        self.ignore_model_audio = False

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

        if self.qwen_reader_task is not None:
            self.qwen_reader_task.cancel()
            try:
                await self.qwen_reader_task
            except asyncio.CancelledError:
                pass

        if self.qwen_client is not None:
            await self.qwen_client.close()

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
        self.browser_config = BrowserSessionConfig(
            speaker=speaker,
            modalities=modalities,
            input_sample_rate=input_sample_rate,
            output_audio=output_audio,
        )
        self.metrics = SessionMetrics()

        self.qwen_client = QwenRealtimeClient(
            model=self.settings.qwen_model,
            url=self.settings.qwen_realtime_url,
            request_timeout_seconds=self.settings.qwen_request_timeout_seconds,
            response_timeout_seconds=self.settings.qwen_response_timeout_seconds,
            output_sample_rate=self.settings.output_sample_rate,
            max_ws_message_bytes=self.settings.max_ws_message_bytes,
            debug_raw_events=self.settings.qwen_debug_raw_events,
            instructions=self.settings.default_system_prompt,
        )

        try:
            await self.qwen_client.connect()
            await self.qwen_client.start_session(
                speaker=speaker,
                modalities=modalities,
                input_sample_rate=input_sample_rate,
                output_audio=output_audio,
            )
        except Exception as exc:
            logger.exception("Failed to establish Qwen session")
            await self.send_error(
                code="QWEN_UNAVAILABLE",
                message=f"Qwen3-Omni realtime server is not available: {exc}",
            )
            return

        self.qwen_reader_task = asyncio.create_task(self._pump_qwen_events())
        await self.send_event(
            {
                "type": "session.ready",
                "session_id": self.session_id,
            }
        )
        await self._send_metrics_update()

    async def _append_audio(self, event: dict) -> None:
        if self.qwen_client is None or self.browser_config is None:
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
        if self.qwen_client is None:
            await self.send_error(
                code="SESSION_NOT_STARTED",
                message="Send session.start before audio.commit.",
            )
            return

        self.ignore_model_audio = False
        self.metrics.mark_once("t_audio_commit_sent")
        try:
            await self.qwen_client.commit_audio()
        except Exception as exc:
            await self.send_error(
                code="QWEN_COMMIT_FAILED",
                message=f"Failed to commit audio to Qwen: {exc}",
            )
            return

        await self._send_metrics_update()

    async def _cancel_response(self) -> None:
        if self.qwen_client is None:
            return

        self.ignore_model_audio = True
        self.metrics.mark_once("t_barge_in")
        try:
            await self.qwen_client.cancel_response()
        except Exception as exc:
            await self.send_error(
                code="QWEN_CANCEL_FAILED",
                message=f"Failed to cancel active response: {exc}",
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
        elif qwen_event.kind == "assistant_text_delta":
            self.metrics.mark_once("t_first_text_delta")
        elif qwen_event.kind == "assistant_audio_delta":
            self.metrics.mark_once("t_first_audio_delta_received")
            if self.ignore_model_audio:
                return
        elif qwen_event.kind == "response_done":
            self.metrics.mark_once("t_response_done")

        normalized_event = normalize_qwen_event(qwen_event)
        if normalized_event is not None:
            await self.send_event(normalized_event)
            await self._send_metrics_update()

    async def _send_metrics_update(self) -> None:
        snapshot = self.metrics.snapshot()
        await self.send_event(
            {
                "type": "metrics.update",
                "metrics": snapshot["metrics"],
                "timestamps": snapshot["timestamps"],
            }
        )
