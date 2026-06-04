import base64
import io
import wave

import httpx


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
        wav_base64 = base64.b64encode(_pcm16_to_wav(audio_pcm16, sample_rate=sample_rate)).decode("ascii")
        payload = {
            "model": self.model,
            "modalities": ["text"],
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
        }

        response = await self._get_client().post(self.url, json=payload)
        response.raise_for_status()
        body = response.json()
        return _extract_text(body)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.request_timeout_seconds)
        return self._client


def _pcm16_to_wav(audio_pcm16: bytes, *, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_pcm16)
    return buffer.getvalue()


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
