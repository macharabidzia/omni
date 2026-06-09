from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.config import get_settings
from src.livekit.auth import build_browser_identity, build_browser_token
from src.livekit.worker_state import load_livekit_worker_state

router = APIRouter(prefix="/livekit")


@router.post("/session")
async def create_livekit_session() -> dict[str, str]:
    settings = get_settings()
    if not settings.livekit_url or not settings.livekit_api_key or not settings.livekit_api_secret:
        raise HTTPException(
            status_code=503,
            detail="LiveKit control plane is not configured.",
        )
    worker_snapshot = load_livekit_worker_state(settings.livekit_worker_state_path)
    if (
        isinstance(worker_snapshot, dict)
        and worker_snapshot.get("livekit_worker_status") == "draining"
    ):
        raise HTTPException(
            status_code=503,
            detail="LiveKit worker is draining and not accepting new sessions.",
        )
    identity = build_browser_identity(settings)
    token = build_browser_token(settings, identity=identity)
    return {
        "livekit_url": settings.livekit_url,
        "room": settings.livekit_room,
        "participant_identity": identity,
        "participant_token": token,
        "control_topic": settings.livekit_control_topic,
    }
