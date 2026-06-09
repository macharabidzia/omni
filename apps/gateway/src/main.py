import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import get_settings
from src.health import prewarm_qwen_startup, router as health_router
from src.livekit.control import router as livekit_control_router

settings = get_settings()

logging.basicConfig(
    level=logging.DEBUG if settings.qwen_debug_raw_events else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    prewarm_task = None
    current_settings = get_settings()
    if current_settings.qwen_prewarm_on_startup:
        prewarm_task = asyncio.create_task(
            prewarm_qwen_startup(current_settings),
            name="qwen-startup-prewarm",
        )
    app.state.qwen_prewarm_task = prewarm_task
    try:
        yield
    finally:
        if prewarm_task is not None and not prewarm_task.done():
            prewarm_task.cancel()
            with suppress(asyncio.CancelledError):
                await prewarm_task
        app.state.qwen_prewarm_task = None


app = FastAPI(
    title="Qwen3-Omni Realtime Control Plane",
    version="0.1.0",
    lifespan=app_lifespan,
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
