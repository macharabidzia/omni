import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import get_settings
from src.health import router as health_router
from src.livekit.control import router as livekit_control_router

settings = get_settings()

logging.basicConfig(
    level=logging.DEBUG if settings.qwen_debug_raw_events else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(
    title="Qwen3-Omni Realtime Control Plane",
    version="0.1.0",
)

if settings.cors_allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

app.include_router(health_router)
app.include_router(livekit_control_router)
