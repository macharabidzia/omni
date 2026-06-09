import base64
import io
import math
import wave
from dataclasses import dataclass
from typing import Iterator

import numpy as np

PCM16_DTYPE = np.dtype("<i2")


class AudioValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class ValidatedAudioChunk:
    audio_base64: str | None
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

    duration_ms, sample_count = _validate_audio_bytes(
        audio_bytes=audio_bytes,
        sample_rate=sample_rate,
        channels=channels,
        audio_format=audio_format,
        allowed_chunk_ms=allowed_chunk_ms,
    )

    return ValidatedAudioChunk(
        audio_base64=audio_base64,
        audio_bytes=audio_bytes,
        sample_rate=sample_rate,
        channels=channels,
        audio_format=audio_format,
        duration_ms=duration_ms,
        sample_count=sample_count,
    )


def validate_audio_bytes(
    *,
    audio_bytes: bytes | bytearray | memoryview,
    sample_rate: int,
    channels: int,
    audio_format: str,
    allowed_chunk_ms: tuple[int, ...],
) -> ValidatedAudioChunk:
    normalized_audio_bytes = (
        audio_bytes
        if isinstance(audio_bytes, bytes)
        else bytes(audio_bytes)
    )
    if not normalized_audio_bytes:
        raise AudioValidationError("EMPTY_AUDIO", "Audio chunk is empty.")

    duration_ms, sample_count = _validate_audio_bytes(
        audio_bytes=normalized_audio_bytes,
        sample_rate=sample_rate,
        channels=channels,
        audio_format=audio_format,
        allowed_chunk_ms=allowed_chunk_ms,
    )

    return ValidatedAudioChunk(
        audio_base64=None,
        audio_bytes=normalized_audio_bytes,
        sample_rate=sample_rate,
        channels=channels,
        audio_format=audio_format,
        duration_ms=duration_ms,
        sample_count=sample_count,
    )


def _validate_audio_bytes(
    *,
    audio_bytes: bytes,
    sample_rate: int,
    channels: int,
    audio_format: str,
    allowed_chunk_ms: tuple[int, ...],
) -> tuple[int, int]:
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
    return closest_ms, sample_count


def pcm16_duration_ms(byte_count: int, sample_rate: int) -> int:
    return int(round(((byte_count / 2) / sample_rate) * 1000))


def chunk_bytes_for_duration_ms(duration_ms: int, sample_rate: int = 16000) -> int:
    sample_count = math.ceil(sample_rate * (duration_ms / 1000))
    return sample_count * 2


def pcm16_samples(audio_pcm16: bytes | bytearray | memoryview, *, copy: bool = False) -> np.ndarray:
    if len(audio_pcm16) % 2 != 0:
        raise ValueError("PCM16 audio must contain 2-byte aligned samples.")
    samples = np.frombuffer(audio_pcm16, dtype=PCM16_DTYPE)
    if copy:
        return samples.astype(np.int16, copy=True)
    return samples


def pcm16_bytes(samples: np.ndarray) -> bytes:
    clipped = np.clip(np.rint(samples), -32768, 32767).astype(PCM16_DTYPE, copy=False)
    return clipped.tobytes()


def pcm16_peak_abs(
    audio_pcm16: bytes | bytearray | memoryview,
    *,
    start_sample: int = 0,
    sample_count: int | None = None,
) -> int:
    samples = pcm16_samples(audio_pcm16)
    start = max(0, start_sample)
    end = len(samples) if sample_count is None else min(len(samples), start + max(0, sample_count))
    if end <= start:
        return 0
    return pcm16_sample_peak_abs(samples[start:end])


def pcm16_sample_peak_abs(samples: np.ndarray) -> int:
    if samples.size == 0:
        return 0
    return int(np.max(np.abs(samples.astype(np.int32, copy=False))))


def pcm16_rms(
    audio_pcm16: bytes | bytearray | memoryview,
    *,
    start_sample: int = 0,
    sample_count: int | None = None,
) -> float:
    samples = pcm16_samples(audio_pcm16)
    start = max(0, start_sample)
    end = len(samples) if sample_count is None else min(len(samples), start + max(0, sample_count))
    if end <= start:
        return 0.0
    window = samples[start:end].astype(np.float32, copy=False)
    return float(np.sqrt(np.mean(window * window)))


def pcm16_to_wav(audio_pcm16: bytes, *, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_pcm16)
    return buffer.getvalue()


def wav_to_pcm16(wav_bytes: bytes) -> tuple[bytes, int]:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            if wav_file.getnchannels() != 1:
                raise ValueError("Only mono WAV audio is supported.")
            if wav_file.getsampwidth() != 2:
                raise ValueError("Only 16-bit PCM WAV audio is supported.")
            sample_rate = wav_file.getframerate()
            pcm16_bytes = wav_file.readframes(wav_file.getnframes())
    except wave.Error as exc:
        raise ValueError(f"Invalid WAV audio: {exc}") from exc

    return pcm16_bytes, sample_rate


def iter_pcm16_chunks(
    audio_bytes: bytes,
    *,
    duration_ms: int,
    sample_rate: int = 16000,
    pad_final_chunk: bool = False,
) -> Iterator[bytes]:
    if len(audio_bytes) % 2 != 0:
        raise ValueError("PCM16 audio must contain 2-byte aligned samples.")

    chunk_size = chunk_bytes_for_duration_ms(duration_ms, sample_rate=sample_rate)
    for start in range(0, len(audio_bytes), chunk_size):
        chunk = audio_bytes[start : start + chunk_size]
        if len(chunk) == chunk_size or not pad_final_chunk:
            yield chunk
            continue
        yield chunk + (b"\x00" * (chunk_size - len(chunk)))
