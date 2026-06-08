import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_SYSTEM_PROMPT = (
    "You are a natural voice assistant. "
    "You are not human and you have no gender or age, but speak like a real helpful person. "
    "Do not mention model names, Alibaba, or Qwen unless the user directly asks. "
    "When the user says 'I/me/my/we/our', they mean themselves. "
    "When the user says 'you/your', they mean you. "
    "Use 'I/me/my' for yourself and 'you/your' for the user. "
    "Keep replies short, natural, and direct, usually under 50 words. "
    "Answer the main point first. "
    "Use the same language as the user unless asked otherwise. "
    "Output only spoken content. "
    "Do not use bullet points, markdown, emojis, stage directions, or emotion labels. "
    "If unclear, ask one short natural question."
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
        default=48000,
        alias="LIVEKIT_INPUT_SAMPLE_RATE",
    )
    livekit_input_vad_enabled: bool = Field(
        default=True,
        alias="LIVEKIT_INPUT_VAD_ENABLED",
    )
    livekit_input_vad_min_speech_duration: float = Field(
        default=0.05,
        alias="LIVEKIT_INPUT_VAD_MIN_SPEECH_DURATION",
    )
    livekit_input_vad_min_silence_duration: float = Field(
        default=0.35,
        alias="LIVEKIT_INPUT_VAD_MIN_SILENCE_DURATION",
    )
    livekit_input_vad_prefix_padding_duration: float = Field(
        default=0.15,
        alias="LIVEKIT_INPUT_VAD_PREFIX_PADDING_DURATION",
    )
    livekit_input_vad_activation_threshold: float = Field(
        default=0.5,
        alias="LIVEKIT_INPUT_VAD_ACTIVATION_THRESHOLD",
    )
    livekit_input_vad_force_cpu: bool = Field(
        default=True,
        alias="LIVEKIT_INPUT_VAD_FORCE_CPU",
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
        default=40,
        alias="LIVEKIT_OUTPUT_QUEUE_MS",
    )
    livekit_preroll_frames: int = Field(
        default=1,
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
    qwen_audio_backend: Literal["chat_stream", "realtime"] = Field(
        default="realtime",
        alias="QWEN_AUDIO_BACKEND",
    )
    qwen_input_sample_rate: int = Field(
        default=16000,
        alias="QWEN_AUDIO_INPUT_SAMPLE_RATE",
    )
    qwen_output_sample_rate: int = Field(
        default=24000,
        alias="QWEN_AUDIO_OUTPUT_SAMPLE_RATE",
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

    @model_validator(mode="after")
    def _validate_audio_rate_separation(self) -> "Settings":
        expected_rates = {
            "LIVEKIT_INPUT_SAMPLE_RATE": (self.livekit_input_sample_rate, 48000),
            "LIVEKIT_OUTPUT_SAMPLE_RATE": (self.livekit_output_sample_rate, 48000),
            "QWEN_AUDIO_INPUT_SAMPLE_RATE": (self.qwen_input_sample_rate, 16000),
            "QWEN_AUDIO_OUTPUT_SAMPLE_RATE": (self.qwen_output_sample_rate, 24000),
        }
        for env_name, (value, expected) in expected_rates.items():
            if value != expected:
                raise ValueError(f"{env_name} must be {expected}; got {value}.")
        return self

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
