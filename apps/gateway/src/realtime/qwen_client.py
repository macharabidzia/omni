import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Literal

import websockets


logger = logging.getLogger(__name__)


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
        url: str,
        request_timeout_seconds: float,
        response_timeout_seconds: float,
        output_sample_rate: int,
        max_ws_message_bytes: int,
        debug_raw_events: bool = False,
        instructions: str | None = None,
    ) -> None:
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

    async def connect(self) -> None:
        if self.websocket is not None:
            return
        self.websocket = await websockets.connect(
            self.url,
            max_size=self.max_ws_message_bytes,
            open_timeout=self.request_timeout_seconds,
            close_timeout=2,
        )

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

        session_update = {
            "type": "session.update",
            "session": {
                "modalities": modalities,
                "voice": speaker,
                "speaker": speaker,
                "instructions": self.instructions,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "audio": {
                    "input": {
                        "format": {
                            "type": "pcm16",
                            "sample_rate_hz": input_sample_rate,
                            "channels": 1,
                        }
                    },
                    "output": {
                        "format": {
                            "type": "pcm16",
                            "sample_rate_hz": self.output_sample_rate,
                            "channels": 1,
                        }
                    },
                },
            },
        }
        await self._send(session_update)

    async def append_audio(self, pcm16_base64: str) -> None:
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": pcm16_base64,
            }
        )

    async def commit_audio(self) -> None:
        response_payload = {
            "modalities": self.modalities,
            "instructions": self.instructions,
        }
        if self.output_audio:
            response_payload["audio"] = {
                "voice": self.speaker,
                "format": "pcm16",
            }

        await self._send({"type": "input_audio_buffer.commit"})
        await self._send(
            {
                "type": "response.create",
                "response": response_payload,
            }
        )

    async def cancel_response(self) -> None:
        await self._send({"type": "response.cancel"})

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
            except websockets.ConnectionClosed:
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
        await self.websocket.send(json.dumps(payload))

    def _parse_event(self, payload: dict) -> QwenEvent | None:
        raw_type = payload.get("type", "")

        if raw_type in {
            "conversation.item.input_audio_transcription.delta",
            "input_audio_buffer.transcript.delta",
            "response.audio_transcript.delta",
        }:
            text = _first_non_empty(
                payload.get("delta"),
                payload.get("text"),
                payload.get("transcript"),
            )
            return QwenEvent(kind="transcript_delta", payload=payload, text=text)

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

        if raw_type in {"response.done", "response.completed"}:
            return QwenEvent(kind="response_done", payload=payload)

        if raw_type == "error":
            return QwenEvent(
                kind="error",
                payload=payload,
                code=_first_non_empty(
                    payload.get("code"),
                    payload.get("error", {}).get("code") if isinstance(payload.get("error"), dict) else None,
                    "QWEN_ERROR",
                ),
                message=_first_non_empty(
                    payload.get("message"),
                    payload.get("error", {}).get("message") if isinstance(payload.get("error"), dict) else None,
                    "Qwen returned an unspecified error.",
                ),
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
