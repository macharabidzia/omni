import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.config import get_settings
from src.realtime.session import RealtimeSession

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/realtime")
async def realtime_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    session = RealtimeSession(websocket=websocket, settings=get_settings())

    try:
        while True:
            raw_message = await websocket.receive_text()
            try:
                event = json.loads(raw_message)
            except json.JSONDecodeError:
                await session.send_error(
                    code="INVALID_JSON",
                    message="Browser message is not valid JSON.",
                )
                continue

            if not isinstance(event, dict):
                await session.send_error(
                    code="INVALID_EVENT",
                    message="Browser websocket messages must be JSON objects.",
                )
                continue

            should_close = await session.handle_browser_event(event)
            if should_close:
                break
    except WebSocketDisconnect:
        logger.info("Browser disconnected: %s", session.session_id)
    except Exception:
        logger.exception("Unhandled browser websocket failure for session %s", session.session_id)
        await session.send_error(
            code="INTERNAL_ERROR",
            message="Unhandled gateway failure.",
        )
    finally:
        await session.close()

