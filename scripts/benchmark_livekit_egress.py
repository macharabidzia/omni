#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import sys
import time
from array import array
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.gateway.src.livekit.output import AssistantAudioPublisher
from apps.gateway.src.realtime.audio import iter_pcm16_chunks


@dataclass(slots=True)
class EgressMeasurement:
    run: int
    first_capture_ms: float
    total_publish_ms: float
    published_frames: int
    capture_call_p95_ms: float


class BenchmarkAudioSource:
    def __init__(self, *, sample_rate: int, queue_size_ms: int) -> None:
        self.sample_rate = sample_rate
        self.queue_size_ms = queue_size_ms
        self.num_channels = 1
        self.frames = []
        self._queued_duration = 0.0
        self.capture_call_durations_ms: list[float] = []
        self.first_capture_at: float | None = None

    async def capture_frame(self, frame) -> None:
        started_at = time.perf_counter()
        if self.first_capture_at is None:
            self.first_capture_at = started_at
        self.frames.append(frame)
        finished_at = time.perf_counter()
        self.capture_call_durations_ms.append(round((finished_at - started_at) * 1000, 4))

    def clear_queue(self) -> None:
        self.frames.clear()
        self.first_capture_at = None
        self.capture_call_durations_ms.clear()

    async def wait_for_playout(self) -> None:
        return None

    @property
    def queued_duration(self) -> float:
        return self._queued_duration


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark the AssistantAudioPublisher path in-process with a fake LiveKit AudioSource. "
            "This is a gateway-only egress benchmark and does not require a model backend."
        )
    )
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--warmup-runs", type=int, default=2)
    parser.add_argument("--input-sample-rate", type=int, default=24000)
    parser.add_argument("--output-sample-rate", type=int, default=48000)
    parser.add_argument("--output-frame-ms", type=int, default=10)
    parser.add_argument("--output-queue-ms", type=int, default=150)
    parser.add_argument("--audio-duration-ms", type=int, default=320)
    parser.add_argument("--chunk-ms", type=int, default=40)
    parser.add_argument("--first-frame-threshold-ms", type=float, default=30.0)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


async def run_measurement(
    *,
    run: int,
    pcm16_audio: bytes,
    input_sample_rate: int,
    output_sample_rate: int,
    output_frame_ms: int,
    output_queue_ms: int,
    chunk_ms: int,
) -> EgressMeasurement:
    audio_source = BenchmarkAudioSource(
        sample_rate=output_sample_rate,
        queue_size_ms=output_queue_ms,
    )
    publisher = AssistantAudioPublisher(
        audio_source=audio_source,
        output_sample_rate=output_sample_rate,
        output_frame_ms=output_frame_ms,
    )

    started_at = time.perf_counter()
    for chunk in iter_pcm16_chunks(
        pcm16_audio,
        duration_ms=chunk_ms,
        sample_rate=input_sample_rate,
        pad_final_chunk=True,
    ):
        await publisher.enqueue_base64(
            base64.b64encode(chunk).decode("ascii"),
            input_sample_rate=input_sample_rate,
        )
    await publisher.finalize_turn()
    finished_at = time.perf_counter()

    if audio_source.first_capture_at is None:
        raise RuntimeError("Egress benchmark did not publish a first LiveKit frame.")

    return EgressMeasurement(
        run=run,
        first_capture_ms=round((audio_source.first_capture_at - started_at) * 1000, 2),
        total_publish_ms=round((finished_at - started_at) * 1000, 2),
        published_frames=len(audio_source.frames),
        capture_call_p95_ms=_percentile(audio_source.capture_call_durations_ms, 95) or 0.0,
    )


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
    pcm16_audio = _tone_pcm16(
        sample_rate=args.input_sample_rate,
        duration_ms=args.audio_duration_ms,
    )

    measurements: list[EgressMeasurement] = []
    total_runs = args.warmup_runs + args.runs
    for run_index in range(1, total_runs + 1):
        measurement = await run_measurement(
            run=run_index,
            pcm16_audio=pcm16_audio,
            input_sample_rate=args.input_sample_rate,
            output_sample_rate=args.output_sample_rate,
            output_frame_ms=args.output_frame_ms,
            output_queue_ms=args.output_queue_ms,
            chunk_ms=args.chunk_ms,
        )
        if run_index > args.warmup_runs:
            measurements.append(
                EgressMeasurement(
                    run=run_index - args.warmup_runs,
                    first_capture_ms=measurement.first_capture_ms,
                    total_publish_ms=measurement.total_publish_ms,
                    published_frames=measurement.published_frames,
                    capture_call_p95_ms=measurement.capture_call_p95_ms,
                )
            )

    first_capture_values = [row.first_capture_ms for row in measurements]
    aggregate = {
        "first_capture_p50_ms": _percentile(first_capture_values, 50),
        "first_capture_p95_ms": _percentile(first_capture_values, 95),
        "first_capture_p99_ms": _percentile(first_capture_values, 99),
        "total_publish_p95_ms": _percentile([row.total_publish_ms for row in measurements], 95),
        "capture_call_p95_ms": _percentile([row.capture_call_p95_ms for row in measurements], 95),
        "average_published_frames": round(
            sum(row.published_frames for row in measurements) / len(measurements),
            2,
        ),
    }

    payload = {
        "runs": args.runs,
        "warmup_runs": args.warmup_runs,
        "output_frame_ms": args.output_frame_ms,
        "output_queue_ms": args.output_queue_ms,
        "measurements": [asdict(row) for row in measurements],
        "aggregate": aggregate,
        "threshold_ms": args.first_frame_threshold_ms,
    }

    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(json.dumps(payload, indent=2), flush=True)

    p95_first_capture_ms = aggregate["first_capture_p95_ms"]
    if (
        p95_first_capture_ms is not None
        and p95_first_capture_ms > args.first_frame_threshold_ms
    ):
        return 1
    return 0


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


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
