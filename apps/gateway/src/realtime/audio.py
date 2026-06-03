import base64
import math
from dataclasses import dataclass


class AudioValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class ValidatedAudioChunk:
    audio_base64: str
    audio_bytes: bytes
    sample_rate: int
    channels: int
    audio_format: str
    duration_ms: int
    sample_count: int


def validate_audio_chunk(
    *,
    audio_base64: str,
    sample_rate: int,
    channels: int,
    audio_format: str,
    allowed_chunk_ms: tuple[int, ...],
) -> ValidatedAudioChunk:
    if audio_format != "pcm16":
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_FORMAT",
            "Only pcm16 input audio is supported.",
        )

    if channels != 1:
        raise AudioValidationError(
            "UNSUPPORTED_AUDIO_CHANNELS",
            "Only mono input audio is supported.",
        )

    if sample_rate != 16000:
        raise AudioValidationError(
            "UNSUPPORTED_SAMPLE_RATE",
            "Input audio must be 16 kHz PCM16 mono.",
        )

    if not audio_base64:
        raise AudioValidationError("EMPTY_AUDIO", "Audio chunk is empty.")

    try:
        audio_bytes = base64.b64decode(audio_base64, validate=True)
    except Exception as exc:
        raise AudioValidationError(
            "INVALID_BASE64_AUDIO",
            f"Audio chunk could not be base64-decoded: {exc}",
        ) from exc

    if not audio_bytes:
        raise AudioValidationError("EMPTY_AUDIO", "Audio chunk is empty.")

    if len(audio_bytes) % 2 != 0:
        raise AudioValidationError(
            "INVALID_PCM16_FRAME_SIZE",
            "PCM16 audio must contain 2-byte aligned samples.",
        )

    sample_count = len(audio_bytes) // 2
    duration_ms = int(round((sample_count / sample_rate) * 1000))
    closest_ms = min(allowed_chunk_ms, key=lambda value: abs(value - duration_ms))

    if abs(closest_ms - duration_ms) > 1:
        allowed = ", ".join(str(value) for value in allowed_chunk_ms)
        raise AudioValidationError(
            "INVALID_CHUNK_DURATION",
            f"Chunk duration {duration_ms} ms is not allowed. Use one of: {allowed}.",
        )

    return ValidatedAudioChunk(
        audio_base64=audio_base64,
        audio_bytes=audio_bytes,
        sample_rate=sample_rate,
        channels=channels,
        audio_format=audio_format,
        duration_ms=closest_ms,
        sample_count=sample_count,
    )


def pcm16_duration_ms(byte_count: int, sample_rate: int) -> int:
    return int(round(((byte_count / 2) / sample_rate) * 1000))


def chunk_bytes_for_duration_ms(duration_ms: int, sample_rate: int = 16000) -> int:
    sample_count = math.ceil(sample_rate * (duration_ms / 1000))
    return sample_count * 2

