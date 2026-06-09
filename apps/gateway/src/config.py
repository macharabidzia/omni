import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
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
    gateway_environment: Literal["development", "production", "test"] = Field(
        default="development",
        alias="GATEWAY_ENV",
    )
    cors_allow_origins: tuple[str, ...] = Field(default=(), alias="CORS_ALLOW_ORIGINS")
    livekit_url: str = Field(default="", alias="LIVEKIT_URL")
    livekit_api_key: str = Field(default="", alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(default="", alias="LIVEKIT_API_SECRET")
    livekit_room: str = Field(default="omni-room", alias="LIVEKIT_ROOM")
    livekit_agent_id: str = Field(
        default="omni-worker",
        alias="LIVEKIT_AGENT_ID",
        validation_alias=AliasChoices("LIVEKIT_AGENT_ID", "LIVEKIT_AGENT_IDENTITY"),
    )
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
        validation_alias=AliasChoices("LIVEKIT_INPUT_SAMPLE_RATE", "LIVEKIT_INPUT_SAMPLE_RATE_HZ"),
    )
    livekit_input_vad_enabled: bool = Field(
        default=True,
        alias="LIVEKIT_INPUT_VAD_ENABLED",
        validation_alias=AliasChoices("LIVEKIT_INPUT_VAD_ENABLED", "VAD_ENABLED"),
    )
    livekit_input_vad_min_speech_duration: float = Field(
        default=0.08,
        alias="LIVEKIT_INPUT_VAD_MIN_SPEECH_DURATION",
        validation_alias=AliasChoices(
            "LIVEKIT_INPUT_VAD_MIN_SPEECH_DURATION",
            "VAD_MIN_SPEECH_MS",
        ),
    )
    livekit_input_vad_min_silence_duration: float = Field(
        default=0.15,
        alias="LIVEKIT_INPUT_VAD_MIN_SILENCE_DURATION",
        validation_alias=AliasChoices(
            "LIVEKIT_INPUT_VAD_MIN_SILENCE_DURATION",
            "VAD_MIN_SILENCE_MS",
        ),
    )
    livekit_input_vad_prefix_padding_duration: float = Field(
        default=0.2,
        alias="LIVEKIT_INPUT_VAD_PREFIX_PADDING_DURATION",
        validation_alias=AliasChoices(
            "LIVEKIT_INPUT_VAD_PREFIX_PADDING_DURATION",
            "VAD_PREROLL_MS",
        ),
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
        validation_alias=AliasChoices(
            "LIVEKIT_OUTPUT_SAMPLE_RATE",
            "LIVEKIT_PUBLISH_SAMPLE_RATE_HZ",
        ),
    )
    livekit_output_frame_ms: int = Field(
        default=10,
        alias="LIVEKIT_OUTPUT_FRAME_MS",
    )
    livekit_output_queue_ms: int = Field(
        default=150,
        alias="LIVEKIT_OUTPUT_QUEUE_MS",
        validation_alias=AliasChoices(
            "LIVEKIT_OUTPUT_QUEUE_MS",
            "LIVEKIT_AUDIO_SOURCE_QUEUE_MS",
        ),
    )
    livekit_preroll_frames: int = Field(
        default=1,
        alias="LIVEKIT_PREROLL_FRAMES",
    )
    livekit_max_turn_ms: int = Field(
        default=30_000,
        alias="LIVEKIT_MAX_TURN_MS",
    )
    livekit_qwen_input_chunk_ms: int = Field(
        default=80,
        alias="LIVEKIT_QWEN_INPUT_CHUNK_MS",
    )
    livekit_worker_state_path: str = Field(
        default=(Path(DEFAULT_WORKSPACE) / "tmp" / "state" / "livekit-worker-state.json").as_posix(),
        alias="LIVEKIT_WORKER_STATE_PATH",
    )
    audio_artifact_logging_enabled: bool = Field(
        default=False,
        alias="AUDIO_ARTIFACT_LOGGING_ENABLED",
        validation_alias=AliasChoices(
            "AUDIO_ARTIFACT_LOGGING_ENABLED",
            "DEBUG_AUDIO_ARTIFACTS",
        ),
    )
    audio_artifact_log_path: str = Field(
        default=(Path(DEFAULT_WORKSPACE) / "tmp" / "logs" / "audio-artifacts.log").as_posix(),
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
        validation_alias=AliasChoices(
            "QWEN_AUDIO_INPUT_SAMPLE_RATE",
            "QWEN_INPUT_SAMPLE_RATE_HZ",
        ),
    )
    qwen_output_sample_rate: int = Field(
        default=24000,
        alias="QWEN_AUDIO_OUTPUT_SAMPLE_RATE",
        validation_alias=AliasChoices(
            "QWEN_AUDIO_OUTPUT_SAMPLE_RATE",
            "QWEN_OUTPUT_SAMPLE_RATE_HZ",
        ),
    )
    qwen_health_url: str = Field(
        default="http://127.0.0.1:17091/health",
        alias="QWEN_HEALTH_URL",
    )
    qwen_debug_raw_events: bool = Field(
        default=False,
        alias="QWEN_DEBUG_RAW_EVENTS",
        validation_alias=AliasChoices("QWEN_DEBUG_RAW_EVENTS", "DEBUG_RAW_QWEN_EVENTS"),
    )
    qwen_request_timeout_seconds: float = Field(
        default=30.0,
        alias="QWEN_REQUEST_TIMEOUT_SECONDS",
    )
    qwen_response_timeout_seconds: float = Field(
        default=90.0,
        alias="QWEN_RESPONSE_TIMEOUT_SECONDS",
    )
    qwen_first_audio_timeout_seconds: float = Field(
        default=8.0,
        alias="QWEN_FIRST_AUDIO_TIMEOUT_SECONDS",
    )
    qwen_total_response_timeout_seconds: float = Field(
        default=45.0,
        alias="QWEN_TOTAL_RESPONSE_TIMEOUT_SECONDS",
    )
    qwen_prewarm_on_startup: bool = Field(
        default=False,
        alias="QWEN_PREWARM_ON_STARTUP",
    )
    qwen_prewarm_max_wait_seconds: float = Field(
        default=60.0,
        alias="QWEN_PREWARM_MAX_WAIT_SECONDS",
    )
    qwen_prewarm_retry_interval_seconds: float = Field(
        default=2.0,
        alias="QWEN_PREWARM_RETRY_INTERVAL_SECONDS",
    )
    livekit_drain_timeout_seconds: float = Field(
        default=15.0,
        alias="LIVEKIT_DRAIN_TIMEOUT_SECONDS",
    )
    livekit_browser_idle_timeout_seconds: float = Field(
        default=120.0,
        alias="LIVEKIT_BROWSER_IDLE_TIMEOUT_SECONDS",
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
        if self.livekit_output_frame_ms not in {10, 20}:
            raise ValueError(
                "LIVEKIT_OUTPUT_FRAME_MS must be 10 or 20 for low-latency LiveKit playout; "
                f"got {self.livekit_output_frame_ms}."
            )
        if self.livekit_output_queue_ms < self.livekit_output_frame_ms:
            raise ValueError(
                "LIVEKIT_OUTPUT_QUEUE_MS must be at least one output frame; "
                f"got queue={self.livekit_output_queue_ms} frame={self.livekit_output_frame_ms}."
            )
        if self.livekit_output_queue_ms > 600:
            raise ValueError(
                "LIVEKIT_OUTPUT_QUEUE_MS must be <= 600 to keep barge-in responsive; "
                f"got {self.livekit_output_queue_ms}."
            )
        if self.livekit_preroll_frames < 1:
            raise ValueError(
                f"LIVEKIT_PREROLL_FRAMES must be >= 1; got {self.livekit_preroll_frames}."
            )
        if self.livekit_max_turn_ms < 1_000:
            raise ValueError(
                "LIVEKIT_MAX_TURN_MS must be at least 1000 to avoid zero-length auto-commits; "
                f"got {self.livekit_max_turn_ms}."
            )
        if self.livekit_max_turn_ms > 30_000:
            raise ValueError(
                "LIVEKIT_MAX_TURN_MS must be <= 30000 to cap runaway turns; "
                f"got {self.livekit_max_turn_ms}."
            )
        if self.livekit_qwen_input_chunk_ms not in {40, 80}:
            raise ValueError(
                "LIVEKIT_QWEN_INPUT_CHUNK_MS must be 40 or 80 for the supported "
                f"Qwen input chunk policy; got {self.livekit_qwen_input_chunk_ms}."
            )
        if self.qwen_first_audio_timeout_seconds <= 0:
            raise ValueError("QWEN_FIRST_AUDIO_TIMEOUT_SECONDS must be > 0.")
        if self.qwen_total_response_timeout_seconds <= 0:
            raise ValueError("QWEN_TOTAL_RESPONSE_TIMEOUT_SECONDS must be > 0.")
        if self.qwen_total_response_timeout_seconds < self.qwen_first_audio_timeout_seconds:
            raise ValueError(
                "QWEN_TOTAL_RESPONSE_TIMEOUT_SECONDS must be >= "
                "QWEN_FIRST_AUDIO_TIMEOUT_SECONDS."
            )
        if self.qwen_prewarm_max_wait_seconds <= 0:
            raise ValueError("QWEN_PREWARM_MAX_WAIT_SECONDS must be > 0.")
        if self.qwen_prewarm_retry_interval_seconds <= 0:
            raise ValueError("QWEN_PREWARM_RETRY_INTERVAL_SECONDS must be > 0.")
        if self.livekit_drain_timeout_seconds <= 0:
            raise ValueError("LIVEKIT_DRAIN_TIMEOUT_SECONDS must be > 0.")
        if self.livekit_browser_idle_timeout_seconds <= 0:
            raise ValueError("LIVEKIT_BROWSER_IDLE_TIMEOUT_SECONDS must be > 0.")
        if self.gateway_environment == "production" and "*" in self.cors_allow_origins:
            raise ValueError(
                "CORS_ALLOW_ORIGINS cannot contain '*' when GATEWAY_ENV=production."
            )
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

    @field_validator(
        "livekit_input_vad_min_speech_duration",
        "livekit_input_vad_min_silence_duration",
        "livekit_input_vad_prefix_padding_duration",
        mode="before",
    )
    @classmethod
    def _coerce_vad_duration_alias_ms(cls, value: object) -> object:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            numeric_value = float(value)
            return numeric_value / 1000 if numeric_value > 10 else numeric_value
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return value
            numeric_value = float(stripped)
            return numeric_value / 1000 if numeric_value > 10 else numeric_value
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
