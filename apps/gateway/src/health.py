from http import HTTPStatus

import httpx
import websockets
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.config import Settings, get_settings

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "container_alive"}


@router.get("/ready")
async def ready() -> JSONResponse:
    settings = get_settings()
    result = await probe_qwen(settings)
    status_code = (
        HTTPStatus.OK
        if result["status"] == "qwen_ready"
        else HTTPStatus.SERVICE_UNAVAILABLE
    )
    return JSONResponse(status_code=status_code, content=result)


async def probe_qwen(settings: Settings) -> dict[str, str]:
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
        async with websockets.connect(
            settings.qwen_realtime_url,
            max_size=settings.max_ws_message_bytes,
            open_timeout=settings.qwen_request_timeout_seconds,
            close_timeout=2,
        ):
            pass
    except Exception as exc:
        return {
            "status": "qwen_failed",
            "detail": f"Realtime websocket probe failed: {exc}",
            "qwen_health_url": settings.qwen_health_url,
            "qwen_realtime_url": settings.qwen_realtime_url,
        }

    return {
        "status": "qwen_ready",
        "detail": "Qwen health and realtime websocket probes succeeded.",
        "qwen_health_url": settings.qwen_health_url,
        "qwen_realtime_url": settings.qwen_realtime_url,
    }

