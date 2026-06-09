#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import math
import os
import sys
import wave
from io import BytesIO
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_text(name: str, default: str) -> str:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else raw


STUB_OUTPUT_SAMPLE_RATE = _env_int("STUB_QWEN_OUTPUT_SAMPLE_RATE", 24000)
STUB_INPUT_SAMPLE_RATE = _env_int("STUB_QWEN_INPUT_SAMPLE_RATE", 16000)
STUB_FIRST_AUDIO_DELAY_MS = _env_float("STUB_QWEN_FIRST_AUDIO_DELAY_MS", 35.0)
STUB_DONE_DELAY_MS = _env_float("STUB_QWEN_DONE_DELAY_MS", 25.0)
STUB_AUDIO_CHUNK_MS = _env_int("STUB_QWEN_AUDIO_CHUNK_MS", 40)
STUB_TONE_FREQUENCY_HZ = _env_float("STUB_QWEN_TONE_FREQUENCY_HZ", 440.0)
STUB_TONE_AMPLITUDE = _env_float("STUB_QWEN_TONE_AMPLITUDE", 0.08)
STUB_TEXT = _env_text("STUB_QWEN_TEXT", "Stub assistant ready.")
STUB_TRANSCRIPT = _env_text("STUB_QWEN_TRANSCRIPT", "stub transcript")

app = FastAPI(title="Stub Qwen Realtime Server", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "backend": "stub_qwen",
        "input_sample_rate": STUB_INPUT_SAMPLE_RATE,
        "output_sample_rate": STUB_OUTPUT_SAMPLE_RATE,
    }


@app.websocket("/v1/realtime")
async def realtime_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json({"type": "session.created", "id": "stub-session"})
    buffered_audio_chunks: list[str] = []
    current_task: asyncio.Task | None = None
    try:
        while True:
            raw_message = await websocket.receive_text()
            payload = json.loads(raw_message)
            event_type = payload.get("type")
            if event_type == "input_audio_buffer.append":
                audio_base64 = payload.get("audio")
                if isinstance(audio_base64, str) and audio_base64:
                    buffered_audio_chunks.append(audio_base64)
                continue
            if event_type != "input_audio_buffer.commit":
                continue
            if payload.get("final") is not True:
                continue
            if current_task is not None and not current_task.done():
                current_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await current_task
            current_task = asyncio.create_task(
                _emit_realtime_response(
                    websocket,
                    buffered_audio_chunks=buffered_audio_chunks,
                )
            )
            buffered_audio_chunks = []
    except WebSocketDisconnect:
        if current_task is not None and not current_task.done():
            current_task.cancel()
    finally:
        if current_task is not None and not current_task.done():
            current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await current_task


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(payload: dict):
    stream = bool(payload.get("stream", False))
    modalities = payload.get("modalities") or ["text", "audio"]
    if not stream:
        return JSONResponse(
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "model": payload.get("model", "stub-qwen"),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": STUB_TEXT if "text" in modalities else "",
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        )

    async def event_stream():
        if "text" in modalities:
            yield f"data: {json.dumps(_chat_stream_payload(modality='text', content=STUB_TEXT))}\n\n"
        if "audio" in modalities:
            audio_wav_base64 = base64.b64encode(
                _tone_wav_bytes(
                    sample_rate=STUB_OUTPUT_SAMPLE_RATE,
                    duration_ms=STUB_AUDIO_CHUNK_MS,
                )
            ).decode("ascii")
            yield f"data: {json.dumps(_chat_stream_payload(modality='audio', content=audio_wav_base64))}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _chat_stream_payload(*, modality: str, content: str) -> dict[str, object]:
    return {
        "id": "chatcmpl-stub-stream",
        "object": "chat.completion.chunk",
        "modality": modality,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "content": content,
                },
            }
        ],
    }


async def _emit_realtime_response(
    websocket: WebSocket,
    *,
    buffered_audio_chunks: list[str],
) -> None:
    del buffered_audio_chunks
    await asyncio.sleep(STUB_FIRST_AUDIO_DELAY_MS / 1000)
    await websocket.send_json(
        {
            "type": "response.audio_transcript.delta",
            "delta": STUB_TRANSCRIPT,
        }
    )
    await websocket.send_json(
        {
            "type": "response.text.delta",
            "delta": STUB_TEXT,
        }
    )
    await websocket.send_json(
        {
            "type": "response.audio.delta",
            "delta": _tone_base64(
                sample_rate=STUB_OUTPUT_SAMPLE_RATE,
                duration_ms=STUB_AUDIO_CHUNK_MS,
            ),
            "sample_rate": STUB_OUTPUT_SAMPLE_RATE,
        }
    )
    await asyncio.sleep(STUB_DONE_DELAY_MS / 1000)
    await websocket.send_json({"type": "response.done"})


def _tone_samples(
    *,
    sample_rate: int,
    duration_ms: int,
    amplitude: float = STUB_TONE_AMPLITUDE,
    frequency_hz: float = STUB_TONE_FREQUENCY_HZ,
) -> bytes:
    frame_count = max(1, int(sample_rate * duration_ms / 1000))
    pcm16 = bytearray()
    for index in range(frame_count):
        sample = amplitude * math.sin(2 * math.pi * frequency_hz * (index / sample_rate))
        value = int(max(-1.0, min(1.0, sample)) * 32767)
        pcm16.extend(value.to_bytes(2, byteorder="little", signed=True))
    return bytes(pcm16)


def _tone_base64(*, sample_rate: int, duration_ms: int) -> str:
    return base64.b64encode(
        _tone_samples(sample_rate=sample_rate, duration_ms=duration_ms)
    ).decode("ascii")


def _tone_wav_bytes(*, sample_rate: int, duration_ms: int) -> bytes:
    pcm16 = _tone_samples(sample_rate=sample_rate, duration_ms=duration_ms)
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16)
    return buffer.getvalue()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a local stub Qwen server that exposes /health, /v1/realtime, and "
            "/v1/chat/completions for gateway/browser smoke tests when a real model is unavailable."
        )
    )
    parser.add_argument("--host", default=os.getenv("STUB_QWEN_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=_env_int("STUB_QWEN_PORT", 17091))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
