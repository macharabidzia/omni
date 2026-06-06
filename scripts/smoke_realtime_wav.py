#!/usr/bin/env python3
import argparse
import asyncio
import base64
import json
import os
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.gateway.src.realtime.audio import iter_pcm16_chunks
from apps.gateway.src.realtime.event_map import normalize_qwen_event
from apps.gateway.src.realtime.metrics import SessionMetrics
from apps.gateway.src.realtime.qwen_client import QwenRealtimeClient

DEFAULT_LOCAL_MODEL = REPO_ROOT / "models" / "Qwen3-Omni-30B-A3B-Instruct"


@dataclass
class SessionRecorder:
    metrics: SessionMetrics = field(default_factory=SessionMetrics)
    events: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    assistant_audio_bytes: bytearray = field(default_factory=bytearray)
    assistant_audio_sample_rate: int = 24000
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def record(self, event: dict) -> None:
        event_with_time = {
            **event,
            "t_ms": round((time.perf_counter() - self.metrics.t_session_start) * 1000, 2),
        }
        self.events.append(event_with_time)
        event_type = event.get("type")
        if event_type == "session.ready":
            self.ready.set()
        elif event_type == "transcript.delta":
            self.metrics.mark_once("t_first_transcript_delta")
        elif event_type == "assistant.text.delta":
            self.metrics.mark_once("t_first_text_delta")
        elif event_type == "assistant.audio.delta":
            self.metrics.mark_once("t_first_audio_delta_received")
            self.metrics.mark_once("t_first_audio_played")
            self.assistant_audio_sample_rate = int(event.get("sample_rate", self.assistant_audio_sample_rate))
            audio_base64 = event.get("audio_base64", "")
            if audio_base64:
                self.assistant_audio_bytes.extend(base64.b64decode(audio_base64))
        elif event_type == "assistant.done":
            self.metrics.mark_once("t_response_done")
            self.done.set()
        elif event_type == "error":
            self.errors.append(event)
            self.done.set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a direct Qwen realtime smoke test from a WAV file.")
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Retained for compatibility. Direct Qwen mode is the only supported mode.",
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--chunk-ms", type=int, default=20)
    parser.add_argument("--output", type=Path, default=Path("output.wav"))
    parser.add_argument("--events", type=Path, default=Path("events.json"))
    parser.add_argument("--metrics", type=Path, default=Path("metrics.json"))
    parser.add_argument("--speaker", default="Ethan")
    parser.add_argument("--modalities", default="text,audio")
    parser.add_argument("--instructions", default=None)
    parser.add_argument(
        "--model",
        default=os.environ.get("QWEN_MODEL", str(DEFAULT_LOCAL_MODEL)),
    )
    parser.add_argument("--qwen-url", default="ws://localhost:8091/v1/realtime")
    parser.add_argument("--send-delay-ms", type=float, default=0.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--response-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--output-sample-rate", type=int, default=24000)
    return parser.parse_args()


def load_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav_file:
        if wav_file.getnchannels() != 1:
            raise ValueError("Input WAV must be mono.")
        if wav_file.getsampwidth() != 2:
            raise ValueError("Input WAV must be PCM16.")
        if wav_file.getframerate() != 16000:
            raise ValueError("Input WAV must be 16 kHz.")
        return wav_file.readframes(wav_file.getnframes())


def iter_chunks(audio_bytes: bytes, *, chunk_ms: int) -> Iterable[bytes]:
    yield from iter_pcm16_chunks(
        audio_bytes,
        duration_ms=chunk_ms,
        sample_rate=16000,
        pad_final_chunk=True,
    )


async def run_direct(args: argparse.Namespace, audio_bytes: bytes, recorder: SessionRecorder) -> None:
    modalities = [value.strip() for value in args.modalities.split(",") if value.strip()]
    client = QwenRealtimeClient(
        model=args.model,
        url=args.qwen_url,
        request_timeout_seconds=args.request_timeout_seconds,
        response_timeout_seconds=args.response_timeout_seconds,
        output_sample_rate=args.output_sample_rate,
        max_ws_message_bytes=4 * 1024 * 1024,
        debug_raw_events=False,
        instructions=args.instructions,
    )

    await client.connect()
    await client.start_session(
        speaker=args.speaker,
        modalities=modalities,
        input_sample_rate=16000,
        output_audio="audio" in modalities,
    )

    reader_task = asyncio.create_task(collect_direct_events(client, recorder))
    recorder.events.append({"type": "session.ready", "mode": "direct"})
    recorder.metrics.mark_once("t_microphone_started")

    chunks = list(iter_chunks(audio_bytes, chunk_ms=args.chunk_ms))
    for index, chunk in enumerate(chunks):
        if index == 0:
            recorder.metrics.mark_once("t_first_audio_chunk_sent")
        await client.append_audio(base64.b64encode(chunk).decode("ascii"))
        if args.send_delay_ms > 0 and index + 1 < len(chunks):
            await asyncio.sleep(args.send_delay_ms / 1000)

    recorder.metrics.mark_once("t_audio_commit_sent")
    await client.commit_audio()
    await asyncio.wait_for(recorder.done.wait(), timeout=args.response_timeout_seconds)
    await client.close()
    await reader_task


async def collect_direct_events(client: QwenRealtimeClient, recorder: SessionRecorder) -> None:
    async for event in client.iter_events():
        normalized = normalize_qwen_event(event)
        if normalized is not None:
            recorder.record(normalized)


def save_output_wav(output_path: Path, recorder: SessionRecorder) -> None:
    if not recorder.assistant_audio_bytes:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(recorder.assistant_audio_sample_rate)
        wav_file.writeframes(bytes(recorder.assistant_audio_bytes))


def save_json(path: Path, payload: dict | list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


async def main_async() -> None:
    args = parse_args()
    audio_bytes = load_wav(args.input)
    recorder = SessionRecorder()
    requested_modalities = [value.strip() for value in args.modalities.split(",") if value.strip()]

    await run_direct(args, audio_bytes, recorder)

    snapshot = recorder.metrics.snapshot()
    save_output_wav(args.output, recorder)
    save_json(args.events, recorder.events)
    save_json(
        args.metrics,
        {
            "mode": "direct",
            "speaker": args.speaker,
            "modalities": args.modalities,
            "chunk_ms": args.chunk_ms,
            **snapshot,
        },
    )

    summary = {
        "mode": "direct",
        "speaker": args.speaker,
        "modalities": requested_modalities,
        "chunk_ms": args.chunk_ms,
        "event_count": len(recorder.events),
        "assistant_audio_bytes": len(recorder.assistant_audio_bytes),
        "errors": recorder.errors,
        "metrics": snapshot["metrics"],
    }
    print(json.dumps(summary, indent=2), flush=True)

    if recorder.errors:
        first_error = recorder.errors[0]
        raise SystemExit(
            f"Smoke test failed: {first_error.get('code', 'UNKNOWN_ERROR')}: {first_error.get('message', 'Unknown error')}"
        )

    if not any(event.get("type") == "assistant.done" for event in recorder.events):
        raise SystemExit("Smoke test failed: assistant.done was not received.")

    if "audio" in requested_modalities and not recorder.assistant_audio_bytes:
        raise SystemExit("Smoke test failed: no assistant audio was received.")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
