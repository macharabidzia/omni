import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.browser_ws import router as websocket_router
from src.config import get_settings
from src.health import router as health_router

settings = get_settings()

logging.basicConfig(
    level=logging.DEBUG if settings.qwen_debug_raw_events else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(
    title="Qwen3-Omni Realtime Gateway",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(websocket_router)

