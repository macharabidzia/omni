from http import HTTPStatus
import asyncio
import base64
from functools import lru_cache
import math
import logging
import time
from pathlib import Path
import wave

import httpx
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from livekit import api as livekit_api

from src.config import Settings, get_settings
from src.livekit.worker_state import load_livekit_worker_state
from src.realtime.audio import iter_pcm16_chunks
from src.realtime.qwen_chat_client import QwenChatClient
from src.realtime.qwen_client import QwenRealtimeClient, probe_qwen_realtime_websocket
from src.security import redact_url_secrets

router = APIRouter()
logger = logging.getLogger(__name__)
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


async def _ready_response(*, deep: bool) -> JSONResponse:
    settings = get_settings()
    qwen_result = await probe_qwen(settings, deep=deep)
    livekit_result = await probe_livekit(settings)
    result = compose_ready_payload(
        qwen_result=qwen_result,
        livekit_result=livekit_result,
        settings=settings,
    )
    status_code = HTTPStatus.OK if result["status"] == "ready" else HTTPStatus.SERVICE_UNAVAILABLE
    return JSONResponse(status_code=status_code, content=result)


@router.get("/ready")
async def ready(deep: bool = Query(default=False)) -> JSONResponse:
    return await _ready_response(deep=deep)


@router.get("/diagnostics/deep")
async def diagnostics_deep() -> JSONResponse:
    return await _ready_response(deep=True)


async def prewarm_qwen_startup(settings: Settings) -> None:
    if not settings.qwen_prewarm_on_startup:
        return

    deadline = time.monotonic() + settings.qwen_prewarm_max_wait_seconds
    attempt = 0
    last_status = "qwen_failed"
    last_detail = "prewarm was not attempted"

    while True:
        attempt += 1
        result = await probe_qwen(settings, deep=True)
        last_status = str(result.get("status", "qwen_failed"))
        last_detail = str(result.get("detail", "unknown prewarm failure"))
        if last_status == "qwen_ready":
            logger.info(
                "Qwen startup prewarm succeeded attempt=%d backend=%s qwen_health_url=%s qwen_realtime_url=%s",
                attempt,
                settings.qwen_audio_backend,
                redact_url_secrets(settings.qwen_health_url),
                redact_url_secrets(settings.qwen_realtime_url),
            )
            return

        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            break

        retry_delay_seconds = min(
            settings.qwen_prewarm_retry_interval_seconds,
            remaining_seconds,
        )
        logger.info(
            "Qwen startup prewarm retrying attempt=%d status=%s retry_in=%.2fs detail=%s",
            attempt,
            last_status,
            retry_delay_seconds,
            last_detail,
        )
        await asyncio.sleep(retry_delay_seconds)

    logger.warning(
        "Qwen startup prewarm exhausted attempt=%d status=%s detail=%s qwen_health_url=%s qwen_realtime_url=%s",
        attempt,
        last_status,
        last_detail,
        redact_url_secrets(settings.qwen_health_url),
        redact_url_secrets(settings.qwen_realtime_url),
    )


async def probe_qwen(settings: Settings, *, deep: bool = False) -> dict[str, str]:
    try:
        async with httpx.AsyncClient(timeout=settings.qwen_request_timeout_seconds) as client:
            response = await client.get(settings.qwen_health_url)
    except Exception as exc:
        return {
            "status": "qwen_unreachable",
            "detail": f"Health probe failed: {exc}",
            "qwen_health_url": redact_url_secrets(settings.qwen_health_url),
            "qwen_realtime_url": redact_url_secrets(settings.qwen_realtime_url),
        }

    if response.status_code >= 500:
        return {
            "status": "qwen_loading",
            "detail": f"Health endpoint returned {response.status_code}",
            "qwen_health_url": redact_url_secrets(settings.qwen_health_url),
            "qwen_realtime_url": redact_url_secrets(settings.qwen_realtime_url),
        }

    if settings.qwen_audio_backend == "realtime":
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
                "qwen_health_url": redact_url_secrets(settings.qwen_health_url),
                "qwen_realtime_url": redact_url_secrets(settings.qwen_realtime_url),
                "qwen_chat_url": redact_url_secrets(settings.qwen_chat_url),
            }

    if deep:
        inference_probe_result = await probe_qwen_realtime_inference_cached(settings)
        if inference_probe_result is not None:
            return inference_probe_result

    return {
        "status": "qwen_ready",
        "detail": (
            (
                "Qwen health, realtime session, and inference probes succeeded."
                if settings.qwen_audio_backend == "realtime"
                else "Qwen health and streaming chat audio inference probes succeeded."
            )
            if deep
            else (
                "Qwen health and realtime session probes succeeded."
                if settings.qwen_audio_backend == "realtime"
                else "Qwen health probe succeeded."
            )
        ),
        "qwen_health_url": redact_url_secrets(settings.qwen_health_url),
        "qwen_realtime_url": redact_url_secrets(settings.qwen_realtime_url),
        "qwen_chat_url": redact_url_secrets(settings.qwen_chat_url),
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
        if settings.qwen_audio_backend == "realtime":
            await probe_qwen_realtime_inference(settings)
        else:
            await probe_qwen_chat_stream_inference(settings)
    except Exception as exc:
        return {
            "status": "qwen_failed",
            "detail": (
                f"Realtime inference probe failed: {exc}"
                if settings.qwen_audio_backend == "realtime"
                else f"Streaming chat audio inference probe failed: {exc}"
            ),
            "qwen_health_url": redact_url_secrets(settings.qwen_health_url),
            "qwen_realtime_url": redact_url_secrets(settings.qwen_realtime_url),
            "qwen_chat_url": redact_url_secrets(settings.qwen_chat_url),
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
        output_sample_rate=settings.qwen_output_sample_rate,
        max_ws_message_bytes=settings.max_ws_message_bytes,
        debug_raw_events=settings.qwen_debug_raw_events,
        instructions="Reply briefly.",
    )
    try:
        await client.connect()
        await client.start_session(
            speaker=settings.supported_speakers[0],
            modalities=["text", "audio"],
            input_sample_rate=settings.qwen_input_sample_rate,
            output_audio=True,
        )
        probe_audio = _load_probe_audio_pcm16(sample_rate=settings.qwen_input_sample_rate)
        for chunk in iter_pcm16_chunks(
            probe_audio,
            duration_ms=settings.default_smoke_chunk_ms,
            sample_rate=settings.qwen_input_sample_rate,
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


async def probe_qwen_chat_stream_inference(settings: Settings) -> None:
    client = QwenChatClient(
        model=settings.qwen_model,
        url=settings.qwen_chat_url,
        request_timeout_seconds=min(
            settings.qwen_response_timeout_seconds,
            _QWEN_INFERENCE_PROBE_TIMEOUT_SECONDS,
        ),
        system_prompt=settings.default_system_prompt,
        max_completion_tokens=settings.text_max_completion_tokens,
    )
    try:
        probe_audio = _load_probe_audio_pcm16(sample_rate=settings.qwen_input_sample_rate)
        async for event in client.stream_audio_response(
            audio_pcm16=probe_audio,
            sample_rate=settings.qwen_input_sample_rate,
            speaker=settings.supported_speakers[0],
        ):
            if event.kind == "assistant_audio_delta":
                return
    finally:
        await client.close()

    raise RuntimeError("No assistant audio was received from the streaming chat probe.")


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


def _load_livekit_worker_snapshot(settings: Settings) -> dict[str, object] | None:
    return load_livekit_worker_state(settings.livekit_worker_state_path)


def _merge_livekit_worker_snapshot(
    result: dict[str, object],
    worker_snapshot: dict[str, object] | None,
) -> dict[str, object]:
    if not worker_snapshot:
        return result
    merged = dict(result)
    merged["livekit_reconnects"] = worker_snapshot.get("livekit_reconnects", 0)
    merged["qwen_reconnects"] = worker_snapshot.get("qwen_reconnects", 0)
    merged["active_sessions"] = worker_snapshot.get("active_sessions", 0)
    merged["livekit_input_track_status"] = worker_snapshot.get("livekit_input_track_status", "idle")
    merged["assistant_queue_depth_ms"] = worker_snapshot.get("assistant_queue_depth_ms", 0.0)
    merged["qwen_last_reconnect_reason"] = worker_snapshot.get("qwen_last_reconnect_reason")
    merged["error_count"] = worker_snapshot.get("error_count", 0)
    merged["interruption_count"] = worker_snapshot.get("interruption_count", 0)
    merged["duplicate_commit_count"] = worker_snapshot.get("duplicate_commit_count", 0)
    merged["stale_output_drop_count"] = worker_snapshot.get("stale_output_drop_count", 0)
    reconnect_reasons = worker_snapshot.get("qwen_reconnect_reasons")
    if isinstance(reconnect_reasons, dict):
        merged["qwen_reconnect_reasons"] = reconnect_reasons
    if "interrupt_clear_ms" in worker_snapshot:
        merged["interrupt_clear_ms"] = worker_snapshot["interrupt_clear_ms"]
    rollups = worker_snapshot.get("metric_rollups")
    if isinstance(rollups, dict):
        merged["metric_rollups"] = rollups
        for metric_key, ready_key in (
            ("commit_to_first_livekit_egress_last_ms", "last_first_audio_ms"),
            ("commit_to_first_livekit_egress_p50_ms", "p50_first_audio_ms"),
            ("commit_to_first_livekit_egress_p95_ms", "p95_first_audio_ms"),
            ("commit_to_first_livekit_egress_p99_ms", "p99_first_audio_ms"),
        ):
            value = rollups.get(metric_key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                merged[ready_key] = value
    updated_at_epoch_ms = worker_snapshot.get("updated_at_epoch_ms")
    if isinstance(updated_at_epoch_ms, (int, float)) and not isinstance(updated_at_epoch_ms, bool):
        merged["livekit_worker_snapshot_age_ms"] = max(
            int(time.time() * 1000) - int(updated_at_epoch_ms),
            0,
        )
    return merged


async def probe_livekit(settings: Settings) -> dict[str, object]:
    worker_snapshot = _load_livekit_worker_snapshot(settings)
    if not settings.livekit_url:
        return _merge_livekit_worker_snapshot(
            {
            "status": "livekit_failed",
            "detail": "LIVEKIT_URL is not configured.",
            "livekit_url": redact_url_secrets(settings.livekit_url),
            "livekit_room": settings.livekit_room,
            "livekit_worker_status": "not_configured",
            "livekit_room_joined": False,
            "livekit_input_track_status": "idle",
            "livekit_output_track_status": "unknown",
            },
            worker_snapshot,
        )

    if not settings.livekit_api_key or not settings.livekit_api_secret:
        return _merge_livekit_worker_snapshot(
            {
            "status": "livekit_failed",
            "detail": "LIVEKIT_API_KEY or LIVEKIT_API_SECRET is missing.",
            "livekit_url": redact_url_secrets(settings.livekit_url),
            "livekit_room": settings.livekit_room,
            "livekit_worker_status": "not_configured",
            "livekit_room_joined": False,
            "livekit_input_track_status": "idle",
            "livekit_output_track_status": "unknown",
            },
            worker_snapshot,
        )

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
        return _merge_livekit_worker_snapshot(
            {
            "status": "livekit_failed",
            "detail": f"LiveKit API probe failed: {exc}",
            "livekit_url": redact_url_secrets(settings.livekit_url),
            "livekit_room": settings.livekit_room,
            "livekit_worker_status": "unreachable",
            "livekit_room_joined": False,
            "livekit_input_track_status": "idle",
            "livekit_output_track_status": "unknown",
            },
            worker_snapshot,
        )
    finally:
        await client.aclose()

    worker = next(
        (participant for participant in participants if participant.identity == settings.livekit_agent_id),
        None,
    )
    assistant_track_ready = False
    if worker is not None:
        assistant_track_ready = any(track.name == "assistant" for track in worker.tracks)

    return _merge_livekit_worker_snapshot(
        {
        "status": "livekit_ready" if worker is not None and assistant_track_ready else "livekit_waiting",
        "detail": "LiveKit worker is connected." if worker is not None else "LiveKit worker is not connected to the room yet.",
        "livekit_url": redact_url_secrets(settings.livekit_url),
        "livekit_room": settings.livekit_room,
        "livekit_worker_status": "connected" if worker is not None else "missing",
        "livekit_room_joined": room_exists,
        "livekit_input_track_status": "idle",
        "livekit_output_track_status": "ready" if assistant_track_ready else "missing",
        "livekit_participants": len(participants),
        },
        worker_snapshot,
    )


def compose_ready_payload(
    *,
    qwen_result: dict[str, str],
    livekit_result: dict[str, object],
    settings: Settings,
) -> dict[str, object]:
    model_server_url = (
        settings.qwen_realtime_url
        if settings.qwen_audio_backend == "realtime"
        else settings.qwen_chat_url
    )
    base_payload: dict[str, object] = {
        **qwen_result,
        **livekit_result,
        "kernel_status": "ready",
        "model_server_status": qwen_result["status"],
        "model_server_url": model_server_url,
        "model_name": settings.qwen_model,
        "model_output_mode": "speech",
        "qwen3_omni_ready": qwen_result["status"] == "qwen_ready",
        "speech_output_ready": (
            qwen_result["status"] == "qwen_ready"
            and livekit_result.get("livekit_output_track_status") == "ready"
        ),
    }
    if qwen_result["status"] != "qwen_ready":
        return {
            **base_payload,
            "status": "warming" if qwen_result["status"] == "qwen_loading" else "failed",
            "detail": qwen_result["detail"],
            "failure_reason": qwen_result["detail"],
        }

    if livekit_result.get("livekit_worker_status") == "draining":
        return {
            **base_payload,
            "status": "degraded",
            "detail": "LiveKit worker is draining and not accepting new sessions.",
            "failure_reason": "LiveKit worker is draining and not accepting new sessions.",
        }

    if livekit_result["status"] != "livekit_ready":
        return {
            **base_payload,
            "status": "degraded",
            "detail": str(livekit_result["detail"]),
            "failure_reason": str(livekit_result["detail"]),
        }

    return {
        **base_payload,
        "status": "ready",
        "detail": "Qwen and LiveKit worker probes succeeded.",
        "failure_reason": None,
    }
