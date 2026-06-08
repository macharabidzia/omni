import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_SYSTEM_PROMPT = (
    "You are Qwen-Omni, a smart voice assistant created by Alibaba Qwen. "
    "You are a virtual voice assistant with no gender or age. "
    "In user messages, 'I/me/my/we/our' refer to the user and 'you/your' refer to the assistant. "
    "In your replies, address the user as 'you/your' and yourself as 'I/me/my'; never mirror the user's pronouns. "
    "Use short, brief, straightforward replies under 50 words in a natural conversational tone. "
    "Output only the spoken content. "
    "Do not use bullet points, stage directions, action descriptions, emotion descriptions, or symbols that describe tone. "
    "Answer the user's audio or text question directly. "
    "Reply in the same language as the user unless asked otherwise. "
    "If you are uncertain or need clarification, ask a short follow-up question."
)


DEFAULT_ENV_FILE = os.environ.get(
    "OMNI_ENV_FILE",
    os.path.join(os.environ.get("WORKSPACE", "/workspace"), ".env"),
)
DEFAULT_WORKSPACE = os.environ.get("WORKSPACE", "/workspace")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    gateway_port: int = Field(default=8080, alias="GATEWAY_PORT")
    cors_allow_origins: tuple[str, ...] = Field(default=(), alias="CORS_ALLOW_ORIGINS")
    livekit_url: str = Field(default="", alias="LIVEKIT_URL")
    livekit_api_key: str = Field(default="", alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(default="", alias="LIVEKIT_API_SECRET")
    livekit_room: str = Field(default="omni-room", alias="LIVEKIT_ROOM")
    livekit_agent_id: str = Field(default="omni-worker", alias="LIVEKIT_AGENT_ID")
    livekit_browser_identity_prefix: str = Field(
        default="browser",
        alias="LIVEKIT_BROWSER_IDENTITY_PREFIX",
    )
    livekit_control_topic: str = Field(
        default="omni.control",
        alias="LIVEKIT_CONTROL_TOPIC",
    )
    livekit_token_ttl_seconds: int = Field(
        default=3600,
        alias="LIVEKIT_TOKEN_TTL_SECONDS",
    )
    livekit_input_sample_rate: int = Field(
        default=16000,
        alias="LIVEKIT_INPUT_SAMPLE_RATE",
    )
    livekit_output_sample_rate: int = Field(
        default=48000,
        alias="LIVEKIT_OUTPUT_SAMPLE_RATE",
    )
    livekit_output_frame_ms: int = Field(
        default=20,
        alias="LIVEKIT_OUTPUT_FRAME_MS",
    )
    livekit_output_queue_ms: int = Field(
        default=60,
        alias="LIVEKIT_OUTPUT_QUEUE_MS",
    )
    livekit_output_lowpass_hz: float = Field(
        default=6000.0,
        alias="LIVEKIT_OUTPUT_LOWPASS_HZ",
    )
    livekit_preroll_frames: int = Field(
        default=8,
        alias="LIVEKIT_PREROLL_FRAMES",
    )
    audio_artifact_log_path: str = Field(
        default=str(Path(DEFAULT_WORKSPACE) / "tmp" / "logs" / "audio-artifacts.log"),
        alias="AUDIO_ARTIFACT_LOG_PATH",
    )
    qwen_model: str = Field(
        default="Qwen/Qwen3-Omni-30B-A3B-Instruct",
        alias="QWEN_MODEL",
    )
    qwen_realtime_url: str = Field(
        default="ws://127.0.0.1:17091/v1/realtime",
        alias="QWEN_REALTIME_URL",
    )
    qwen_chat_url: str = Field(
        default="http://127.0.0.1:17091/v1/chat/completions",
        alias="QWEN_CHAT_URL",
    )
    qwen_health_url: str = Field(
        default="http://127.0.0.1:17091/health",
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
    allowed_chunk_ms: tuple[int, ...] = (10, 20, 40, 80, 120, 200)
    default_browser_chunk_ms: int = 20
    default_smoke_chunk_ms: int = 20
    supported_speakers: tuple[str, ...] = ("Ethan", "Chelsie", "Aiden")
    default_modalities: tuple[str, ...] = ("text", "audio")
    max_ws_message_bytes: int = 4 * 1024 * 1024
    default_system_prompt: str = DEFAULT_SYSTEM_PROMPT
    text_max_completion_tokens: int = Field(
        default=64,
        alias="QWEN_TEXT_MAX_COMPLETION_TOKENS",
    )

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _parse_cors_allow_origins(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return tuple(origin.strip() for origin in value.split(",") if origin.strip())
        if isinstance(value, (list, tuple)):
            return tuple(str(origin).strip() for origin in value if str(origin).strip())
        raise TypeError("CORS_ALLOW_ORIGINS must be a comma-separated string or list of origins.")


@lru_cache
def get_settings() -> Settings:
    return Settings()
