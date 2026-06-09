from __future__ import annotations

from typing import Literal, TypeAlias, TypedDict


PublicBackend = Literal[
    "qwen_realtime_audio",
    "qwen_chat_stream_audio",
    "text_only",
]


class GatewayEventBase(TypedDict, total=False):
    session_id: str
    participant_id: str
    participant_identity: str
    room: str
    turn_id: str
    input_epoch: int
    interrupt_epoch: int
    output_version: int
    backend: PublicBackend
    reason: str
    rollups: dict[str, float | int | None]


class SessionReadyEvent(GatewayEventBase, total=False):
    type: Literal["session.ready"]
    degraded_reason: Literal["non_realtime_audio_backend"] | None


class TurnStartedEvent(GatewayEventBase, total=False):
    type: Literal["turn.started"]
    speech_duration_ms: float | None


class TurnCommittedEvent(GatewayEventBase, total=False):
    type: Literal["turn.committed"]
    speech_duration_ms: float | None
    silence_duration_ms: float | None
    input_audio_ms: float | None


class TranscriptDeltaEvent(GatewayEventBase):
    type: Literal["transcript.delta"]
    text: str


class AssistantTextDeltaEvent(GatewayEventBase):
    type: Literal["assistant.text.delta"]
    text: str


class AssistantAudioStartedEvent(GatewayEventBase, total=False):
    type: Literal["assistant.audio_started"]
    sample_rate: int | None
    channels: int | None
    format: str | None


class AssistantAudioMetadataEvent(GatewayEventBase, total=False):
    type: Literal["assistant.audio.metadata"]
    sample_rate: int | None
    channels: int | None
    format: str | None
    audio_base64_length: int | None


class AssistantDoneEvent(GatewayEventBase):
    type: Literal["assistant.done"]


class AssistantInterruptedEvent(GatewayEventBase):
    type: Literal["assistant.interrupted"]


class MetricsUpdateEvent(GatewayEventBase, total=False):
    type: Literal["metrics.update"]
    metrics: dict[str, float | int | None]
    timestamps: dict[str, float | int | None]
    reconnects: dict[str, int]
    last_reconnect_reason: str | None


class ErrorEvent(GatewayEventBase):
    type: Literal["error"]
    code: str
    message: str


class RoomBusyEvent(GatewayEventBase):
    type: Literal["room.busy"]
    code: Literal["ROOM_BUSY"]
    message: str


class AssistantAudioDeltaEvent(GatewayEventBase, total=False):
    type: Literal["assistant.audio.delta"]
    audio_base64: str
    sample_rate: int | None
    channels: int | None
    format: str | None


class InputSpeechTruncatedEvent(GatewayEventBase, total=False):
    type: Literal["input.speech.truncated"]
    max_turn_ms: int
    input_audio_ms: float | None
    reason: str


PublicGatewayEvent: TypeAlias = (
    SessionReadyEvent
    | TurnStartedEvent
    | TurnCommittedEvent
    | TranscriptDeltaEvent
    | AssistantTextDeltaEvent
    | AssistantAudioStartedEvent
    | AssistantAudioMetadataEvent
    | AssistantDoneEvent
    | AssistantInterruptedEvent
    | MetricsUpdateEvent
    | ErrorEvent
    | RoomBusyEvent
    | InputSpeechTruncatedEvent
)

RuntimeGatewayEvent: TypeAlias = PublicGatewayEvent | AssistantAudioDeltaEvent
