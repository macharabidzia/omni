from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_SYSTEM_PROMPT = (
    "You are Qwen-Omni, a smart voice assistant created by Alibaba Qwen. "
    "Use short conversational replies under 50 words. "
    "Reply in the same language as the user unless asked otherwise. "
    "Output only the spoken content."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    gateway_port: int = Field(default=8080, alias="GATEWAY_PORT")
    qwen_model: str = Field(
        default="Qwen/Qwen3-Omni-30B-A3B-Instruct",
        alias="QWEN_MODEL",
    )
    qwen_realtime_url: str = Field(
        default="ws://qwen3-omni:8091/v1/realtime",
        alias="QWEN_REALTIME_URL",
    )
    qwen_health_url: str = Field(
        default="http://qwen3-omni:8091/health",
        alias="QWEN_HEALTH_URL",
    )
    qwen_debug_raw_events: bool = Field(default=False, alias="QWEN_DEBUG_RAW_EVENTS")
    qwen_request_timeout_seconds: float = Field(
        default=30.0,
        alias="QWEN_REQUEST_TIMEOUT_SECONDS",
    )
    qwen_response_timeout_seconds: float = Field(
        default=90.0,
        alias="QWEN_RESPONSE_TIMEOUT_SECONDS",
    )
    input_sample_rate: int = 16000
    output_sample_rate: int = 24000
    allowed_chunk_ms: tuple[int, ...] = (20, 40, 80, 120, 200)
    default_browser_chunk_ms: int = 80
    default_smoke_chunk_ms: int = 200
    supported_speakers: tuple[str, ...] = ("Ethan", "Chelsie", "Aiden")
    default_modalities: tuple[str, ...] = ("text", "audio")
    max_ws_message_bytes: int = 4 * 1024 * 1024
    default_system_prompt: str = DEFAULT_SYSTEM_PROMPT


@lru_cache
def get_settings() -> Settings:
    return Settings()

