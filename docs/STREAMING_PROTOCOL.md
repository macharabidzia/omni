# Streaming Protocol

## Browser to Gateway

### `session.start`

```json
{
  "type": "session.start",
  "speaker": "Ethan",
  "modalities": ["text", "audio"],
  "input_sample_rate": 16000,
  "output_audio": true
}
```

### `audio.append`

```json
{
  "type": "audio.append",
  "audio_base64": "<base64 pcm16 chunk>",
  "sample_rate": 16000,
  "channels": 1,
  "format": "pcm16"
}
```

### `audio.commit`

```json
{
  "type": "audio.commit"
}
```

### `response.cancel`

```json
{
  "type": "response.cancel"
}
```

### `session.end`

```json
{
  "type": "session.end"
}
```

## Gateway to Browser

### `session.ready`

```json
{
  "type": "session.ready",
  "session_id": "<uuid>"
}
```

### `transcript.delta`

```json
{
  "type": "transcript.delta",
  "text": "partial user transcript"
}
```

### `assistant.text.delta`

```json
{
  "type": "assistant.text.delta",
  "text": "partial assistant text"
}
```

### `assistant.audio.delta`

```json
{
  "type": "assistant.audio.delta",
  "audio_base64": "<base64 pcm16 chunk>",
  "sample_rate": 24000,
  "channels": 1,
  "format": "pcm16"
}
```

### `assistant.done`

```json
{
  "type": "assistant.done"
}
```

### `metrics.update`

```json
{
  "type": "metrics.update",
  "metrics": {
    "commit_to_first_transcript_ms": 0,
    "commit_to_first_text_ms": 0,
    "commit_to_first_audio_delta_ms": 0,
    "commit_to_first_audio_played_ms": 0,
    "full_response_ms": 0
  }
}
```

### `error`

```json
{
  "type": "error",
  "code": "QWEN_UNAVAILABLE",
  "message": "Qwen3-Omni realtime server is not available."
}
```

## Qwen Mapping Boundary

Raw Qwen websocket event names are isolated in `apps/gateway/src/realtime/qwen_client.py`.

The frontend consumes only the normalized browser event contract above.

