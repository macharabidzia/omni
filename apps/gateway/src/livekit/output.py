from __future__ import annotations

import base64
import logging
import time
from math import ceil
from typing import Awaitable, Callable

import numpy as np
from livekit import rtc

from src.realtime.audio import (
    chunk_bytes_for_duration_ms,
    pcm16_bytes as encode_pcm16_bytes,
    pcm16_peak_abs,
    pcm16_rms,
    pcm16_samples,
)

logger = logging.getLogger(__name__)

FrameSink = Callable[[bytes], Awaitable[None]]
LEADING_SILENCE_TRIM_MAX_MS = 120
INTERCHUNK_LEADING_SILENCE_TRIM_MAX_MS = 60
INTERCHUNK_TRAILING_SILENCE_TRIM_MIN_MS = 20
INTERCHUNK_TRAILING_SILENCE_TRIM_MAX_MS = 30
INTERCHUNK_TRAILING_SILENCE_RMS_THRESHOLD = 96
LEADING_SILENCE_WINDOW_MS = 5
LEADING_SILENCE_PEAK_THRESHOLD = 48
LEADING_FADE_IN_MS = 4
CHUNK_GAP_WARNING_MS = 160
QUEUE_STARVATION_WARNING_MS = 40
BOUNDARY_SMOOTH_MS = 3.0
BOUNDARY_JUMP_SMOOTH_ABS = 4_000
BOUNDARY_JUMP_WARNING_ABS = 8_000
ISOLATED_SPIKE_SMOOTH_ABS = 12_000


class AssistantAudioPublisher:
    def __init__(
        self,
        *,
        audio_source: rtc.AudioSource,
        output_sample_rate: int,
        output_frame_ms: int,
        frame_sink: FrameSink | None = None,
    ) -> None:
        self.audio_source = audio_source
        self.output_sample_rate = output_sample_rate
        self.output_frame_ms = output_frame_ms
        self.frame_sink = frame_sink
        self.output_frame_bytes = chunk_bytes_for_duration_ms(
            output_frame_ms,
            sample_rate=output_sample_rate,
        )
        self.pending_output = bytearray()
        self._resampler: rtc.AudioResampler | None = None
        self._resampler_input_rate: int | None = None
        self._leading_trim_window_samples = max(
            1,
            int(round(output_sample_rate * (LEADING_SILENCE_WINDOW_MS / 1000))),
        )
        self._interchunk_trim_max_samples = max(
            1,
            int(round(output_sample_rate * (INTERCHUNK_LEADING_SILENCE_TRIM_MAX_MS / 1000))),
        )
        self._interchunk_trailing_trim_min_samples = max(
            1,
            int(round(output_sample_rate * (INTERCHUNK_TRAILING_SILENCE_TRIM_MIN_MS / 1000))),
        )
        self._interchunk_trailing_trim_max_samples = max(
            1,
            int(round(output_sample_rate * (INTERCHUNK_TRAILING_SILENCE_TRIM_MAX_MS / 1000))),
        )
        self._leading_fade_in_samples = max(
            1,
            int(round(output_sample_rate * (LEADING_FADE_IN_MS / 1000))),
        )
        self._boundary_smooth_samples = max(
            1,
            int(round(output_sample_rate * (BOUNDARY_SMOOTH_MS / 1000))),
        )
        self._last_chunk_monotonic: float | None = None
        self._reset_turn_boundary_state()

    async def enqueue_base64(self, audio_base64: str, *, input_sample_rate: int) -> None:
        now = time.monotonic()
        if self._last_chunk_monotonic is not None:
            gap_ms = (now - self._last_chunk_monotonic) * 1000
            queued_ms = self.queued_duration_seconds() * 1000
            if gap_ms >= CHUNK_GAP_WARNING_MS and queued_ms <= QUEUE_STARVATION_WARNING_MS:
                logger.warning(
                    "Assistant audio input gap gap_ms=%.2f queued_ms=%.2f pending_bytes=%d turn_chunks=%d",
                    gap_ms,
                    queued_ms,
                    len(self.pending_output),
                    self._turn_chunk_count,
                )
        self._last_chunk_monotonic = now

        pcm16_bytes = base64.b64decode(audio_base64)
        pcm16_output = self._convert_input_rate(
            pcm16_bytes,
            input_sample_rate=input_sample_rate,
        )
        pcm16_output = self._normalize_chunk_silence(pcm16_output)
        if not pcm16_output:
            return
        pcm16_output = self._smooth_artifacts(pcm16_output)
        if not pcm16_output:
            return
        self._turn_chunk_count += 1
        self._turn_input_audio_ms += (len(pcm16_output) / 2 / self.output_sample_rate) * 1000
        self.pending_output.extend(pcm16_output)
        await self._flush_complete_frames()

    async def finalize_turn(self) -> None:
        tail = self._flush_resampler()
        if tail:
            tail = self._normalize_chunk_silence(tail)
            if tail:
                self.pending_output.extend(tail)

        if not self.pending_output:
            logger.info(
                "Assistant audio turn finalized with no publishable audio trimmed_leading_ms=%.2f interchunk_trimmed_ms=%.2f dropped_silent_chunks=%d",
                self._turn_trimmed_leading_ms,
                self._turn_interchunk_trimmed_ms,
                self._turn_dropped_silent_chunk_count,
            )
            self._reset_turn_boundary_state()
            return

        self._pad_tail_with_fadeout()
        await self._flush_complete_frames()
        logger.info(
            "Assistant audio turn finalized chunks=%d published_audio_ms=%.2f queued_ms=%.2f trimmed_leading_ms=%.2f interchunk_trimmed_ms=%.2f dropped_silent_chunks=%d raw_boundary_jump_max=%d raw_boundary_jump_p95=%d effective_boundary_jump_max=%d effective_boundary_jump_p95=%d suspicious_boundaries=%d smoothed_boundaries=%d smoothed_spikes=%d",
            self._turn_chunk_count,
            self._turn_input_audio_ms,
            self.queued_duration_seconds() * 1000,
            self._turn_trimmed_leading_ms,
            self._turn_interchunk_trimmed_ms,
            self._turn_dropped_silent_chunk_count,
            max(self._turn_raw_boundary_jump_values) if self._turn_raw_boundary_jump_values else 0,
            self._percentile(self._turn_raw_boundary_jump_values, 95),
            max(self._turn_effective_boundary_jump_values)
            if self._turn_effective_boundary_jump_values
            else 0,
            self._percentile(self._turn_effective_boundary_jump_values, 95),
            self._turn_suspicious_boundary_count,
            self._turn_smoothed_boundary_count,
            self._turn_smoothed_spike_count,
        )
        self._reset_turn_boundary_state()

    async def clear(self) -> None:
        if self._turn_chunk_count > 0 or self.pending_output:
            logger.info(
                "Assistant audio turn cleared chunks=%d queued_ms=%.2f pending_bytes=%d trimmed_leading_ms=%.2f interchunk_trimmed_ms=%.2f dropped_silent_chunks=%d",
                self._turn_chunk_count,
                self.queued_duration_seconds() * 1000,
                len(self.pending_output),
                self._turn_trimmed_leading_ms,
                self._turn_interchunk_trimmed_ms,
                self._turn_dropped_silent_chunk_count,
            )
        self.pending_output.clear()
        self._resampler = None
        self._resampler_input_rate = None
        self._reset_turn_boundary_state()
        self.audio_source.clear_queue()

    async def wait_for_playout(self) -> None:
        await self.audio_source.wait_for_playout()

    def queued_duration_seconds(self) -> float:
        return self.audio_source.queued_duration

    def _convert_input_rate(self, pcm16_bytes: bytes, *, input_sample_rate: int) -> bytes:
        if input_sample_rate == self.output_sample_rate:
            return pcm16_bytes

        if self._resampler is None:
            self._resampler_input_rate = input_sample_rate
            self._resampler = rtc.AudioResampler(
                input_rate=input_sample_rate,
                output_rate=self.output_sample_rate,
                num_channels=1,
                quality=rtc.AudioResamplerQuality.HIGH,
            )
        elif self._resampler_input_rate != input_sample_rate:
            raise ValueError(
                "Assistant audio input sample rate changed mid-turn: "
                f"{self._resampler_input_rate} -> {input_sample_rate}."
            )

        return self._frames_to_bytes(self._resampler.push(bytearray(pcm16_bytes)))

    def _flush_resampler(self) -> bytes:
        if self._resampler is None:
            return b""

        frames = self._resampler.flush()
        self._resampler = None
        self._resampler_input_rate = None
        return self._frames_to_bytes(frames)

    def _pad_tail_with_fadeout(self) -> None:
        remainder = len(self.pending_output) % self.output_frame_bytes
        if remainder == 0 or len(self.pending_output) < 2:
            return

        missing_bytes = self.output_frame_bytes - remainder
        missing_samples = missing_bytes // 2
        if missing_samples <= 0:
            return
        last_sample = int(pcm16_samples(self.pending_output[-2:])[0])
        scale = np.arange(missing_samples - 1, -1, -1, dtype=np.float32) / missing_samples
        self.pending_output.extend(encode_pcm16_bytes(last_sample * scale))

    @staticmethod
    def _frames_to_bytes(frames: list[rtc.AudioFrame]) -> bytes:
        if not frames:
            return b""
        return b"".join(bytes(frame.data) for frame in frames)

    def _normalize_chunk_silence(self, pcm16_bytes: bytes) -> bytes:
        if not pcm16_bytes:
            return b""

        if not self._turn_voiced_audio_started:
            pcm16_bytes = self._trim_turn_leading_silence(pcm16_bytes)
            if not pcm16_bytes:
                return b""
            if self._chunk_peak_abs(pcm16_bytes) > LEADING_SILENCE_PEAK_THRESHOLD:
                self._turn_voiced_audio_started = True
            return pcm16_bytes

        if self._chunk_peak_abs(pcm16_bytes) <= LEADING_SILENCE_PEAK_THRESHOLD:
            self._turn_dropped_silent_chunk_count += 1
            return b""

        sample_count = len(pcm16_bytes) // 2
        trim_samples = self._count_leading_silence_samples(
            pcm16_bytes,
            max_trim_samples=min(sample_count, self._interchunk_trim_max_samples),
        )
        if trim_samples <= 0:
            return self._trim_interchunk_trailing_silence(pcm16_bytes)

        self._turn_interchunk_trimmed_ms += (trim_samples / self.output_sample_rate) * 1000
        return self._trim_interchunk_trailing_silence(pcm16_bytes[trim_samples * 2 :])

    def _trim_turn_leading_silence(self, pcm16_bytes: bytes) -> bytes:
        if self._leading_trim_complete or not pcm16_bytes:
            return pcm16_bytes

        sample_count = len(pcm16_bytes) // 2
        if sample_count == 0:
            return pcm16_bytes

        trim_samples = self._count_leading_silence_samples(
            pcm16_bytes,
            max_trim_samples=min(sample_count, self._leading_trim_remaining_samples),
        )

        if trim_samples == 0:
            self._leading_trim_complete = True
            return pcm16_bytes

        self._leading_trim_remaining_samples -= trim_samples
        trimmed_ms = (trim_samples / self.output_sample_rate) * 1000
        self._turn_trimmed_leading_ms += trimmed_ms
        trimmed_bytes = pcm16_bytes[trim_samples * 2 :]
        if not trimmed_bytes:
            if self._leading_trim_remaining_samples <= 0:
                self._leading_trim_complete = True
            return b""

        self._leading_trim_complete = True
        return self._apply_fade_in(trimmed_bytes)

    def _apply_fade_in(self, pcm16_bytes: bytes) -> bytes:
        sample_count = len(pcm16_bytes) // 2
        fade_samples = min(sample_count, self._leading_fade_in_samples)
        if fade_samples <= 1:
            return pcm16_bytes

        samples = pcm16_samples(pcm16_bytes, copy=True)
        scale = np.arange(1, fade_samples + 1, dtype=np.float32) / fade_samples
        samples[:fade_samples] = np.rint(samples[:fade_samples].astype(np.float32) * scale)
        return encode_pcm16_bytes(samples)

    def _smooth_artifacts(self, pcm16_bytes: bytes) -> bytes:
        samples = pcm16_samples(pcm16_bytes, copy=True)
        if samples.size == 0:
            return pcm16_bytes

        self._smooth_chunk_boundary_inplace(samples)
        self._smooth_isolated_spikes_inplace(samples)
        self._last_output_sample = int(samples[-1])
        return encode_pcm16_bytes(samples)

    def _smooth_chunk_boundary_inplace(self, samples: np.ndarray) -> None:
        if self._last_output_sample is None or samples.size == 0:
            return

        raw_boundary_jump_abs = abs(int(samples[0]) - self._last_output_sample)
        self._turn_raw_boundary_jump_values.append(raw_boundary_jump_abs)
        if raw_boundary_jump_abs < BOUNDARY_JUMP_SMOOTH_ABS:
            self._turn_effective_boundary_jump_values.append(raw_boundary_jump_abs)
            if raw_boundary_jump_abs >= BOUNDARY_JUMP_WARNING_ABS:
                self._turn_suspicious_boundary_count += 1
                logger.warning(
                    "Assistant audio boundary jump effective_abs=%d raw_abs=%d turn_chunks=%d queued_ms=%.2f",
                    raw_boundary_jump_abs,
                    raw_boundary_jump_abs,
                    self._turn_chunk_count,
                    self.queued_duration_seconds() * 1000,
                )
            return

        fade_samples = min(len(samples), self._boundary_smooth_samples)
        if fade_samples <= 1:
            candidate_prefix = np.array(
                [int(round((int(samples[0]) + self._last_output_sample) / 2))],
                dtype=np.int16,
            )
        else:
            alpha = np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
            original = samples[:fade_samples].astype(np.float32)
            candidate_prefix = np.rint(
                ((1.0 - alpha) * self._last_output_sample) + (alpha * original)
            ).astype(np.int16)

        effective_boundary_jump_abs = self._boundary_transition_peak_abs(
            candidate_prefix,
            previous_sample=self._last_output_sample,
            transition_samples=fade_samples,
        )
        if effective_boundary_jump_abs >= raw_boundary_jump_abs:
            self._turn_effective_boundary_jump_values.append(raw_boundary_jump_abs)
            if raw_boundary_jump_abs >= BOUNDARY_JUMP_WARNING_ABS:
                self._turn_suspicious_boundary_count += 1
                logger.warning(
                    "Assistant audio boundary jump effective_abs=%d raw_abs=%d turn_chunks=%d queued_ms=%.2f",
                    raw_boundary_jump_abs,
                    raw_boundary_jump_abs,
                    self._turn_chunk_count,
                    self.queued_duration_seconds() * 1000,
                )
            return

        samples[:fade_samples] = candidate_prefix
        self._turn_effective_boundary_jump_values.append(effective_boundary_jump_abs)
        if effective_boundary_jump_abs >= BOUNDARY_JUMP_WARNING_ABS:
            self._turn_suspicious_boundary_count += 1
            logger.warning(
                "Assistant audio boundary jump effective_abs=%d raw_abs=%d turn_chunks=%d queued_ms=%.2f",
                effective_boundary_jump_abs,
                raw_boundary_jump_abs,
                self._turn_chunk_count,
                self.queued_duration_seconds() * 1000,
            )
        self._turn_smoothed_boundary_count += 1

    def _smooth_isolated_spikes_inplace(self, samples: np.ndarray) -> None:
        if len(samples) < 3:
            return

        previous_samples = samples[:-2].astype(np.int32)
        current_samples = samples[1:-1].astype(np.int32)
        next_samples = samples[2:].astype(np.int32)
        deviation = np.abs((2 * current_samples) - previous_samples - next_samples)
        mask = (
            (deviation >= ISOLATED_SPIKE_SMOOTH_ABS)
            & (np.abs(current_samples - previous_samples) >= (ISOLATED_SPIKE_SMOOTH_ABS // 2))
            & (np.abs(current_samples - next_samples) >= (ISOLATED_SPIKE_SMOOTH_ABS // 2))
        )
        smoothed = int(np.count_nonzero(mask))
        if smoothed > 0:
            middle_samples = samples[1:-1]
            middle_samples[mask] = np.rint((previous_samples[mask] + next_samples[mask]) / 2)

        if smoothed > 0:
            self._turn_smoothed_spike_count += smoothed
            logger.info(
                "Assistant audio spike smoothing applied smoothed_samples=%d turn_chunks=%d queued_ms=%.2f",
                smoothed,
                self._turn_chunk_count,
                self.queued_duration_seconds() * 1000,
            )

    def _reset_turn_boundary_state(self) -> None:
        self._leading_trim_complete = False
        self._leading_trim_remaining_samples = max(
            1,
            int(round(self.output_sample_rate * (LEADING_SILENCE_TRIM_MAX_MS / 1000))),
        )
        self._last_chunk_monotonic = None
        self._last_output_sample: int | None = None
        self._turn_voiced_audio_started = False
        self._turn_chunk_count = 0
        self._turn_input_audio_ms = 0.0
        self._turn_trimmed_leading_ms = 0.0
        self._turn_interchunk_trimmed_ms = 0.0
        self._turn_dropped_silent_chunk_count = 0
        self._turn_raw_boundary_jump_values: list[int] = []
        self._turn_effective_boundary_jump_values: list[int] = []
        self._turn_suspicious_boundary_count = 0
        self._turn_smoothed_boundary_count = 0
        self._turn_smoothed_spike_count = 0

    def _count_leading_silence_samples(self, pcm16_bytes: bytes, *, max_trim_samples: int) -> int:
        trim_samples = 0
        while trim_samples < max_trim_samples:
            window_samples = min(
                self._leading_trim_window_samples,
                max_trim_samples - trim_samples,
            )
            if window_samples <= 0:
                break
            peak_abs = self._peak_abs(
                pcm16_bytes,
                start_sample=trim_samples,
                sample_count=window_samples,
            )
            if peak_abs > LEADING_SILENCE_PEAK_THRESHOLD:
                break
            trim_samples += window_samples
        return trim_samples

    def _trim_interchunk_trailing_silence(self, pcm16_bytes: bytes) -> bytes:
        sample_count = len(pcm16_bytes) // 2
        if sample_count <= 0:
            return b""

        trim_samples = self._count_trailing_silence_samples(
            pcm16_bytes,
            max_trim_samples=min(sample_count, self._interchunk_trailing_trim_max_samples),
        )
        if trim_samples < self._interchunk_trailing_trim_min_samples:
            return pcm16_bytes
        if (
            self._rms(
                pcm16_bytes,
                start_sample=sample_count - trim_samples,
                sample_count=trim_samples,
            )
            > INTERCHUNK_TRAILING_SILENCE_RMS_THRESHOLD
        ):
            return pcm16_bytes

        keep_samples = sample_count - trim_samples
        if keep_samples <= 0:
            return pcm16_bytes

        self._turn_interchunk_trimmed_ms += (trim_samples / self.output_sample_rate) * 1000
        return pcm16_bytes[: keep_samples * 2]

    @staticmethod
    def _chunk_peak_abs(pcm16_bytes: bytes) -> int:
        return pcm16_peak_abs(pcm16_bytes)

    def _count_trailing_silence_samples(self, pcm16_bytes: bytes, *, max_trim_samples: int) -> int:
        sample_count = len(pcm16_bytes) // 2
        trim_samples = 0
        while trim_samples < max_trim_samples:
            window_samples = min(
                self._leading_trim_window_samples,
                max_trim_samples - trim_samples,
            )
            if window_samples <= 0:
                break
            start_sample = sample_count - trim_samples - window_samples
            peak_abs = self._peak_abs(
                pcm16_bytes,
                start_sample=start_sample,
                sample_count=window_samples,
            )
            if peak_abs > LEADING_SILENCE_PEAK_THRESHOLD:
                break
            trim_samples += window_samples
        return trim_samples

    @staticmethod
    def _rms(pcm16_bytes: bytes, *, start_sample: int, sample_count: int) -> float:
        return pcm16_rms(
            pcm16_bytes,
            start_sample=start_sample,
            sample_count=sample_count,
        )

    @staticmethod
    def _boundary_transition_peak_abs(
        samples: np.ndarray,
        *,
        previous_sample: int,
        transition_samples: int,
    ) -> int:
        sample_count = min(len(samples), max(1, transition_samples))
        if sample_count <= 0:
            return 0
        transition = samples[:sample_count].astype(np.int32)
        previous = np.array([previous_sample], dtype=np.int32)
        deltas = np.diff(np.concatenate((previous, transition)))
        if deltas.size == 0:
            return 0
        return int(np.max(np.abs(deltas)))

    @staticmethod
    def _peak_abs(pcm16_bytes: bytes, *, start_sample: int, sample_count: int) -> int:
        return pcm16_peak_abs(
            pcm16_bytes,
            start_sample=start_sample,
            sample_count=sample_count,
        )

    @staticmethod
    def _percentile(values: list[int], pct: int) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        index = min(len(ordered) - 1, ceil((len(ordered) * pct / 100)) - 1)
        return ordered[index]

    async def _flush_complete_frames(self) -> None:
        while len(self.pending_output) >= self.output_frame_bytes:
            frame_bytes = bytes(self.pending_output[: self.output_frame_bytes])
            del self.pending_output[: self.output_frame_bytes]
            if self.frame_sink is not None:
                await self.frame_sink(frame_bytes)
            audio_frame = rtc.AudioFrame(
                data=frame_bytes,
                sample_rate=self.output_sample_rate,
                num_channels=1,
                samples_per_channel=len(frame_bytes) // 2,
            )
            await self.audio_source.capture_frame(audio_frame)
