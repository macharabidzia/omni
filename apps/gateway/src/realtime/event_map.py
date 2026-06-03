from src.realtime.qwen_client import QwenEvent


def normalize_qwen_event(event: QwenEvent) -> dict | None:
    if event.kind == "transcript_delta" and event.text:
        return {
            "type": "transcript.delta",
            "text": event.text,
        }

    if event.kind == "assistant_text_delta" and event.text:
        return {
            "type": "assistant.text.delta",
            "text": event.text,
        }

    if event.kind == "assistant_audio_delta" and event.audio_base64:
        return {
            "type": "assistant.audio.delta",
            "audio_base64": event.audio_base64,
            "sample_rate": event.sample_rate,
            "channels": event.channels,
            "format": event.audio_format,
        }

    if event.kind == "response_done":
        return {"type": "assistant.done"}

    if event.kind == "error":
        return {
            "type": "error",
            "code": event.code or "QWEN_ERROR",
            "message": event.message or "Qwen returned an unspecified error.",
        }

    return None

