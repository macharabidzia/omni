from src.realtime.events import (
    AssistantAudioDeltaEvent,
    AssistantDoneEvent,
    AssistantTextDeltaEvent,
    ErrorEvent,
    RuntimeGatewayEvent,
    TranscriptDeltaEvent,
)
from src.realtime.qwen_client import QwenEvent


def normalize_qwen_event(event: QwenEvent) -> RuntimeGatewayEvent | None:
    if event.kind == "transcript_delta" and event.text:
        return TranscriptDeltaEvent(
            type="transcript.delta",
            text=event.text,
        )

    if event.kind == "assistant_text_delta" and event.text:
        return AssistantTextDeltaEvent(
            type="assistant.text.delta",
            text=event.text,
        )

    if event.kind == "assistant_audio_delta" and event.audio_base64:
        return AssistantAudioDeltaEvent(
            type="assistant.audio.delta",
            audio_base64=event.audio_base64,
            sample_rate=event.sample_rate,
            channels=event.channels,
            format=event.audio_format,
        )

    if event.kind == "response_done":
        return AssistantDoneEvent(type="assistant.done")

    if event.kind == "error":
        return ErrorEvent(
            type="error",
            code=event.code or "QWEN_ERROR",
            message=event.message or "Qwen returned an unspecified error.",
        )

    return None
