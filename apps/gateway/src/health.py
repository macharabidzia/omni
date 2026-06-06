from http import HTTPStatus
import base64
from functools import lru_cache
import math
import time
from pathlib import Path
import wave

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from livekit import api as livekit_api

from src.config import Settings, get_settings
from src.realtime.audio import iter_pcm16_chunks
from src.realtime.qwen_client import QwenRealtimeClient, probe_qwen_realtime_websocket

router = APIRouter()
_QWEN_INFERENCE_PROBE_TIMEOUT_SECONDS = 10.0
_QWEN_INFERENCE_PROBE_DURATION_SECONDS = 0.5
_QWEN_INFERENCE_PROBE_WAV_DURATION_SECONDS = 0.8
_QWEN_INFERENCE_PROBE_FREQUENCY_HZ = 440.0
_QWEN_INFERENCE_PROBE_AMPLITUDE = 0.2
_QWEN_INFERENCE_PROBE_SUCCESS_TTL_SECONDS = 15.0
_last_qwen_inference_probe_ok_at: float | None = None
_REPO_ROOT = Path(__file__).resolve().parents[3]
_QWEN_INFERENCE_PROBE_WAV_PATH = _REPO_ROOT / "tmp" / "fake_mic_turn.wav"


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "container_alive"}


@router.get("/ready")
async def ready(deep: bool = Query(default=True)) -> JSONResponse:
    settings = get_settings()
    qwen_result = await probe_qwen(settings, deep=deep)
    livekit_result = await probe_livekit(settings)
    result = compose_ready_payload(qwen_result=qwen_result, livekit_result=livekit_result)
    status_code = HTTPStatus.OK if result["status"] == "ready" else HTTPStatus.SERVICE_UNAVAILABLE
    return JSONResponse(status_code=status_code, content=result)


async def probe_qwen(settings: Settings, *, deep: bool = False) -> dict[str, str]:
    try:
        async with httpx.AsyncClient(timeout=settings.qwen_request_timeout_seconds) as client:
            response = await client.get(settings.qwen_health_url)
    except Exception as exc:
        return {
            "status": "qwen_unreachable",
            "detail": f"Health probe failed: {exc}",
            "qwen_health_url": settings.qwen_health_url,
            "qwen_realtime_url": settings.qwen_realtime_url,
        }

    if response.status_code >= 500:
        return {
            "status": "qwen_loading",
            "detail": f"Health endpoint returned {response.status_code}",
            "qwen_health_url": settings.qwen_health_url,
            "qwen_realtime_url": settings.qwen_realtime_url,
        }

    try:
        await probe_qwen_realtime_websocket(
            url=settings.qwen_realtime_url,
            request_timeout_seconds=settings.qwen_request_timeout_seconds,
            max_ws_message_bytes=settings.max_ws_message_bytes,
            debug_raw_events=settings.qwen_debug_raw_events,
        )
    except Exception as exc:
        return {
            "status": "qwen_failed",
            "detail": f"Realtime websocket probe failed: {exc}",
            "qwen_health_url": settings.qwen_health_url,
            "qwen_realtime_url": settings.qwen_realtime_url,
        }

    if deep:
        inference_probe_result = await probe_qwen_realtime_inference_cached(settings)
        if inference_probe_result is not None:
            return inference_probe_result

    return {
        "status": "qwen_ready",
        "detail": (
            "Qwen health, realtime session, and inference probes succeeded."
            if deep
            else "Qwen health and realtime session probes succeeded."
        ),
        "qwen_health_url": settings.qwen_health_url,
        "qwen_realtime_url": settings.qwen_realtime_url,
    }


async def probe_qwen_realtime_inference_cached(settings: Settings) -> dict[str, str] | None:
    global _last_qwen_inference_probe_ok_at

    now = time.monotonic()
    if (
        _last_qwen_inference_probe_ok_at is not None
        and now - _last_qwen_inference_probe_ok_at <= _QWEN_INFERENCE_PROBE_SUCCESS_TTL_SECONDS
    ):
        return None

    try:
        await probe_qwen_realtime_inference(settings)
    except Exception as exc:
        return {
            "status": "qwen_failed",
            "detail": f"Realtime inference probe failed: {exc}",
            "qwen_health_url": settings.qwen_health_url,
            "qwen_realtime_url": settings.qwen_realtime_url,
        }

    _last_qwen_inference_probe_ok_at = now
    return None


async def probe_qwen_realtime_inference(settings: Settings) -> None:
    timeout_seconds = min(
        settings.qwen_response_timeout_seconds,
        _QWEN_INFERENCE_PROBE_TIMEOUT_SECONDS,
    )
    client = QwenRealtimeClient(
        model=settings.qwen_model,
        url=settings.qwen_realtime_url,
        request_timeout_seconds=settings.qwen_request_timeout_seconds,
        response_timeout_seconds=timeout_seconds,
        output_sample_rate=settings.output_sample_rate,
        max_ws_message_bytes=settings.max_ws_message_bytes,
        debug_raw_events=settings.qwen_debug_raw_events,
        instructions="Reply briefly.",
    )
    try:
        await client.connect()
        await client.start_session(
            speaker=settings.supported_speakers[0],
            modalities=["text", "audio"],
            input_sample_rate=settings.input_sample_rate,
            output_audio=True,
        )
        probe_audio = _load_probe_audio_pcm16(sample_rate=settings.input_sample_rate)
        for chunk in iter_pcm16_chunks(
            probe_audio,
            duration_ms=settings.default_smoke_chunk_ms,
            sample_rate=settings.input_sample_rate,
            pad_final_chunk=True,
        ):
            await client.append_audio(base64.b64encode(chunk).decode("ascii"))
        await client.commit_audio()

        async for event in client.iter_events():
            if event.kind == "assistant_audio_delta":
                return
            if event.kind == "error":
                raise RuntimeError(
                    f"{event.code or 'QWEN_ERROR'}: {event.message or 'unknown error'}"
                )
            if event.kind == "response_done":
                break
    finally:
        await client.close()

    raise RuntimeError("No assistant audio was received from the realtime probe.")


def _build_probe_audio_pcm16(
    *,
    duration_seconds: float,
    sample_rate: int,
    frequency_hz: float,
    amplitude: float,
) -> bytes:
    frame_count = int(duration_seconds * sample_rate)
    pcm_frames = bytearray()
    for index in range(frame_count):
        sample = amplitude * math.sin(2 * math.pi * frequency_hz * (index / sample_rate))
        value = int(max(-1.0, min(1.0, sample)) * 32767)
        pcm_frames.extend(value.to_bytes(2, byteorder="little", signed=True))
    return bytes(pcm_frames)


@lru_cache
def _load_probe_audio_pcm16(*, sample_rate: int) -> bytes:
    if _QWEN_INFERENCE_PROBE_WAV_PATH.exists():
        try:
            with wave.open(str(_QWEN_INFERENCE_PROBE_WAV_PATH), "rb") as wav_file:
                if (
                    wav_file.getnchannels() == 1
                    and wav_file.getsampwidth() == 2
                    and wav_file.getframerate() == sample_rate
                ):
                    frame_count = min(
                        wav_file.getnframes(),
                        int(sample_rate * _QWEN_INFERENCE_PROBE_WAV_DURATION_SECONDS),
                    )
                    audio_bytes = wav_file.readframes(frame_count)
                    if audio_bytes:
                        return audio_bytes
        except Exception:
            pass

    return _build_probe_audio_pcm16(
        duration_seconds=_QWEN_INFERENCE_PROBE_DURATION_SECONDS,
        sample_rate=sample_rate,
        frequency_hz=_QWEN_INFERENCE_PROBE_FREQUENCY_HZ,
        amplitude=_QWEN_INFERENCE_PROBE_AMPLITUDE,
    )


async def probe_livekit(settings: Settings) -> dict[str, str | bool | int]:
    if not settings.livekit_url:
        return {
            "status": "livekit_failed",
            "detail": "LIVEKIT_URL is not configured.",
            "livekit_url": settings.livekit_url,
            "livekit_room": settings.livekit_room,
            "livekit_worker_status": "not_configured",
            "livekit_room_joined": False,
            "livekit_output_track_status": "unknown",
        }

    if not settings.livekit_api_key or not settings.livekit_api_secret:
        return {
            "status": "livekit_failed",
            "detail": "LIVEKIT_API_KEY or LIVEKIT_API_SECRET is missing.",
            "livekit_url": settings.livekit_url,
            "livekit_room": settings.livekit_room,
            "livekit_worker_status": "not_configured",
            "livekit_room_joined": False,
            "livekit_output_track_status": "unknown",
        }

    client = livekit_api.LiveKitAPI(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret,
    )
    try:
        room_response = await client.room.list_rooms(
            livekit_api.ListRoomsRequest(names=[settings.livekit_room])
        )
        rooms = list(room_response.rooms)
        room_exists = any(room.name == settings.livekit_room for room in rooms)
        participants = []
        if room_exists:
            participant_response = await client.room.list_participants(
                livekit_api.ListParticipantsRequest(room=settings.livekit_room)
            )
            participants = list(participant_response.participants)
    except Exception as exc:
        return {
            "status": "livekit_failed",
            "detail": f"LiveKit API probe failed: {exc}",
            "livekit_url": settings.livekit_url,
            "livekit_room": settings.livekit_room,
            "livekit_worker_status": "unreachable",
            "livekit_room_joined": False,
            "livekit_output_track_status": "unknown",
        }
    finally:
        await client.aclose()

    worker = next(
        (participant for participant in participants if participant.identity == settings.livekit_agent_id),
        None,
    )
    assistant_track_ready = False
    if worker is not None:
        assistant_track_ready = any(track.name == "assistant" for track in worker.tracks)

    return {
        "status": "livekit_ready" if worker is not None and assistant_track_ready else "livekit_waiting",
        "detail": "LiveKit worker is connected." if worker is not None else "LiveKit worker is not connected to the room yet.",
        "livekit_url": settings.livekit_url,
        "livekit_room": settings.livekit_room,
        "livekit_worker_status": "connected" if worker is not None else "missing",
        "livekit_room_joined": room_exists,
        "livekit_output_track_status": "ready" if assistant_track_ready else "missing",
        "livekit_participants": len(participants),
    }


def compose_ready_payload(
    *,
    qwen_result: dict[str, str],
    livekit_result: dict[str, str | bool | int],
) -> dict[str, str | bool | int]:
    if qwen_result["status"] != "qwen_ready":
        return {
            **qwen_result,
            **livekit_result,
            "status": "warming" if qwen_result["status"] == "qwen_loading" else "failed",
            "detail": qwen_result["detail"],
        }

    if livekit_result["status"] != "livekit_ready":
        return {
            **qwen_result,
            **livekit_result,
            "status": "degraded",
            "detail": str(livekit_result["detail"]),
        }

    return {
        **qwen_result,
        **livekit_result,
        "status": "ready",
        "detail": "Qwen and LiveKit worker probes succeeded.",
    }
