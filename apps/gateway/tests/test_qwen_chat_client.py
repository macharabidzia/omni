import base64

from src.realtime.audio import pcm16_to_wav
from src.realtime.qwen_chat_client import _parse_stream_event


def test_parse_stream_event_maps_text_delta() -> None:
    event = _parse_stream_event(
        {
            "modality": "text",
            "choices": [
                {
                    "delta": {
                        "content": "hello",
                    }
                }
            ],
        }
    )

    assert event is not None
    assert event.kind == "assistant_text_delta"
    assert event.text == "hello"


def test_parse_stream_event_decodes_wav_audio_chunk() -> None:
    pcm16_bytes = b"\x01\x00" * 480
    wav_base64 = base64.b64encode(
        pcm16_to_wav(pcm16_bytes, sample_rate=24000)
    ).decode("ascii")

    event = _parse_stream_event(
        {
            "modality": "audio",
            "choices": [
                {
                    "delta": {
                        "content": wav_base64,
                    }
                }
            ],
        }
    )

    assert event is not None
    assert event.kind == "assistant_audio_delta"
    assert event.sample_rate == 24000
    assert base64.b64decode(event.audio_base64 or "") == pcm16_bytes
