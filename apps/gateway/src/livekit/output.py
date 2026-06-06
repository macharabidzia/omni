from __future__ import annotations

import base64
from array import array
from typing import Awaitable, Callable

from livekit import rtc

from src.realtime.audio import chunk_bytes_for_duration_ms

FrameSink = Callable[[bytes], Awaitable[None]]
LEADING_SILENCE_TRIM_MAX_MS = 120
LEADING_SILENCE_WINDOW_MS = 5
LEADING_SILENCE_PEAK_THRESHOLD = 48
LEADING_FADE_IN_MS = 4


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
        self._leading_fade_in_samples = max(
            1,
            int(round(output_sample_rate * (LEADING_FADE_IN_MS / 1000))),
        )
        self._reset_turn_boundary_state()

    async def enqueue_base64(self, audio_base64: str, *, input_sample_rate: int) -> None:
        pcm16_bytes = base64.b64decode(audio_base64)
        pcm16_output = self._convert_input_rate(
            pcm16_bytes,
            input_sample_rate=input_sample_rate,
        )
        pcm16_output = self._trim_turn_leading_silence(pcm16_output)
        if not pcm16_output:
            return
        self.pending_output.extend(pcm16_output)
        await self._flush_complete_frames()

    async def finalize_turn(self) -> None:
        tail = self._flush_resampler()
        if tail:
            tail = self._trim_turn_leading_silence(tail)
            self.pending_output.extend(tail)

        if not self.pending_output:
            self._reset_turn_boundary_state()
            return

        self._pad_tail_with_fadeout()
        await self._flush_complete_frames()
        self._reset_turn_boundary_state()

    async def clear(self) -> None:
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

    def _reset_turn_boundary_state(self) -> None:
        self._leading_trim_complete = False
        self._leading_trim_remaining_samples = max(
            1,
            int(round(self.output_sample_rate * (LEADING_SILENCE_TRIM_MAX_MS / 1000))),
        )

    @staticmethod
    def _peak_abs(pcm16_bytes: bytes, *, start_sample: int, sample_count: int) -> int:
        peak_abs = 0
        start = start_sample * 2
        end = start + (sample_count * 2)
        for offset in range(start, end, 2):
            sample = int.from_bytes(pcm16_bytes[offset : offset + 2], byteorder="little", signed=True)
            peak_abs = max(peak_abs, abs(sample))
        return peak_abs

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
