#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import sys
import time
import wave
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.gateway.src.config import Settings
from apps.gateway.src.livekit.output import AssistantAudioPublisher
from apps.gateway.src.realtime.audio import iter_pcm16_chunks
from apps.gateway.src.realtime.qwen_client import QwenEvent
from apps.gateway.src.realtime.session import RealtimeSession
from apps.gateway.src.livekit import worker as worker_module


@dataclass(slots=True)
class TurnBenchmarkResult:
    run: int
    turn_id: str | None
    metrics: dict[str, float | int | None]
    timestamps: dict[str, float | int | None]


class FakeLocalParticipant:
    def __init__(self) -> None:
        self.published_payloads: list[dict] = []

    async def publish_data(
        self,
        payload: str,
        *,
        reliable: bool,
        destination_identities: list[str],
        topic: str,
    ) -> None:
        del reliable, destination_identities, topic
        self.published_payloads.append(json.loads(payload))


class FakeRoom:
    def __init__(self) -> None:
        self.local_participant = FakeLocalParticipant()


class BenchmarkAudioSource:
    def __init__(self, *, sample_rate: int, queue_size_ms: int) -> None:
        self.sample_rate = sample_rate
        self.num_channels = 1
        self.queue_size_ms = queue_size_ms
        self._queued_duration = 0.0
        self.frames = []

    async def capture_frame(self, frame) -> None:
        self.frames.append(frame)

    def clear_queue(self) -> None:
        self.frames.clear()

    async def wait_for_playout(self) -> None:
        return None

    @property
    def queued_duration(self) -> float:
        return self._queued_duration


class StubQwenRealtimeClient:
    def __init__(
        self,
        *,
        first_audio_delay_ms: float,
        done_delay_ms: float,
        output_sample_rate: int,
    ) -> None:
        self.first_audio_delay_ms = first_audio_delay_ms
        self.done_delay_ms = done_delay_ms
        self.output_sample_rate = output_sample_rate
        self.events: asyncio.Queue[QwenEvent | None] = asyncio.Queue()
        self.closed = False

    async def connect(self) -> None:
        return None

    async def start_session(
        self,
        *,
        speaker: str,
        modalities: list[str],
        input_sample_rate: int,
        output_audio: bool,
    ) -> None:
        del speaker, modalities, input_sample_rate, output_audio
        return None

    async def append_audio(self, pcm16_base64: str) -> None:
        del pcm16_base64
        return None

    async def commit_audio(self) -> None:
        async def emit_response() -> None:
            await asyncio.sleep(self.first_audio_delay_ms / 1000)
            if self.closed:
                return
            await self.events.put(
                QwenEvent(
                    kind="assistant_audio_delta",
                    payload={"type": "response.audio.delta"},
                    audio_base64=_tone_base64(
                        sample_rate=self.output_sample_rate,
                        duration_ms=40,
                    ),
                    sample_rate=self.output_sample_rate,
                )
            )
            await asyncio.sleep(self.done_delay_ms / 1000)
            if self.closed:
                return
            await self.events.put(
                QwenEvent(
                    kind="response_done",
                    payload={"type": "response.done"},
                )
            )

        asyncio.create_task(emit_response())

    async def cancel_response(self) -> None:
        self.closed = True
        await self.events.put(None)

    async def close(self) -> None:
        self.closed = True
        await self.events.put(None)

    async def iter_events(self):
        while True:
            event = await self.events.get()
            if event is None:
                return
            yield event


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark the in-process gateway turn path using the real worker/session/publisher "
            "components against a stub realtime backend. This does not require a hosted model."
        )
    )
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--warmup-runs", type=int, default=2)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--input-frame-ms", type=int, default=20)
    parser.add_argument("--stub-first-audio-delay-ms", type=float, default=45.0)
    parser.add_argument("--stub-done-delay-ms", type=float, default=25.0)
    parser.add_argument("--require-p95-ms", type=float, default=500.0)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


async def main_async() -> int:
    args = parse_args()
    if args.runs < 1:
        raise SystemExit("--runs must be >= 1")
    if args.warmup_runs < 0:
        raise SystemExit("--warmup-runs must be >= 0")

    output_json = (
        args.output_json
        if args.output_json is None or args.output_json.is_absolute()
        else REPO_ROOT / args.output_json
    )
    input_pcm16 = _load_input_pcm16(args.input)

    settings = Settings(
        LIVEKIT_INPUT_VAD_ENABLED=False,
        LIVEKIT_OUTPUT_FRAME_MS=10,
        LIVEKIT_OUTPUT_QUEUE_MS=150,
        LIVEKIT_QWEN_INPUT_CHUNK_MS=80,
        QWEN_AUDIO_BACKEND="realtime",
        QWEN_AUDIO_INPUT_SAMPLE_RATE=16000,
        QWEN_AUDIO_OUTPUT_SAMPLE_RATE=24000,
        LIVEKIT_INPUT_SAMPLE_RATE=48000,
        LIVEKIT_OUTPUT_SAMPLE_RATE=48000,
    )

    original_realtime_session = worker_module.RealtimeSession

    class BenchmarkRealtimeSession(RealtimeSession):
        def _build_qwen_realtime_client(self):
            return StubQwenRealtimeClient(
                first_audio_delay_ms=args.stub_first_audio_delay_ms,
                done_delay_ms=args.stub_done_delay_ms,
                output_sample_rate=self.settings.qwen_output_sample_rate,
            )

    worker_module.RealtimeSession = BenchmarkRealtimeSession
    results: list[TurnBenchmarkResult] = []
    room = FakeRoom()
    audio_source = BenchmarkAudioSource(
        sample_rate=settings.livekit_output_sample_rate,
        queue_size_ms=settings.livekit_output_queue_ms,
    )
    audio_publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=settings.livekit_output_sample_rate,
        output_frame_ms=settings.livekit_output_frame_ms,
    )
    bridge = worker_module.ParticipantBridgeSession(
        settings=settings,
        participant_identity="benchmark-browser",
        room=room,
        audio_publisher=audio_publisher,
        metric_rollups=worker_module.MetricRollupWindow(),
    )

    try:
        await bridge.handle_control_message({"type": "session.start"})
        baseline_index = len(room.local_participant.published_payloads)
        total_runs = args.warmup_runs + args.runs
        for run_index in range(1, total_runs + 1):
            audio_source.clear_queue()
            await bridge.handle_control_message({"type": "client.speech.start"})
            for frame_bytes in iter_pcm16_chunks(
                input_pcm16,
                duration_ms=args.input_frame_ms,
                sample_rate=settings.qwen_input_sample_rate,
                pad_final_chunk=True,
            ):
                await bridge.ingest_audio_frame(
                    worker_module.rtc.AudioFrame(
                        data=frame_bytes,
                        sample_rate=settings.qwen_input_sample_rate,
                        num_channels=1,
                        samples_per_channel=len(frame_bytes) // 2,
                    )
                )
            await bridge.handle_control_message({"type": "client.speech.commit"})
            payloads = await _wait_for_turn_payloads(
                room.local_participant,
                start_index=baseline_index,
            )
            baseline_index = len(room.local_participant.published_payloads)
            metrics_payload = next(
                (
                    payload
                    for payload in reversed(payloads)
                    if payload.get("type") == "metrics.update"
                ),
                None,
            )
            if metrics_payload is None:
                raise RuntimeError("Turn benchmark did not receive metrics.update.")
            if run_index > args.warmup_runs:
                results.append(
                    TurnBenchmarkResult(
                        run=run_index - args.warmup_runs,
                        turn_id=_extract_turn_id(payloads),
                        metrics=dict(metrics_payload.get("metrics") or {}),
                        timestamps=dict(metrics_payload.get("timestamps") or {}),
                    )
                )
    finally:
        await bridge.close()
        worker_module.RealtimeSession = original_realtime_session

    speech_egress_values = [
        float(result.metrics["speech_end_to_first_assistant_egress_ms"])
        for result in results
        if isinstance(result.metrics.get("speech_end_to_first_assistant_egress_ms"), (int, float))
    ]
    aggregate = {
        "speech_end_to_first_assistant_egress_p50_ms": _percentile(speech_egress_values, 50),
        "speech_end_to_first_assistant_egress_p95_ms": _percentile(speech_egress_values, 95),
        "commit_to_qwen_first_audio_p95_ms": _percentile(
            [
                float(result.metrics["commit_to_qwen_first_audio_ms"])
                for result in results
                if isinstance(result.metrics.get("commit_to_qwen_first_audio_ms"), (int, float))
            ],
            95,
        ),
        "qwen_first_audio_to_livekit_first_frame_p95_ms": _percentile(
            [
                float(result.metrics["qwen_first_audio_to_livekit_first_frame_ms"])
                for result in results
                if isinstance(
                    result.metrics.get("qwen_first_audio_to_livekit_first_frame_ms"),
                    (int, float),
                )
            ],
            95,
        ),
    }
    payload = {
        "mode": "in_process_stub_turn",
        "runs": args.runs,
        "warmup_runs": args.warmup_runs,
        "require_p95_ms": args.require_p95_ms,
        "turns": [
            {
                "run": row.run,
                "turn_id": row.turn_id,
                "metrics": row.metrics,
                "timestamps": row.timestamps,
            }
            for row in results
        ],
        "aggregate": aggregate,
    }

    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(json.dumps(payload, indent=2), flush=True)

    p95_speech_egress_ms = aggregate["speech_end_to_first_assistant_egress_p95_ms"]
    if p95_speech_egress_ms is not None and p95_speech_egress_ms > args.require_p95_ms:
        return 1
    return 0


async def _wait_for_turn_payloads(
    local_participant: FakeLocalParticipant,
    *,
    start_index: int,
    timeout_seconds: float = 5.0,
) -> list[dict]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payloads = local_participant.published_payloads[start_index:]
        if any(payload.get("type") == "assistant.done" for payload in payloads):
            return payloads
        await asyncio.sleep(0.001)
    raise TimeoutError("Timed out waiting for assistant.done in realtime turn benchmark.")


def _extract_turn_id(payloads: list[dict]) -> str | None:
    for payload in payloads:
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            return turn_id
    return None


def _load_input_pcm16(path: Path | None) -> bytes:
    if path is None:
        return _tone_pcm16(sample_rate=16000, duration_ms=640)
    absolute_path = path if path.is_absolute() else REPO_ROOT / path
    with wave.open(str(absolute_path), "rb") as wav_file:
        if wav_file.getnchannels() != 1:
            raise ValueError("Input WAV must be mono.")
        if wav_file.getsampwidth() != 2:
            raise ValueError("Input WAV must be PCM16.")
        if wav_file.getframerate() != 16000:
            raise ValueError("Input WAV must be 16 kHz.")
        return wav_file.readframes(wav_file.getnframes())


def _percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 2)
    quantiles = statistics.quantiles(values, n=100, method="inclusive")
    return round(quantiles[pct - 1], 2)


def _tone_pcm16(*, sample_rate: int, duration_ms: int, amplitude: int = 1200) -> bytes:
    sample_count = max(1, int(sample_rate * duration_ms / 1000))
    pcm16 = array("h", [amplitude] * sample_count)
    return pcm16.tobytes()


def _tone_base64(*, sample_rate: int, duration_ms: int, amplitude: int = 1200) -> str:
    return base64.b64encode(
        _tone_pcm16(sample_rate=sample_rate, duration_ms=duration_ms, amplitude=amplitude)
    ).decode("ascii")


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
