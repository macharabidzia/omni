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

import websockets

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.gateway.src.realtime.qwen_client import QwenRealtimeClient


@dataclass(slots=True)
class TurnMeasurement:
    run: int
    append_audio_ms: float
    commit_send_ms: float
    commit_to_first_audio_ms: float
    commit_to_done_ms: float


class StubRealtimeServer:
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
        self.server: websockets.server.Serve | None = None
        self.url: str | None = None

    async def __aenter__(self) -> "StubRealtimeServer":
        self.server = await websockets.serve(self._handle_connection, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _handle_connection(self, websocket) -> None:
        await websocket.send(json.dumps({"type": "session.created"}))
        async for message in websocket:
            payload = json.loads(message)
            if (
                payload.get("type") == "input_audio_buffer.commit"
                and payload.get("final") is True
            ):
                await asyncio.sleep(self.first_audio_delay_ms / 1000)
                await websocket.send(
                    json.dumps(
                        {
                            "type": "response.audio.delta",
                            "delta": _tone_base64(
                                sample_rate=self.output_sample_rate,
                                duration_ms=20,
                            ),
                            "sample_rate": self.output_sample_rate,
                        }
                    )
                )
                await asyncio.sleep(self.done_delay_ms / 1000)
                await websocket.send(json.dumps({"type": "response.done"}))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure warm Qwen realtime append/commit/first-audio latency. "
            "Defaults to an in-process stub websocket so the benchmark runs without a live model."
        )
    )
    parser.add_argument("--runs", type=int, default=8)
    parser.add_argument("--warmup-runs", type=int, default=2)
    parser.add_argument("--request-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--response-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--input-chunk-ms", type=int, default=80)
    parser.add_argument("--input-sample-rate", type=int, default=16000)
    parser.add_argument("--output-sample-rate", type=int, default=24000)
    parser.add_argument("--model", default="stub-qwen-realtime")
    parser.add_argument("--realtime-url", default=None)
    parser.add_argument("--stub-first-audio-delay-ms", type=float, default=35.0)
    parser.add_argument("--stub-done-delay-ms", type=float, default=25.0)
    parser.add_argument("--first-audio-threshold-ms", type=float, default=250.0)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


async def measure_turn(
    client: QwenRealtimeClient,
    *,
    run: int,
    audio_base64: str,
) -> TurnMeasurement:
    append_started_at = time.perf_counter()
    await client.append_audio(audio_base64)
    append_finished_at = time.perf_counter()

    commit_started_at = time.perf_counter()
    await client.commit_audio()
    commit_finished_at = time.perf_counter()

    first_audio_at: float | None = None
    done_at: float | None = None
    async for event in client.iter_events():
        now = time.perf_counter()
        if event.kind == "assistant_audio_delta" and first_audio_at is None:
            first_audio_at = now
        if event.kind == "error":
            raise RuntimeError(f"{event.code or 'QWEN_ERROR'}: {event.message or 'unknown error'}")
        if event.kind == "response_done":
            done_at = now
            break

    if first_audio_at is None or done_at is None:
        raise RuntimeError("Stub benchmark did not observe assistant audio and response.done.")

    return TurnMeasurement(
        run=run,
        append_audio_ms=round((append_finished_at - append_started_at) * 1000, 2),
        commit_send_ms=round((commit_finished_at - commit_started_at) * 1000, 2),
        commit_to_first_audio_ms=round((first_audio_at - commit_started_at) * 1000, 2),
        commit_to_done_ms=round((done_at - commit_started_at) * 1000, 2),
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
    input_audio_base64 = _tone_base64(
        sample_rate=args.input_sample_rate,
        duration_ms=args.input_chunk_ms,
    )

    measurements: list[TurnMeasurement] = []

    if args.realtime_url:
        benchmark_url = args.realtime_url
        server_cm = _null_async_context()
    else:
        server_cm = StubRealtimeServer(
            first_audio_delay_ms=args.stub_first_audio_delay_ms,
            done_delay_ms=args.stub_done_delay_ms,
            output_sample_rate=args.output_sample_rate,
        )

    async with server_cm as maybe_server:
        if args.realtime_url:
            benchmark_url = args.realtime_url
        else:
            assert isinstance(maybe_server, StubRealtimeServer)
            assert maybe_server.url is not None
            benchmark_url = maybe_server.url

        client = QwenRealtimeClient(
            model=args.model,
            url=benchmark_url,
            request_timeout_seconds=args.request_timeout_seconds,
            response_timeout_seconds=args.response_timeout_seconds,
            output_sample_rate=args.output_sample_rate,
            max_ws_message_bytes=4 * 1024 * 1024,
            debug_raw_events=False,
            instructions="Reply briefly.",
        )

        connect_started_at = time.perf_counter()
        await client.connect()
        connect_finished_at = time.perf_counter()
        session_started_at = time.perf_counter()
        await client.start_session(
            speaker="Ethan",
            modalities=["text", "audio"],
            input_sample_rate=args.input_sample_rate,
            output_audio=True,
        )
        session_finished_at = time.perf_counter()

        try:
            total_runs = args.warmup_runs + args.runs
            for run_index in range(1, total_runs + 1):
                measurement = await measure_turn(
                    client,
                    run=run_index,
                    audio_base64=input_audio_base64,
                )
                if run_index > args.warmup_runs:
                    measurements.append(
                        TurnMeasurement(
                            run=run_index - args.warmup_runs,
                            append_audio_ms=measurement.append_audio_ms,
                            commit_send_ms=measurement.commit_send_ms,
                            commit_to_first_audio_ms=measurement.commit_to_first_audio_ms,
                            commit_to_done_ms=measurement.commit_to_done_ms,
                        )
                    )
        finally:
            await client.close()

    aggregate = {
        "connect_ms": round((connect_finished_at - connect_started_at) * 1000, 2),
        "session_update_ms": round((session_finished_at - session_started_at) * 1000, 2),
        "append_audio_p50_ms": _percentile([row.append_audio_ms for row in measurements], 50),
        "append_audio_p95_ms": _percentile([row.append_audio_ms for row in measurements], 95),
        "commit_send_p50_ms": _percentile([row.commit_send_ms for row in measurements], 50),
        "commit_send_p95_ms": _percentile([row.commit_send_ms for row in measurements], 95),
        "commit_to_first_audio_p50_ms": _percentile(
            [row.commit_to_first_audio_ms for row in measurements], 50
        ),
        "commit_to_first_audio_p95_ms": _percentile(
            [row.commit_to_first_audio_ms for row in measurements], 95
        ),
        "commit_to_done_p50_ms": _percentile([row.commit_to_done_ms for row in measurements], 50),
        "commit_to_done_p95_ms": _percentile([row.commit_to_done_ms for row in measurements], 95),
    }
    payload = {
        "mode": "realtime_stub" if not args.realtime_url else "realtime_live",
        "runs": args.runs,
        "warmup_runs": args.warmup_runs,
        "benchmark_url": benchmark_url,
        "first_audio_threshold_ms": args.first_audio_threshold_ms,
        "measurements": [asdict(row) for row in measurements],
        "aggregate": aggregate,
    }

    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(json.dumps(payload, indent=2), flush=True)
    p95_first_audio_ms = aggregate["commit_to_first_audio_p95_ms"]
    if p95_first_audio_ms is not None and p95_first_audio_ms > args.first_audio_threshold_ms:
        return 1
    return 0


class _null_async_context:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb


def _percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 2)
    quantiles = statistics.quantiles(values, n=100, method="inclusive")
    return round(quantiles[pct - 1], 2)


def _tone_base64(*, sample_rate: int, duration_ms: int, amplitude: int = 1200) -> str:
    sample_count = max(1, int(sample_rate * duration_ms / 1000))
    pcm16 = array("h", [amplitude] * sample_count)
    return base64.b64encode(pcm16.tobytes()).decode("ascii")


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
