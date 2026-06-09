import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Literal

import websockets

from src.security import redact_url_secrets

logger = logging.getLogger(__name__)
_INITIAL_SERVER_EVENT_TIMEOUT_SECONDS = 5.0
_QWEN_RETRYABLE_ERROR_FRAGMENTS = (
    "1012",
    "service restart",
    "connection closed",
    "connect call failed",
    "connection refused",
    "errno 111",
    "timed out during opening handshake",
    "timed out waiting for qwen realtime session.created",
    "qwen realtime websocket closed during startup",
    "http 503",
    "service unavailable",
)


@dataclass(slots=True)
class QwenEvent:
    kind: Literal[
        "transcript_delta",
        "assistant_text_delta",
        "assistant_audio_delta",
        "response_done",
        "error",
    ]
    payload: dict
    text: str | None = None
    audio_base64: str | None = None
    sample_rate: int | None = None
    channels: int = 1
    audio_format: str = "pcm16"
    code: str | None = None
    message: str | None = None


class QwenRealtimeClient:
    def __init__(
        self,
        *,
        model: str,
        url: str,
        request_timeout_seconds: float,
        response_timeout_seconds: float,
        output_sample_rate: int,
        max_ws_message_bytes: int,
        debug_raw_events: bool = False,
        instructions: str | None = None,
    ) -> None:
        self.model = model
        self.url = url
        self.request_timeout_seconds = request_timeout_seconds
        self.response_timeout_seconds = response_timeout_seconds
        self.output_sample_rate = output_sample_rate
        self.max_ws_message_bytes = max_ws_message_bytes
        self.debug_raw_events = debug_raw_events
        self.instructions = instructions
        self.websocket: websockets.WebSocketClientProtocol | None = None
        self.modalities: list[str] = ["text", "audio"]
        self.speaker = "Ethan"
        self.output_audio = True
        self.input_stream_started = False
        self.awaiting_response = False

    async def connect(self) -> None:
        if self.websocket is not None:
            return
        logger.info("Connecting to Qwen realtime websocket url=%s", redact_url_secrets(self.url))
        websocket = await websockets.connect(
            self.url,
            max_size=self.max_ws_message_bytes,
            open_timeout=self.request_timeout_seconds,
            close_timeout=2,
        )
        try:
            await _expect_session_created(
                websocket,
                timeout_seconds=min(
                    self.request_timeout_seconds,
                    _INITIAL_SERVER_EVENT_TIMEOUT_SECONDS,
                ),
                debug_raw_events=self.debug_raw_events,
            )
        except Exception:
            await websocket.close()
            raise
        self.websocket = websocket
        logger.info("Connected to Qwen realtime websocket url=%s", redact_url_secrets(self.url))

    async def start_session(
        self,
        *,
        speaker: str,
        modalities: list[str],
        input_sample_rate: int,
        output_audio: bool,
    ) -> None:
        self.modalities = modalities
        self.speaker = speaker
        self.output_audio = output_audio
        self.input_stream_started = False
        self.awaiting_response = False
        logger.info(
            "Starting Qwen realtime session speaker=%s modalities=%s output_audio=%s input_sample_rate=%s",
            speaker,
            ",".join(modalities),
            output_audio,
            input_sample_rate,
        )

        session_update = {
            "type": "session.update",
            "model": self.model,
        }
        await self._send(session_update)

    async def _ensure_input_stream_started(self) -> None:
        if self.input_stream_started:
            return
        await self._send({"type": "input_audio_buffer.commit", "final": False})
        self.input_stream_started = True
        self.awaiting_response = True

    async def append_audio(self, pcm16_base64: str) -> None:
        await self._ensure_input_stream_started()
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": pcm16_base64,
            }
        )

    async def commit_audio(self) -> None:
        await self._send({"type": "input_audio_buffer.commit", "final": True})

    async def cancel_response(self) -> None:
        if self.websocket is None:
            return
        logger.warning(
            "Qwen realtime websocket cancel requested without native upstream cancel support; closing the socket."
        )
        self.input_stream_started = False
        self.awaiting_response = False
        await self.websocket.close()
        self.websocket = None

    async def close(self) -> None:
        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None

    async def iter_events(self):
        if self.websocket is None:
            raise RuntimeError("Qwen websocket has not been connected.")

        while True:
            try:
                raw_message = await asyncio.wait_for(
                    self.websocket.recv(),
                    timeout=self.response_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                yield QwenEvent(
                    kind="error",
                    payload={},
                    code="QWEN_TIMEOUT",
                    message=f"Timed out waiting for Qwen response: {exc}",
                )
                return
            except websockets.ConnectionClosed as exc:
                logger.warning(
                    "Qwen realtime websocket closed code=%s reason=%s awaiting_response=%s input_stream_started=%s",
                    exc.code,
                    exc.reason or "",
                    self.awaiting_response,
                    self.input_stream_started,
                )
                if self.awaiting_response:
                    self.input_stream_started = False
                    self.awaiting_response = False
                    yield QwenEvent(
                        kind="error",
                        payload={},
                        code="QWEN_CONNECTION_CLOSED",
                        message=(
                            "Qwen realtime websocket closed before a terminal response event: "
                            f"{exc}"
                        ),
                    )
                return

            if isinstance(raw_message, bytes):
                logger.debug("Ignoring unexpected binary message from Qwen (%d bytes)", len(raw_message))
                continue

            payload = json.loads(raw_message)
            if self.debug_raw_events:
                logger.debug("Raw Qwen event: %s", payload)

            event = self._parse_event(payload)
            if event is not None:
                yield event

    async def _send(self, payload: dict) -> None:
        if self.websocket is None:
            raise RuntimeError("Qwen websocket has not been connected.")
        try:
            await asyncio.wait_for(
                self.websocket.send(json.dumps(payload)),
                timeout=self.request_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Qwen websocket send timed out type=%s timeout_seconds=%.2f",
                payload.get("type", "unknown"),
                self.request_timeout_seconds,
            )
            raise
        except websockets.ConnectionClosed as exc:
            logger.warning(
                "Qwen websocket send failed type=%s code=%s reason=%s",
                payload.get("type", "unknown"),
                exc.code,
                exc.reason or "",
            )
            raise

    def _parse_event(self, payload: dict) -> QwenEvent | None:
        raw_type = payload.get("type", "")

        if raw_type in {
            "conversation.item.input_audio_transcription.delta",
            "input_audio_buffer.transcript.delta",
        }:
            text = _first_non_empty(
                payload.get("delta"),
                payload.get("text"),
                payload.get("transcript"),
            )
            return QwenEvent(kind="transcript_delta", payload=payload, text=text)

        if raw_type in {
            "response.audio_transcript.delta",
            "transcription.delta",
        }:
            text = _first_non_empty(
                payload.get("delta"),
                payload.get("text"),
                payload.get("transcript"),
            )
            return QwenEvent(kind="assistant_text_delta", payload=payload, text=text)

        if raw_type in {"response.text.delta", "response.output_text.delta"}:
            text = _first_non_empty(payload.get("delta"), payload.get("text"))
            return QwenEvent(kind="assistant_text_delta", payload=payload, text=text)

        if raw_type in {"response.audio.delta", "response.output_audio.delta"}:
            audio_base64 = _extract_audio_base64(payload)
            sample_rate = _extract_audio_sample_rate(payload) or self.output_sample_rate
            return QwenEvent(
                kind="assistant_audio_delta",
                payload=payload,
                audio_base64=audio_base64,
                sample_rate=sample_rate,
            )

        if raw_type in {
            "response.done",
            "response.completed",
            "transcription.done",
        }:
            if not self.awaiting_response:
                return None
            self.input_stream_started = False
            self.awaiting_response = False
            return QwenEvent(kind="response_done", payload=payload)

        if raw_type == "response.audio.done":
            return None

        if raw_type == "error":
            self.input_stream_started = False
            self.awaiting_response = False
            return QwenEvent(
                kind="error",
                payload=payload,
                code=_extract_error_code(payload),
                message=_extract_error_message(payload),
            )

        return None


def _first_non_empty(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _extract_audio_base64(payload: dict) -> str | None:
    for key in ("delta", "audio", "chunk"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            nested = value.get("data")
            if isinstance(nested, str) and nested:
                return nested
    return None


def _extract_audio_sample_rate(payload: dict) -> int | None:
    value = payload.get("sample_rate")
    if isinstance(value, int):
        return value
    value = payload.get("sample_rate_hz")
    if isinstance(value, int):
        return value
    audio = payload.get("audio")
    if isinstance(audio, dict):
        nested = audio.get("sample_rate")
        if isinstance(nested, int):
            return nested
        fmt = audio.get("format")
        if isinstance(fmt, dict):
            nested = fmt.get("sample_rate_hz")
            if isinstance(nested, int):
                return nested
    return None


def _extract_error_code(payload: dict) -> str:
    return (
        _first_non_empty(
            payload.get("code"),
            payload.get("error", {}).get("code") if isinstance(payload.get("error"), dict) else None,
        )
        or "QWEN_ERROR"
    )


def _extract_error_message(payload: dict) -> str:
    nested_error = payload.get("error")
    return (
        _first_non_empty(
            payload.get("message"),
            nested_error if isinstance(nested_error, str) else None,
            nested_error.get("message") if isinstance(nested_error, dict) else None,
        )
        or "Qwen returned an unspecified error."
    )


def is_qwen_connection_retryable_error(exc: Exception) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, websockets.ConnectionClosed):
            return True
        if isinstance(current, asyncio.TimeoutError):
            return True
        if isinstance(current, OSError) and getattr(current, "errno", None) in {
            104,
            110,
            111,
            113,
        }:
            return True
        message = str(current).lower()
        if any(fragment in message for fragment in _QWEN_RETRYABLE_ERROR_FRAGMENTS):
            return True
        current = current.__cause__ or current.__context__
    return False


def is_qwen_connection_closed_error(exc: Exception) -> bool:
    return is_qwen_connection_retryable_error(exc)


async def probe_qwen_realtime_websocket(
    *,
    url: str,
    request_timeout_seconds: float,
    max_ws_message_bytes: int,
    debug_raw_events: bool = False,
) -> None:
    websocket = await websockets.connect(
        url,
        max_size=max_ws_message_bytes,
        open_timeout=request_timeout_seconds,
        close_timeout=2,
    )
    try:
        await _expect_session_created(
            websocket,
            timeout_seconds=min(
                request_timeout_seconds,
                _INITIAL_SERVER_EVENT_TIMEOUT_SECONDS,
            ),
            debug_raw_events=debug_raw_events,
        )
    finally:
        await websocket.close()


async def _expect_session_created(
    websocket,
    *,
    timeout_seconds: float,
    debug_raw_events: bool,
) -> None:
    try:
        raw_message = await asyncio.wait_for(
            websocket.recv(),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            "Timed out waiting for Qwen realtime session.created event."
        ) from exc
    except websockets.ConnectionClosed as exc:
        raise RuntimeError(
            f"Qwen realtime websocket closed during startup: {exc}"
        ) from exc

    if isinstance(raw_message, bytes):
        raise RuntimeError(
            "Qwen realtime websocket sent unexpected binary startup data."
        )

    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Qwen realtime websocket sent invalid JSON during startup."
        ) from exc

    if debug_raw_events:
        logger.debug("Raw Qwen startup event: %s", payload)

    event_type = payload.get("type")
    if event_type == "error":
        raise RuntimeError(
            f"{_extract_error_code(payload)}: {_extract_error_message(payload)}"
        )
    if event_type != "session.created":
        raise RuntimeError(
            f"Unexpected Qwen realtime startup event: {event_type or 'unknown'}"
        )
