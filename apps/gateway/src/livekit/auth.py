from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from livekit.api import AccessToken, VideoGrants

from src.config import Settings


def build_browser_identity(settings: Settings) -> str:
    return f"{settings.livekit_browser_identity_prefix}-{uuid4().hex[:12]}"


def build_browser_token(settings: Settings, *, identity: str) -> str:
    token = (
        AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_ttl(timedelta(seconds=settings.livekit_token_ttl_seconds))
        .with_grants(
            VideoGrants(
                room_join=True,
                room=settings.livekit_room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
    )
    return token.to_jwt()


def build_worker_token(settings: Settings) -> str:
    token = (
        AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(settings.livekit_agent_id)
        .with_name(settings.livekit_agent_id)
        .with_ttl(timedelta(seconds=settings.livekit_token_ttl_seconds))
        .with_grants(
            VideoGrants(
                room_join=True,
                room=settings.livekit_room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
    )
    return token.to_jwt()
