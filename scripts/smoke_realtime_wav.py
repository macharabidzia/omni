#!/usr/bin/env python3
import argparse
import asyncio
import base64
import json
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import websockets

from apps.gateway.src.realtime.audio import chunk_bytes_for_duration_ms
from apps.gateway.src.realtime.event_map import normalize_qwen_event
from apps.gateway.src.realtime.metrics import SessionMetrics
from apps.gateway.src.realtime.qwen_client import QwenRealtimeClient


@dataclass
class SessionRecorder:
    metrics: SessionMetrics = field(default_factory=SessionMetrics)
    events: list[dict] = field(default_factory=list)
    assistant_audio_bytes: bytearray = field(default_factory=bytearray)
    assistant_audio_sample_rate: int = 24000
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def record(self, event: dict) -> None:
        self.events.append(event)
        event_type = event.get("type")
        if event_type == "transcript.delta":
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
            self.done.set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream a WAV file into Qwen realtime or the gateway.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--direct", action="store_true")
    mode.add_argument("--gateway", action="store_true")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--chunk-ms", type=int, default=200)
    parser.add_argument("--output", type=Path, default=Path("output.wav"))
    parser.add_argument("--events", type=Path, default=Path("events.json"))
    parser.add_argument("--metrics", type=Path, default=Path("metrics.json"))
    parser.add_argument("--speaker", default="Ethan")
    parser.add_argument("--modalities", default="text,audio")
    parser.add_argument("--instructions", default=None)
    parser.add_argument("--gateway-url", default="ws://localhost:8080/ws/realtime")
    parser.add_argument("--qwen-url", default="ws://localhost:8091/v1/realtime")
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
    chunk_size = chunk_bytes_for_duration_ms(chunk_ms, sample_rate=16000)
    for start in range(0, len(audio_bytes), chunk_size):
        yield audio_bytes[start : start + chunk_size]


async def run_direct(args: argparse.Namespace, audio_bytes: bytes, recorder: SessionRecorder) -> None:
    modalities = [value.strip() for value in args.modalities.split(",") if value.strip()]
    client = QwenRealtimeClient(
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

    for index, chunk in enumerate(iter_chunks(audio_bytes, chunk_ms=args.chunk_ms)):
        if index == 0:
            recorder.metrics.mark_once("t_first_audio_chunk_sent")
        await client.append_audio(base64.b64encode(chunk).decode("ascii"))

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


async def run_gateway(args: argparse.Namespace, audio_bytes: bytes, recorder: SessionRecorder) -> None:
    modalities = [value.strip() for value in args.modalities.split(",") if value.strip()]

    async with websockets.connect(args.gateway_url, max_size=4 * 1024 * 1024) as websocket:
        reader_task = asyncio.create_task(collect_gateway_events(websocket, recorder))
        await websocket.send(
            json.dumps(
                {
                    "type": "session.start",
                    "speaker": args.speaker,
                    "modalities": modalities,
                    "input_sample_rate": 16000,
                    "output_audio": "audio" in modalities,
                }
            )
        )
        recorder.metrics.mark_once("t_microphone_started")

        for index, chunk in enumerate(iter_chunks(audio_bytes, chunk_ms=args.chunk_ms)):
            if index == 0:
                recorder.metrics.mark_once("t_first_audio_chunk_sent")
            await websocket.send(
                json.dumps(
                    {
                        "type": "audio.append",
                        "audio_base64": base64.b64encode(chunk).decode("ascii"),
                        "sample_rate": 16000,
                        "channels": 1,
                        "format": "pcm16",
                    }
                )
            )

        recorder.metrics.mark_once("t_audio_commit_sent")
        await websocket.send(json.dumps({"type": "audio.commit"}))
        await asyncio.wait_for(recorder.done.wait(), timeout=args.response_timeout_seconds)
        await websocket.send(json.dumps({"type": "session.end"}))
        await reader_task


async def collect_gateway_events(websocket, recorder: SessionRecorder) -> None:
    while True:
        try:
            raw_message = await websocket.recv()
        except websockets.ConnectionClosed:
            return
        event = json.loads(raw_message)
        recorder.record(event)


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

    if args.direct:
        await run_direct(args, audio_bytes, recorder)
    else:
        await run_gateway(args, audio_bytes, recorder)

    save_output_wav(args.output, recorder)
    save_json(args.events, recorder.events)
    save_json(
        args.metrics,
        {
            "mode": "direct" if args.direct else "gateway",
            "speaker": args.speaker,
            "modalities": args.modalities,
            "chunk_ms": args.chunk_ms,
            **recorder.metrics.snapshot(),
        },
    )


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

