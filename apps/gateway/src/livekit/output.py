from __future__ import annotations

import base64
import logging
import time
from array import array
from math import ceil, pi
from typing import Awaitable, Callable

from livekit import rtc

from src.realtime.audio import chunk_bytes_for_duration_ms

logger = logging.getLogger(__name__)

FrameSink = Callable[[bytes], Awaitable[None]]
LEADING_SILENCE_TRIM_MAX_MS = 120
LEADING_SILENCE_WINDOW_MS = 5
LEADING_SILENCE_PEAK_THRESHOLD = 48
LEADING_FADE_IN_MS = 4
CHUNK_GAP_WARNING_MS = 160
QUEUE_STARVATION_WARNING_MS = 40
BOUNDARY_SMOOTH_MS = 1.5
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
        output_lowpass_hz: float,
        frame_sink: FrameSink | None = None,
    ) -> None:
        self.audio_source = audio_source
        self.output_sample_rate = output_sample_rate
        self.output_frame_ms = output_frame_ms
        self.output_lowpass_hz = max(0.0, output_lowpass_hz)
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
        self._leading_fade_in_samples = max(
            1,
            int(round(output_sample_rate * (LEADING_FADE_IN_MS / 1000))),
        )
        self._boundary_smooth_samples = max(
            1,
            int(round(output_sample_rate * (BOUNDARY_SMOOTH_MS / 1000))),
        )
        self._lowpass_alpha = self._compute_lowpass_alpha(
            cutoff_hz=self.output_lowpass_hz,
            sample_rate=output_sample_rate,
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
        pcm16_output = self._trim_turn_leading_silence(pcm16_output)
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
            tail = self._trim_turn_leading_silence(tail)
            self.pending_output.extend(tail)

        if not self.pending_output:
            logger.info(
                "Assistant audio turn finalized with no publishable audio trimmed_leading_ms=%.2f",
                self._turn_trimmed_leading_ms,
            )
            self._reset_turn_boundary_state()
            return

        self._pad_tail_with_fadeout()
        await self._flush_complete_frames()
        logger.info(
            "Assistant audio turn finalized chunks=%d published_audio_ms=%.2f queued_ms=%.2f trimmed_leading_ms=%.2f lowpass_hz=%.1f boundary_jump_max=%d boundary_jump_p95=%d suspicious_boundaries=%d smoothed_boundaries=%d smoothed_spikes=%d",
            self._turn_chunk_count,
            self._turn_input_audio_ms,
            self.queued_duration_seconds() * 1000,
            self._turn_trimmed_leading_ms,
            self.output_lowpass_hz,
            max(self._turn_boundary_jump_values) if self._turn_boundary_jump_values else 0,
            self._percentile(self._turn_boundary_jump_values, 95),
            self._turn_suspicious_boundary_count,
            self._turn_smoothed_boundary_count,
            self._turn_smoothed_spike_count,
        )
        self._reset_turn_boundary_state()

    async def clear(self) -> None:
        if self._turn_chunk_count > 0 or self.pending_output:
            logger.info(
                "Assistant audio turn cleared chunks=%d queued_ms=%.2f pending_bytes=%d trimmed_leading_ms=%.2f",
                self._turn_chunk_count,
                self.queued_duration_seconds() * 1000,
                len(self.pending_output),
                self._turn_trimmed_leading_ms,
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
        last_sample = int.from_bytes(self.pending_output[-2:], byteorder="little", signed=True)
        fadeout = array("h")
        for index in range(missing_samples):
            remaining = missing_samples - index - 1
            fadeout.append(int(round(last_sample * remaining / missing_samples)))
        self.pending_output.extend(fadeout.tobytes())

    @staticmethod
    def _frames_to_bytes(frames: list[rtc.AudioFrame]) -> bytes:
        if not frames:
            return b""
        return b"".join(bytes(frame.data) for frame in frames)

    def _trim_turn_leading_silence(self, pcm16_bytes: bytes) -> bytes:
        if self._leading_trim_complete or not pcm16_bytes:
            return pcm16_bytes

        sample_count = len(pcm16_bytes) // 2
        if sample_count == 0:
            return pcm16_bytes

        max_trim_samples = min(sample_count, self._leading_trim_remaining_samples)
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

        faded = array("h")
        faded.frombytes(pcm16_bytes)
        for index in range(fade_samples):
            faded[index] = int(round(faded[index] * ((index + 1) / fade_samples)))
        return faded.tobytes()

    def _smooth_artifacts(self, pcm16_bytes: bytes) -> bytes:
        samples = array("h")
        samples.frombytes(pcm16_bytes)
        if not samples:
            return pcm16_bytes

        self._smooth_chunk_boundary_inplace(samples)
        self._smooth_isolated_spikes_inplace(samples)
        self._apply_lowpass_inplace(samples)
        self._last_output_sample = samples[-1]
        return samples.tobytes()

    def _smooth_chunk_boundary_inplace(self, samples: array) -> None:
        if self._last_output_sample is None or not samples:
            return

        boundary_jump_abs = abs(samples[0] - self._last_output_sample)
        self._turn_boundary_jump_values.append(boundary_jump_abs)
        if boundary_jump_abs >= BOUNDARY_JUMP_WARNING_ABS:
            self._turn_suspicious_boundary_count += 1
            logger.warning(
                "Assistant audio boundary jump boundary_abs=%d turn_chunks=%d queued_ms=%.2f",
                boundary_jump_abs,
                self._turn_chunk_count,
                self.queued_duration_seconds() * 1000,
            )
        if boundary_jump_abs < BOUNDARY_JUMP_SMOOTH_ABS:
            return

        fade_samples = min(len(samples), self._boundary_smooth_samples)
        if fade_samples <= 1:
            samples[0] = int(round((samples[0] + self._last_output_sample) / 2))
        else:
            for index in range(fade_samples):
                alpha = index / (fade_samples - 1)
                samples[index] = int(
                    round((1 - alpha) * self._last_output_sample + (alpha * samples[index]))
                )
        self._turn_smoothed_boundary_count += 1

    def _smooth_isolated_spikes_inplace(self, samples: array) -> None:
        if len(samples) < 3:
            return

        smoothed = 0
        for index in range(1, len(samples) - 1):
            previous_sample = samples[index - 1]
            current_sample = samples[index]
            next_sample = samples[index + 1]
            deviation = abs((2 * current_sample) - previous_sample - next_sample)
            if deviation < ISOLATED_SPIKE_SMOOTH_ABS:
                continue
            if (
                abs(current_sample - previous_sample) < (ISOLATED_SPIKE_SMOOTH_ABS // 2)
                or abs(current_sample - next_sample) < (ISOLATED_SPIKE_SMOOTH_ABS // 2)
            ):
                continue
            samples[index] = int(round((previous_sample + next_sample) / 2))
            smoothed += 1

        if smoothed > 0:
            self._turn_smoothed_spike_count += smoothed
            logger.warning(
                "Assistant audio spike smoothing smoothed_samples=%d turn_chunks=%d queued_ms=%.2f",
                smoothed,
                self._turn_chunk_count,
                self.queued_duration_seconds() * 1000,
            )

    def _apply_lowpass_inplace(self, samples: array) -> None:
        if self._lowpass_alpha is None or not samples:
            return

        previous_output = self._lowpass_prev_output_sample
        if previous_output is None:
            previous_output = float(samples[0])
            self._lowpass_prev_output_sample = previous_output
            start_index = 1
        else:
            start_index = 0

        for index in range(start_index, len(samples)):
            previous_output = previous_output + (
                self._lowpass_alpha * (samples[index] - previous_output)
            )
            samples[index] = int(round(previous_output))

        self._lowpass_prev_output_sample = previous_output

    def _reset_turn_boundary_state(self) -> None:
        self._leading_trim_complete = False
        self._leading_trim_remaining_samples = max(
            1,
            int(round(self.output_sample_rate * (LEADING_SILENCE_TRIM_MAX_MS / 1000))),
        )
        self._last_chunk_monotonic = None
        self._last_output_sample: int | None = None
        self._lowpass_prev_output_sample: float | None = None
        self._turn_chunk_count = 0
        self._turn_input_audio_ms = 0.0
        self._turn_trimmed_leading_ms = 0.0
        self._turn_boundary_jump_values: list[int] = []
        self._turn_suspicious_boundary_count = 0
        self._turn_smoothed_boundary_count = 0
        self._turn_smoothed_spike_count = 0

    @staticmethod
    def _peak_abs(pcm16_bytes: bytes, *, start_sample: int, sample_count: int) -> int:
        peak_abs = 0
        start = start_sample * 2
        end = start + (sample_count * 2)
        for offset in range(start, end, 2):
            sample = int.from_bytes(pcm16_bytes[offset : offset + 2], byteorder="little", signed=True)
            peak_abs = max(peak_abs, abs(sample))
        return peak_abs

    @staticmethod
    def _percentile(values: list[int], pct: int) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        index = min(len(ordered) - 1, ceil((len(ordered) * pct / 100)) - 1)
        return ordered[index]

    @staticmethod
    def _compute_lowpass_alpha(*, cutoff_hz: float, sample_rate: int) -> float | None:
        if cutoff_hz <= 0 or sample_rate <= 0:
            return None
        dt = 1 / sample_rate
        rc = 1 / (2 * pi * cutoff_hz)
        return dt / (rc + dt)

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
