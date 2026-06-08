import base64
import json
from collections.abc import AsyncIterator

import httpx

from src.realtime.audio import pcm16_to_wav, wav_to_pcm16
from src.realtime.qwen_client import QwenEvent


class QwenChatClient:
    def __init__(
        self,
        *,
        model: str,
        url: str,
        request_timeout_seconds: float,
        system_prompt: str,
        max_completion_tokens: int,
    ) -> None:
        self.model = model
        self.url = url
        self.request_timeout_seconds = request_timeout_seconds
        self.system_prompt = system_prompt
        self.max_completion_tokens = max_completion_tokens
        self._client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self._client is None:
            return
        await self._client.aclose()
        self._client = None

    async def respond_text_only(self, *, audio_pcm16: bytes, sample_rate: int) -> str:
        payload = self._build_audio_request_payload(
            audio_pcm16=audio_pcm16,
            sample_rate=sample_rate,
            modalities=["text"],
        )

        response = await self._get_client().post(self.url, json=payload)
        response.raise_for_status()
        body = response.json()
        return _extract_text(body)

    async def stream_audio_response(
        self,
        *,
        audio_pcm16: bytes,
        sample_rate: int,
        speaker: str,
    ) -> AsyncIterator[QwenEvent]:
        payload = self._build_audio_request_payload(
            audio_pcm16=audio_pcm16,
            sample_rate=sample_rate,
            modalities=["text", "audio"],
            speaker=speaker,
            stream=True,
        )

        async with self._get_client().stream("POST", self.url, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                payload = json.loads(data)
                event = _parse_stream_event(payload)
                if event is not None:
                    yield event

        yield QwenEvent(kind="response_done", payload={"type": "response.done"})

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.request_timeout_seconds)
        return self._client

    def _build_audio_request_payload(
        self,
        *,
        audio_pcm16: bytes,
        sample_rate: int,
        modalities: list[str],
        speaker: str | None = None,
        stream: bool = False,
    ) -> dict:
        wav_base64 = base64.b64encode(pcm16_to_wav(audio_pcm16, sample_rate=sample_rate)).decode("ascii")
        payload = {
            "model": self.model,
            "modalities": modalities,
            "max_completion_tokens": self.max_completion_tokens,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Answer the user's audio briefly."},
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": wav_base64,
                                "format": "wav",
                            },
                        },
                    ],
                },
            ],
            "stream": stream,
        }
        if speaker:
            payload["voice"] = speaker
        return payload


def _parse_stream_event(payload: dict) -> QwenEvent | None:
    modality = payload.get("modality")
    delta = _extract_stream_delta_content(payload)
    if not delta:
        return None

    if modality == "text":
        return QwenEvent(
            kind="assistant_text_delta",
            payload=payload,
            text=delta,
        )

    if modality == "audio":
        wav_bytes = base64.b64decode(delta)
        pcm16_bytes, sample_rate = wav_to_pcm16(wav_bytes)
        return QwenEvent(
            kind="assistant_audio_delta",
            payload=payload,
            audio_base64=base64.b64encode(pcm16_bytes).decode("ascii"),
            sample_rate=sample_rate,
        )

    return None


def _extract_stream_delta_content(payload: dict) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return None
    delta = first_choice.get("delta")
    if not isinstance(delta, dict):
        return None
    content = delta.get("content")
    if isinstance(content, str) and content:
        return content
    return None


def _extract_text(body: dict) -> str:
    texts: list[str] = []
    for choice in body.get("choices", []):
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            texts.append(content.strip())
            continue
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
    combined = "\n\n".join(texts).strip()
    if not combined:
        raise ValueError("Qwen chat completion returned no text content.")
    return combined
