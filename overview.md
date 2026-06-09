# Python Overview For `apps/`

Scope: every `*.py` file under `apps/` as of 2026-06-09.

## `apps/__init__.py`
Purpose: Package marker for the top-level `apps` package.

Classes
- None.

Functions
- None.

## `apps/gateway/__init__.py`
Purpose: Package marker for the `gateway` app package.

Classes
- None.

Functions
- None.

## `apps/gateway/src/__init__.py`
Purpose: Package marker for the gateway source package.

Classes
- None.

Functions
- None.

## `apps/gateway/src/config.py`
Purpose: Defines environment-backed settings shared by the API server, LiveKit worker, and Qwen clients.

Classes
- `Settings`: Pydantic settings model that loads config from env and locks the audio pipeline to the supported sample rates.
  - `_validate_audio_rate_separation()`: Rejects unsupported sample-rate overrides and unsafe LiveKit frame/queue settings so browser playout stays low-latency.
  - `_parse_cors_allow_origins(value)`: Normalizes CORS origins from a comma-separated string or list into a tuple.
  - `_coerce_vad_duration_alias_ms(value)`: Accepts legacy VAD duration env values in milliseconds or seconds and normalizes them to seconds before validation.

Functions
- `get_settings()`: Returns a cached `Settings` instance so configuration is parsed once per process.

## `apps/gateway/src/health.py`
Purpose: Implements liveness and readiness endpoints plus deep probes for Qwen and LiveKit.

Functions
- `health()`: Returns a simple container liveness payload.
- `_ready_response(deep)`: Builds the shared shallow/deep readiness payload for Qwen and LiveKit.
- `ready(deep=False)`: Runs shallow readiness by default and only performs expensive inference probing when explicitly requested.
- `diagnostics_deep()`: Runs the deep readiness path as an operator-triggered diagnostic route.
- `prewarm_qwen_startup(settings)`: Retries deep Qwen readiness probes during process startup until the model is ready or the configured prewarm window expires.
- `probe_qwen(settings, deep=False)`: Checks the Qwen health endpoint, optionally verifies realtime websocket startup, and optionally runs an inference probe.
- `probe_qwen_realtime_inference_cached(settings)`: Skips repeated deep inference probes for a short TTL after a recent success.
- `probe_qwen_realtime_inference(settings)`: Pushes probe audio through the realtime websocket API and waits for assistant audio to confirm end-to-end audio generation.
- `probe_qwen_chat_stream_inference(settings)`: Sends probe audio through the streaming chat-completions API and waits for assistant audio output.
- `_build_probe_audio_pcm16(...)`: Synthesizes a mono PCM16 sine-wave probe signal.
- `_load_probe_audio_pcm16(sample_rate)`: Loads a cached WAV probe clip when available, otherwise generates synthetic probe audio.
- `_load_livekit_worker_snapshot(settings)`: Loads the persisted LiveKit worker-state snapshot when one is available on disk.
- `_merge_livekit_worker_snapshot(result, worker_snapshot)`: Enriches readiness output with persisted worker counters, reconnect stats, rollups, and snapshot age.
- `probe_livekit(settings)`: Verifies LiveKit configuration, room visibility, worker presence, and assistant-track publication.
- `compose_ready_payload(qwen_result, livekit_result)`: Merges probe results into a final `ready`, `degraded`, `warming`, or `failed` payload.

## `apps/gateway/src/livekit/__init__.py`
Purpose: Package marker for LiveKit-specific gateway code.

Classes
- None.

Functions
- None.

## `apps/gateway/src/livekit/audio_policy.py`
Purpose: Defines shared assistant-audio thresholds used by runtime smoothing and capture-analysis tooling so pause/click policy does not drift.

Classes
- None.

Functions
- None.

## `apps/gateway/src/livekit/auth.py`
Purpose: Builds LiveKit participant identities and access tokens.

Functions
- `build_browser_identity(settings)`: Creates a unique browser participant identity using the configured prefix.
- `build_browser_token(settings, identity)`: Builds a browser JWT with room join, publish, subscribe, and data permissions.
- `build_worker_token(settings)`: Builds the worker JWT using the configured agent identity and room grants.

## `apps/gateway/src/livekit/control.py`
Purpose: Exposes the API route used by browsers to obtain a LiveKit session token.

Functions
- `create_livekit_session()`: Validates LiveKit config, creates a browser identity and token, and returns room/session connection details.

## `apps/gateway/src/livekit/output.py`
Purpose: Converts model audio chunks into smooth LiveKit audio frames with silence trimming, resampling, and artifact cleanup.

Classes
- `AssistantAudioPublisher`: Buffers assistant PCM16 audio, normalizes it for transport, and publishes complete LiveKit frames.
  - `__init__(...)`: Stores output settings, computes frame and trimming windows, and initializes per-turn audio state.
  - `set_log_context(...)`: Stores session, participant, and turn identifiers so publisher warnings and stats include request lineage.
  - `enqueue_base64(audio_base64, input_sample_rate)`: Decodes one audio chunk, resamples it, trims/smooths it, buffers it, flushes complete frames, and applies queue backpressure safeguards.
  - `finalize_turn()`: Flushes resampler tail data, trims final trailing silence, pads the last partial frame, publishes the rest, logs stats, and resets turn state.
  - `clear()`: Drops queued audio, resets resampler and smoothing state, and clears the LiveKit source queue.
  - `wait_for_playout()`: Waits until the underlying LiveKit source finishes playback.
  - `queued_duration_seconds()`: Returns the amount of audio still queued for playout.
  - `_convert_input_rate(pcm16_bytes, input_sample_rate)`: Resamples chunks to the configured transport rate and forbids rate changes mid-turn.
  - `_flush_resampler()`: Drains any buffered resampler output and resets the resampler.
  - `_pad_tail_with_fadeout()`: Extends a short final chunk to a full frame using a decaying copy of the last sample.
  - `_frames_to_bytes(frames)`: Flattens LiveKit `AudioFrame` objects into one PCM byte stream.
  - `_normalize_chunk_silence(pcm16_bytes)`: Trims leading/inter-chunk artifacts while preserving short internal pauses after speech has started.
  - `_trim_turn_leading_silence(pcm16_bytes)`: Removes silence from the start of a turn and applies a short fade-in to the first voiced samples.
  - `_apply_fade_in(pcm16_bytes)`: Ramps the start of a chunk upward to reduce clicks after silence trimming.
  - `_smooth_artifacts(pcm16_bytes)`: Applies boundary smoothing and isolated-spike smoothing to one chunk.
  - `_smooth_chunk_boundary_inplace(samples)`: Softens large jumps between consecutive chunks when smoothing actually improves the transition.
  - `_smooth_isolated_spikes_inplace(samples)`: Replaces single-sample click spikes with the average of their neighbors.
  - `_warn_if_output_queue_backed_up()`: Logs a once-per-turn warning when the LiveKit playout queue crosses the soft backpressure threshold.
  - `_should_drop_backpressure_frame()`: Decides whether another frame should be dropped because the queue already hit its hard cap.
  - `_record_backpressure_frame_drop()`: Tracks dropped frames and logs the first hard-cap backpressure drop for the turn.
  - `_reset_turn_boundary_state()`: Clears per-turn counters, silence-trim state, and smoothing statistics.
  - `_samples_to_ms(sample_count)`: Converts sample counts into rounded milliseconds for per-turn logging.
  - `_count_leading_silence_samples(pcm16_bytes, max_trim_samples)`: Counts how much silence appears at the front of a chunk.
  - `_trim_interchunk_trailing_silence(pcm16_bytes)`: Removes trailing silence from a chunk when it is quiet enough to be safe to trim.
  - `_trim_final_trailing_silence()`: Trims quiet tail silence at turn end while preserving a short natural release.
  - `_chunk_peak_abs(pcm16_bytes)`: Returns the peak absolute amplitude of a chunk.
  - `_count_trailing_silence_samples(pcm16_bytes, max_trim_samples)`: Counts how much silence appears at the end of a chunk.
  - `_rms(pcm16_bytes, start_sample, sample_count)`: Computes RMS energy for a region of PCM16 data.
  - `_boundary_transition_peak_abs(samples, previous_sample, transition_samples)`: Measures the largest sample jump across a boundary transition.
  - `_peak_abs(pcm16_bytes, start_sample, sample_count)`: Returns peak absolute amplitude for a region of PCM16 data.
  - `_percentile(values, pct)`: Returns a simple percentile used for boundary-jump logging.
  - `_flush_complete_frames()`: Sends complete output frames to the optional sink and LiveKit source, dropping frames when the hard queue cap is already exceeded.

## `apps/gateway/src/livekit/worker.py`
Purpose: Runs the LiveKit worker and bridges browser speech events, VAD, Qwen sessions, and assistant audio playout.

Functions
- `build_input_vad(settings)`: Creates a Silero VAD instance from settings or disables VAD when configured off.
- `build_assistant_track_publish_options()`: Builds LiveKit track publish options tuned for assistant audio transport.
- `_run_worker()`: Loads settings, runs the worker, and guarantees shutdown cleanup.
- `configure_worker_logging(settings)`: Configures process logging and optionally attaches the audio-artifact file handler used by worker and output logs.
- `main()`: Configures logging and artifact logs, then starts the async worker entrypoint.
- `_install_worker_signal_handlers(worker)`: Installs SIGINT and SIGTERM handlers that switch the worker into drain mode instead of exiting mid-session.
- `_coerce_optional_float(value)`: Safely parses optional numeric telemetry fields from ints, floats, or strings.

Classes
- `TurnLifecycleState`: Enum describing the allowed participant turn states from idle through closed.
- `MetricRollupWindow`: Rolling worker-side percentile tracker used to expose last/p50/p95/p99 values for numeric metrics across recent turns.
  - `__init__(max_samples=...)`: Creates bounded per-metric sample windows for recent-turn rollups.
  - `record_metrics(metrics, last_seen)`: Appends changed numeric metrics into per-metric sample windows while ignoring repeated values.
  - `snapshot()`: Produces last/p50/p95/p99/count rollups for each tracked metric.
  - `_split_metric_name(name)`: Splits metric names into a base name plus optional `_ms` suffix so rollup keys stay readable.
  - `_percentile(values, pct)`: Calculates a rounded percentile for a metric sample window.
- `ParticipantBridgeSession`: Manages one browser participant's audio/control lifecycle and forwards it to a `RealtimeSession`.
  - `__init__(...)`: Initializes participant state, preroll and input buffers, optional VAD, worker-state reporting, and the realtime session bridge.
  - `handle_control_message(payload)`: Handles session start, speech control, telemetry, interrupts, and close packets while rejecting manual turn control when VAD owns boundaries.
  - `bind_audio_track(track)`: Replaces the active LiveKit audio-consumer task for the participant's subscribed track.
  - `_set_turn_state(new_state, reason)`: Enforces the allowed turn-lifecycle transitions and ignores invalid duplicates.
  - `_append_input_audio(audio_bytes, sample_rate, channels=1)`: Buffers and sends user PCM into the active turn while tracking uploaded duration.
  - `_send_input_audio_chunk(audio_bytes, sample_rate, channels)`: Base64-encodes one PCM chunk and sends it into the bridged realtime session.
  - `_bytes_for_duration_ms(sample_rate, channels, duration_ms)`: Calculates the PCM16 byte count for one chunk duration at a given rate and channel count.
  - `_flush_pending_input_audio()`: Drains buffered input bytes into allowed upstream chunk sizes before commit or live streaming.
  - `_clear_pending_input_audio()`: Clears buffered input audio and its associated format metadata.
  - `_current_turn_input_audio_ms(sample_rate)`: Returns how much user audio has accumulated for the active turn.
  - `_enforce_max_turn_length(sample_rate)`: Auto-commits overlong turns, emits a truncation event, and drops additional audio until the next turn boundary.
  - `ingest_audio_frame(frame)`: Routes incoming audio through VAD or direct upload logic while enforcing turn-length limits.
  - `close()`: Stops tasks, clears publisher and input buffers, cancels idle watchers, and closes the bridged realtime session.
  - `_start_turn()`: Starts a new user turn, optionally interrupts assistant output, and flushes preroll audio into the active turn.
  - `_commit_turn(...)`: Publishes commit metadata once, flushes buffered input audio, commits the turn to Qwen, and ignores duplicates after closure.
  - `_queued_assistant_egress_seconds()`: Safely reads how much assistant audio is still queued for playout.
  - `_should_hard_interrupt_for_new_speech()`: Decides whether new speech should immediately cut off current assistant output.
  - `_hard_interrupt_assistant(reason)`: Clears queued assistant audio, cancels the model response, and notifies the browser about the interruption.
  - `_consume_audio_stream(track)`: Reads a LiveKit audio stream and forwards each frame into `ingest_audio_frame`.
  - `_consume_vad_events()`: Consumes VAD start/end events and converts them into turn boundaries.
  - `_handle_vad_start(event)`: Starts a turn on VAD speech detection, flushes preroll, and begins live upstream audio streaming.
  - `_handle_vad_end(event)`: Publishes speech metadata and commits the already-streamed turn without re-uploading the utterance.
  - `_handle_published_frame(_frame_bytes)`: Schedules first-egress metric emission when the shared publisher captures a frame for the current turn.
  - `_mark_assistant_egress_active()`: Extends the short-lived "assistant is still speaking" window based on queued audio.
  - `_schedule_livekit_egress_metrics_update(queue_depth_ms)`: Schedules the async metrics callback that records first-egress queue depth once per turn.
  - `_emit_livekit_egress_metrics_update(queue_depth_ms)`: Forwards first-egress queue depth into the realtime session and clears the one-shot task reference.
  - `_handle_client_telemetry(payload)`: Parses browser telemetry events and feeds playback latency into session metrics.
  - `_emit_runtime_event(payload)`: Receives normalized runtime events, queues pre-audio text and metrics until audio starts when appropriate, and augments metrics with queue-depth, reconnect, and counter data.
  - `_is_stale_output_event(payload)`: Drops assistant output from an old output version after an interrupt or restart.
  - `_payload_output_version(payload)`: Parses the output-version field from a payload when present.
  - `_publish_control(payload, reliable=True)`: Sends a JSON control packet to the participant, attaches lineage fields, and emits worker rollups plus operator counters.
  - `_flush_pending_pre_audio_payloads()`: Publishes buffered transcript or metrics events once audio starts or the turn finishes.
  - `_buffer_pre_audio_payload(payload)`: Bounds the pre-audio payload queue and prefers dropping older metrics updates when it fills.
  - `_increment_worker_counter(name)`: Increments a persisted worker counter when worker-state tracking is enabled.
  - `_worker_counters_snapshot()`: Extracts persisted operator counters that should be mirrored into control payloads.
  - `_record_browser_activity()`: Arms or refreshes the browser-idle watchdog on control or audio activity.
  - `_cancel_browser_idle_timeout_watchdog()`: Cancels the pending browser-idle timer and clears its deadline.
  - `_handle_browser_idle_timeout_if_deadline(expected_deadline)`: Fires idle cleanup only if the scheduled deadline is still current.
  - `_handle_browser_idle_timeout()`: Emits a structured idle-timeout error and closes the participant session.
  - `_log_context(...)`: Builds a consistent worker log-context string with session, participant, room, turn, and backend details.
- `LiveKitWorker`: Owns the room connection, shared audio source and publisher, persisted worker state, and a strict single active browser participant per room.
  - `__init__(settings)`: Creates the room, audio source, audio publisher, worker-state store, participant map, stop event, and optional VAD.
  - `request_drain(reason)`: Puts the worker into drain mode, rejects new sessions, and stops immediately if there are no active participants.
  - `_force_drain_after_timeout()`: Force-closes remaining sessions when drain mode exceeds the configured timeout.
  - `_reject_participant(...)`: Publishes `ROOM_BUSY` and removes an extra participant from the LiveKit room.
  - `run()`: Connects to LiveKit, publishes the assistant track, updates worker-state status, and waits until the worker is told to stop.
  - `shutdown()`: Closes all participant sessions, closes the audio source, and disconnects the room.
  - `_register_room_handlers()`: Installs LiveKit callbacks for data packets, audio subscriptions, disconnects, and reconnect events.
  - `_handle_data_packet(data_packet)`: Parses browser control packets, enforces single-participant and drain-mode rules, and dispatches them to the right session.
  - `_handle_track_subscribed(track, participant)`: Rejects extra participants or new tracks while draining and only binds the active participant's subscribed audio track.
  - `_drop_participant(participant_identity)`: Closes and removes a participant session when that participant leaves.

## `apps/gateway/src/livekit/worker_state.py`
Purpose: Persists a lightweight LiveKit worker snapshot to disk so readiness probes can report recent worker health, counters, and latency rollups.

Classes
- `LiveKitWorkerStateStore`: Maintains the on-disk JSON snapshot used by health and readiness endpoints.
  - `__init__(path)`: Seeds the default snapshot fields and writes the initial state file.
  - `update(**fields)`: Merges changed snapshot fields and flushes the file only when values actually changed.
  - `record_metric_rollups(...)`: Stores the latest rollup metrics plus queue-depth and interrupt-clear values in the snapshot.
  - `increment_counter(name, amount=1)`: Safely increments an integer counter in the snapshot.
  - `_flush()`: Atomically writes the current snapshot to disk and stamps it with the latest update time.

Functions
- `load_livekit_worker_state(path)`: Loads and JSON-decodes a saved worker snapshot when the file exists and is valid.

## `apps/gateway/src/main.py`
Purpose: Creates the FastAPI application, configures logging/CORS, and mounts the health and LiveKit control routers.

Classes
- None.

Functions
- `app_lifespan(app)`: Starts the optional Qwen prewarm task on startup and cancels any unfinished prewarm work during shutdown.

## `apps/gateway/src/security.py`
Purpose: Redacts credentials and auth-like query values before URLs are logged or returned in health payloads.

Classes
- None.

Functions
- `redact_url_secrets(url)`: Masks URL userinfo and sensitive query parameters while leaving safe URLs unchanged.

## `apps/gateway/src/realtime/__init__.py`
Purpose: Package marker for realtime session code.

Classes
- None.

Functions
- None.

## `apps/gateway/src/realtime/audio.py`
Purpose: Provides PCM16 validation, conversion, chunking, and analysis helpers used across the gateway.

Classes
- `AudioValidationError`: Structured validation error carrying a machine-readable code and message.
  - `__init__(code, message)`: Stores the error code/message alongside the exception text.
- `ValidatedAudioChunk`: Data container for one validated input-audio chunk and its decoded metadata.

Functions
- `validate_audio_chunk(...)`: Validates base64 PCM16 mono 16 kHz input, checks allowed chunk durations, and returns the decoded chunk metadata.
- `validate_audio_bytes(...)`: Validates already-decoded PCM16 mono 16 kHz input and returns the same normalized chunk metadata structure.
- `_validate_audio_bytes(...)`: Shared internal validator that enforces PCM16 format, mono, 16 kHz, sample alignment, and allowed durations.
- `pcm16_duration_ms(byte_count, sample_rate)`: Converts a PCM16 byte count into milliseconds.
- `chunk_bytes_for_duration_ms(duration_ms, sample_rate=16000)`: Computes how many PCM16 bytes are needed for a target duration.
- `pcm16_samples(audio_pcm16, copy=False)`: Views PCM16 bytes as a NumPy `int16` array, optionally copying it.
- `pcm16_bytes(samples)`: Clips and rounds sample values and converts them back to PCM16 bytes.
- `pcm16_peak_abs(audio_pcm16, start_sample=0, sample_count=None)`: Returns peak absolute amplitude for all or part of a PCM16 buffer.
- `pcm16_sample_peak_abs(samples)`: Returns peak absolute amplitude for an in-memory sample array.
- `pcm16_rms(audio_pcm16, start_sample=0, sample_count=None)`: Returns RMS energy for all or part of a PCM16 buffer.
- `pcm16_to_wav(audio_pcm16, sample_rate)`: Wraps raw PCM16 mono audio in an in-memory WAV container.
- `wav_to_pcm16(wav_bytes)`: Validates a mono 16-bit WAV blob and returns raw PCM16 bytes plus sample rate.
- `iter_pcm16_chunks(audio_bytes, duration_ms, sample_rate=16000, pad_final_chunk=False)`: Splits PCM16 bytes into fixed-duration chunks and optionally zero-pads the last chunk.

## `apps/gateway/src/realtime/event_map.py`
Purpose: Converts internal Qwen events into the public event schema sent to clients.

Functions
- `normalize_qwen_event(event)`: Maps transcript, assistant text/audio, response completion, and error events into gateway payloads.

## `apps/gateway/src/realtime/events.py`
Purpose: Defines TypedDict payloads and public/runtime event unions for the gateway event schema.

Classes
- `GatewayEventBase`: Shared optional metadata fields attached to gateway events.
- `SessionReadyEvent`: Public `session.ready` payload.
- `TurnStartedEvent`: Public `turn.started` payload with optional speech-duration metadata.
- `TurnCommittedEvent`: Public `turn.committed` payload with optional speech, silence, and input-audio durations.
- `TranscriptDeltaEvent`: Public transcript-delta payload.
- `AssistantTextDeltaEvent`: Public assistant-text-delta payload.
- `AssistantAudioStartedEvent`: Event emitted when assistant audio playback starts and transport metadata becomes known.
- `AssistantAudioMetadataEvent`: Optional debug event exposing assistant audio format and encoded-chunk length.
- `AssistantDoneEvent`: Completion event for one assistant response.
- `AssistantInterruptedEvent`: Event emitted when assistant playback is interrupted.
- `MetricsUpdateEvent`: Metrics payload containing latency metrics, timestamp markers, and reconnect summaries.
- `ErrorEvent`: Structured error payload.
- `RoomBusyEvent`: Structured rejection payload for extra participants when the room is already occupied.
- `AssistantAudioDeltaEvent`: Runtime payload carrying one assistant audio chunk plus format metadata.
- `InputSpeechTruncatedEvent`: Event emitted when max-turn enforcement auto-commits and drops remaining speech.

Functions
- None.

## `apps/gateway/src/realtime/metrics.py`
Purpose: Tracks important session timestamps and derives latency metrics from them.

Classes
- `SessionMetrics`: Records session milestones and computes relative timestamps and latency deltas.
  - `reset_turn()`: Clears turn-specific timestamps and metric values while preserving session-level timings.
  - `mark_once(field_name)`: Sets a timestamp only if that metric has not been recorded yet.
  - `mark_latest(field_name)`: Overwrites a timestamp with the current time so the latest occurrence wins.
  - `set_value_once(field_name, value)`: Stores a numeric metric value only on first write.
  - `snapshot()`: Returns both relative timestamps and derived latency metrics in a serializable structure.
  - `_relative_ms(value)`: Converts an absolute perf-counter timestamp into milliseconds since session start.
  - `_delta_ms(start, end)`: Returns the non-negative millisecond delta between two timestamps.

## `apps/gateway/src/realtime/qwen_chat_client.py`
Purpose: Wraps Qwen's chat-completions API for text-only and streaming-audio completions.

Classes
- `QwenChatClient`: Lazy HTTP client for chat-based audio/text responses.
  - `__init__(...)`: Stores endpoint settings, system prompt, and completion limits.
  - `close()`: Closes the cached `httpx.AsyncClient` if one exists.
  - `respond_text_only(audio_pcm16, sample_rate)`: Sends audio as a WAV chat completion request and returns the extracted text response.
  - `stream_audio_response(audio_pcm16, sample_rate, speaker)`: Streams server-sent chat events, converts them into `QwenEvent` objects, and yields a final `response_done`.
  - `_get_client()`: Lazily creates and reuses the HTTP client.
  - `_build_audio_request_payload(...)`: Encodes audio to WAV/base64 and assembles the Qwen chat request body.

Functions
- `_parse_stream_event(payload)`: Converts one streamed chat payload into either an assistant text delta or assistant audio delta event.
- `_extract_stream_delta_content(payload)`: Pulls the first textual delta payload out of a streamed response choice.
- `_extract_text(body)`: Collects returned message text from a non-streaming chat completion response and raises if none exists.

## `apps/gateway/src/realtime/qwen_client.py`
Purpose: Wraps Qwen's realtime websocket API and normalizes raw websocket events into gateway-friendly objects.

Classes
- `QwenEvent`: Normalized representation of transcript, assistant text/audio, completion, and error events coming from Qwen.
- `QwenRealtimeClient`: Stateful websocket client for the realtime Qwen endpoint.
  - `__init__(...)`: Stores websocket settings and initializes response/session state.
  - `connect()`: Opens the websocket and verifies that the first server event is `session.created`.
  - `start_session(speaker, modalities, input_sample_rate, output_audio)`: Resets local session flags and sends a `session.update` request.
  - `_ensure_input_stream_started()`: Sends the non-final input-buffer commit marker before the first appended audio chunk.
  - `append_audio(pcm16_base64)`: Appends one base64 PCM16 chunk to Qwen's input buffer.
  - `commit_audio()`: Sends the final input-buffer commit marker to request a response.
  - `cancel_response()`: Treats hard interrupt as a socket close when the upstream realtime backend does not expose native cancel semantics.
  - `close()`: Closes the websocket connection if it is open.
  - `iter_events()`: Reads websocket messages with timeout/close handling and yields normalized events or terminal errors.
  - `_send(payload)`: Serializes and sends a JSON payload over the websocket, logging close failures.
  - `_parse_event(payload)`: Maps raw Qwen event types into `QwenEvent` objects and updates response-state flags.

Functions
- `_first_non_empty(*values)`: Returns the first non-empty string from a list of candidates.
- `_extract_audio_base64(payload)`: Finds audio bytes inside the common raw payload shapes Qwen may emit.
- `_extract_audio_sample_rate(payload)`: Finds the emitted sample rate across the payload variants Qwen may use.
- `_extract_error_code(payload)`: Normalizes an error code from top-level or nested error fields.
- `_extract_error_message(payload)`: Normalizes an error message from top-level or nested error fields.
- `is_qwen_connection_retryable_error(exc)`: Walks an exception chain and decides whether the failure looks transient and safe to retry.
- `is_qwen_connection_closed_error(exc)`: Alias for the retryable-connection check used by session recovery code.
- `probe_qwen_realtime_websocket(url, request_timeout_seconds, max_ws_message_bytes, debug_raw_events=False)`: Opens a websocket, verifies startup, and closes it as a lightweight connectivity probe.
- `_expect_session_created(websocket, timeout_seconds, debug_raw_events)`: Validates the initial websocket event and raises descriptive startup errors for bad/missing handshake events.

## `apps/gateway/src/realtime/session.py`
Purpose: Orchestrates one browser conversation turn, including buffering, upstream Qwen connections, retries, output gating, and metrics.

Classes
- `RealtimeSessionConfig`: Stores the selected speaker, requested modalities, input sample rate, and whether audio output is enabled.
- `RealtimeSession`: Owns one end-to-end conversation session between the browser and the configured Qwen backend.
  - `__init__(settings, emit_event)`: Initializes session identifiers, event sink, upstream client slots, locks, buffers, metrics, reconnect counters, and output-version tracking.
  - `send_event(payload)`: Emits one event through the configured sink unless the session is closed.
  - `send_error(code, message)`: Emits a standardized error payload.
  - `_backend_name()`: Returns the public backend name for the current session mode or `inactive` before startup.
  - `_log_context(...)`: Builds a consistent session log context string with session, participant, room, turn, output version, and backend.
  - `close()`: Marks the session closed and tears down all runtime state.
  - `_reset_runtime()`: Cancels in-flight tasks, closes Qwen clients, clears buffers, resets watchdogs, and zeros per-turn state.
  - `_clear_turn_audio_buffer()`: Clears buffered audio for the current turn and resets its byte counters.
  - `_request_metrics_emit()`: Marks that a fresh `metrics.update` should be sent when the next flush point is reached.
  - `_cancel_response_watchdogs()`: Cancels the first-audio and total-response timeout tasks and waits for them to exit cleanly.
  - `_arm_response_watchdogs(expect_first_audio)`: Starts the active response timeout watchdogs and bumps their generation token.
  - `_run_first_audio_timeout_watchdog(generation)`: Waits for first assistant audio and triggers a structured timeout if it never arrives.
  - `_run_total_response_timeout_watchdog(generation)`: Waits for overall response completion and triggers a structured timeout if it hangs.
  - `_handle_response_timeout(...)`: Cancels the active response, rotates the upstream realtime session, and emits the appropriate timeout error.
  - `begin_turn(turn_id=None)`: Resets turn metrics and records the externally assigned turn id.
  - `mark_vad_speech_start()`: Records the start-of-speech timestamp once.
  - `mark_vad_speech_end()`: Records the end-of-speech timestamp once.
  - `mark_turn_closed()`: Records turn closure and requests a metrics flush when it happens for the first time.
  - `mark_browser_first_audio_played(latency_ms)`: Stores browser-reported first-audio-played latency once and requests a metrics update.
  - `flush_metrics_update()`: Forces an immediate `metrics.update` emission if the snapshot changed.
  - `mark_livekit_egress_started_with_queue_depth(queue_depth_ms)`: Records first LiveKit egress, captures initial queue depth, and emits metrics immediately.
  - `_build_qwen_realtime_client()`: Creates a configured realtime websocket client from current settings.
  - `_build_qwen_chat_client()`: Creates a configured chat-completions client from current settings.
  - `_uses_realtime_audio_backend()`: Returns whether audio responses should come from the realtime websocket backend.
  - `_close_qwen_realtime_session()`: Closes the realtime websocket session while holding the session lock.
  - `_close_qwen_realtime_session_unlocked()`: Cancels the Qwen event reader task and closes the realtime client.
  - `_open_qwen_realtime_session()`: Opens the realtime websocket session while holding the session lock.
  - `_open_qwen_realtime_session_unlocked()`: Connects to Qwen, starts the upstream session, and starts the Qwen event pump task.
  - `_open_qwen_realtime_session_with_retry_unlocked(reason)`: Retries transient realtime session open failures until the request timeout expires.
  - `_restart_qwen_realtime_session(preserve_turn_audio=False, advance_output_version=True, reason="unspecified")`: Locked wrapper that rotates the realtime session.
  - `_restart_qwen_realtime_session_unlocked(...)`: Reopens the upstream session, optionally preserves buffered turn audio, and resets assistant-output state.
  - `_ensure_qwen_session_for_buffered_turn(reason)`: Recreates a missing Qwen session when there is still buffered turn audio to send.
  - `_replay_turn_audio_unlocked()`: Re-sends buffered turn audio chunks into a fresh realtime session.
  - `_recover_qwen_turn_after_send_failure(failed_action, exc, commit_after_replay)`: Attempts to recover from transient append/commit failures by reopening Qwen and replaying the turn.
  - `_start_session(event)`: Validates the requested session config, resets runtime state, chooses the proper backend, and emits `session.ready` with explicit backend metadata.
  - `start_session(event)`: Public wrapper for session start.
  - `_append_audio(event)`: Validates an incoming audio chunk, enforces the max-turn limit, forwards or buffers it, and handles send-recovery logic.
  - `_append_validated_audio(validated)`: Handles a prevalidated audio chunk, reuses restart and limit logic, and forwards or buffers it according to backend mode.
  - `append_audio_chunk(audio_base64, sample_rate, channels=1, audio_format="pcm16")`: Public wrapper for appending a single input chunk.
  - `append_audio_bytes(audio_bytes, sample_rate, channels=1, audio_format="pcm16")`: Public wrapper for appending already-decoded PCM16 bytes.
  - `_commit_audio()`: Commits the buffered turn using text-only chat, streaming chat audio, or realtime websocket audio depending on session mode.
  - `commit_audio()`: Public wrapper for commit.
  - `_cancel_response()`: Cancels active local tasks or restarts the realtime session to interrupt assistant output.
  - `cancel_response()`: Public wrapper for interrupt/cancel.
  - `mark_livekit_egress_started()`: Compatibility helper that records first LiveKit assistant egress when queue-depth metadata is unavailable.
  - `_bump_output_version(reason)`: Advances the output version so stale assistant events can be filtered after interrupts or restarts.
  - `_is_current_output_version(output_version)`: Checks whether a payload belongs to the active output version.
  - `_log_stale_qwen_event(qwen_event, output_version)`: Logs dropped assistant output from older output versions.
  - `_record_qwen_session_reason(reason)`: Increments reconnect counters using a normalized restart reason label.
  - `_normalize_qwen_session_reason(reason)`: Maps internal restart reasons to the public reconnect-reason names used in metrics.
  - `_pump_qwen_events(output_version)`: Consumes the Qwen event iterator and forwards events until the session closes.
  - `_handle_qwen_event(qwen_event, output_version=None)`: Buffers pre-commit assistant output when needed and otherwise forwards normalized Qwen events.
  - `_should_buffer_assistant_event(qwen_event)`: Decides whether assistant text/audio/done should be held behind the output gate.
  - `_buffer_assistant_event(output_version, qwen_event)`: Buffers assistant events before commit and drops lower-priority entries when the buffer cap is hit.
  - `_select_buffered_assistant_drop_index()`: Picks which buffered assistant event should be discarded first when the precommit buffer is full.
  - `_flush_buffered_assistant_events()`: Releases assistant events that arrived before commit once the output gate opens.
  - `_forward_qwen_event(qwen_event, output_version)`: Updates counters/metrics, normalizes the event, attaches output versioning, and emits it downstream.
  - `_send_metrics_update()`: Emits `metrics.update` only when the metrics snapshot changed.
  - `_run_streaming_audio_completion(audio_pcm16, sample_rate, speaker, output_version)`: Streams chat-based audio completion events and converts failures into structured errors.
  - `_run_text_only_completion()`: Runs a text-only chat completion from buffered audio and emits assistant text and done events.
  - `has_active_response()`: Reports whether a local or upstream assistant response is still in progress.

## `apps/gateway/tests/test_audio.py`
Purpose: Verifies audio validation, chunking, NumPy PCM helpers, and WAV round-tripping.

Functions
- `test_validate_audio_chunk_accepts_valid_80ms_pcm16()`: Confirms that a correctly sized 80 ms chunk is accepted and its metadata is computed correctly.
- `test_validate_audio_chunk_rejects_invalid_duration()`: Confirms that chunks with unsupported durations raise `INVALID_CHUNK_DURATION`.
- `test_validate_audio_bytes_accepts_internal_pcm16_chunk()`: Confirms already-decoded PCM16 input can be validated internally without first base64-encoding it.
- `test_iter_pcm16_chunks_pads_final_chunk_when_requested()`: Verifies that the final chunk is zero-padded when padding is enabled.
- `test_iter_pcm16_chunks_rejects_unaligned_pcm16_bytes()`: Verifies that odd-length PCM16 buffers are rejected.
- `test_pcm16_numpy_helpers_handle_signed_extremes()`: Confirms sample decoding, peak detection, and RMS math work for signed extreme values.
- `test_pcm16_to_wav_round_trips_back_to_pcm16()`: Confirms PCM16 audio survives WAV encoding and decoding unchanged.
- `test_wav_to_pcm16_rejects_invalid_wav()`: Confirms invalid WAV input raises a validation error.

## `apps/gateway/tests/test_benchmark_scripts.py`
Purpose: Smoke-tests the benchmark scripts with stub inputs and validates their JSON output summaries.

Functions
- `test_benchmark_qwen_ttfb_script_runs_with_stub(tmp_path)`: Verifies the Qwen TTFB benchmark runs against the stub backend and writes aggregate latency results.
- `test_benchmark_livekit_egress_script_runs(tmp_path)`: Verifies the LiveKit egress benchmark runs and records first-frame and frame-count aggregates.
- `test_benchmark_realtime_turn_script_runs_with_stub_backend(tmp_path)`: Verifies the end-to-end realtime-turn benchmark runs against the in-process stub backend and writes turn metrics.
- `test_smoke_stub_qwen_realtime_script_runs(tmp_path)`: Verifies the stub-Qwen smoke script generates output artifacts plus assistant-done and latency metrics.

## `apps/gateway/tests/test_capture_audio_analysis.py`
Purpose: Verifies the external `scripts/analyze_capture_audio.py` analysis logic against synthetic audio patterns.

Functions
- `_load_analysis_module()`: Loads `scripts/analyze_capture_audio.py` dynamically for direct function testing.
- `_segment(sample_count, amplitude)`: Builds a constant-amplitude sample segment.
- `_repeat_upsample(values, factor)`: Repeats each sample value to simulate simple upsampling.
- `test_analyze_samples_ignores_edge_silence_runs()`: Verifies that long silence at the edges is tracked separately and does not trigger warnings.
- `test_analyze_samples_flags_internal_silence_runs()`: Verifies that suspicious silence inside voiced content is reported as a warning.
- `test_analyze_samples_does_not_flag_low_energy_internal_pause()`: Verifies that a low-energy internal pause is tolerated even when it exceeds the raw silence threshold.
- `test_analyze_samples_flags_rough_noisy_frames_and_click_spikes()`: Verifies that noisy voiced frames and click spikes are both detected and warned about.
- `test_analyze_samples_normalizes_artifact_metrics_across_sample_rates()`: Verifies that artifact metrics stay comparable after rate normalization from 48 kHz to 24 kHz analysis.
- `test_analyze_samples_defaults_artifacts_to_native_rate()`: Verifies that artifact analysis stays at the native sample rate when no normalization rate is requested.

## `apps/gateway/tests/test_config.py`
Purpose: Verifies settings parsing defaults and the fixed audio-rate validation rules.

Functions
- `test_settings_parse_comma_separated_cors_allow_origins()`: Confirms comma-separated CORS origins are split and trimmed correctly.
- `test_settings_allow_empty_cors_allow_origins()`: Confirms an empty CORS string becomes an empty tuple.
- `test_settings_allow_wildcard_cors_outside_production()`: Confirms wildcard CORS is still allowed in non-production environments.
- `test_settings_reject_wildcard_cors_in_production()`: Confirms production config rejects wildcard CORS origins.
- `test_settings_default_audio_artifact_log_path_uses_workspace()`: Confirms the default artifact-log path is rooted under the workspace temp directory.
- `test_settings_default_livekit_worker_state_path_uses_workspace()`: Confirms the default worker-state snapshot path is rooted under the workspace temp directory.
- `test_settings_default_qwen_audio_backend()`: Confirms realtime audio is the default Qwen backend.
- `test_settings_default_audio_rates_separate_model_from_livekit()`: Confirms the default LiveKit and Qwen sample rates stay on their intended fixed values.
- `test_settings_parse_preferred_qwen_audio_rate_env_names()`: Confirms the preferred Qwen env var names map to the expected settings fields.
- `test_settings_parse_architecture_alias_env_names()`: Confirms legacy architecture env aliases still map to the current settings fields.
- `test_settings_reject_livekit_publish_rate_equal_to_qwen_output_rate()`: Confirms an invalid LiveKit output rate is rejected.
- `test_settings_reject_livekit_input_rate_equal_to_qwen_input_rate()`: Confirms an invalid LiveKit input rate is rejected.
- `test_settings_reject_non_native_qwen_output_rate()`: Confirms an unsupported Qwen output rate is rejected.
- `test_settings_default_livekit_input_vad()`: Confirms the default VAD settings are enabled and set to the expected values.
- `test_settings_reject_invalid_livekit_output_frame_ms()`: Confirms unsupported LiveKit output frame sizes are rejected.
- `test_settings_reject_livekit_output_queue_smaller_than_frame()`: Confirms the output queue cannot be smaller than one publish frame.
- `test_settings_reject_livekit_output_queue_above_barge_in_limit()`: Confirms excessively large LiveKit output queues are rejected.
- `test_settings_reject_livekit_max_turn_below_one_second()`: Confirms max-turn limits below one second are rejected.
- `test_settings_reject_livekit_max_turn_above_thirty_seconds()`: Confirms max-turn limits above thirty seconds are rejected.
- `test_settings_reject_unsupported_livekit_qwen_input_chunk_ms()`: Confirms unsupported LiveKit-to-Qwen chunk sizes are rejected.
- `test_settings_reject_non_positive_qwen_first_audio_timeout()`: Confirms the first-audio timeout must be positive.
- `test_settings_reject_total_response_timeout_shorter_than_first_audio_timeout()`: Confirms total-response timeout cannot be shorter than first-audio timeout.
- `test_settings_reject_non_positive_qwen_prewarm_max_wait()`: Confirms the startup prewarm max-wait setting must be positive.
- `test_settings_reject_non_positive_qwen_prewarm_retry_interval()`: Confirms the startup prewarm retry interval must be positive.
- `test_settings_reject_non_positive_livekit_drain_timeout()`: Confirms the worker drain timeout must be positive.
- `test_settings_reject_non_positive_livekit_browser_idle_timeout()`: Confirms the browser idle timeout must be positive.

## `apps/gateway/tests/test_health.py`
Purpose: Verifies liveness/readiness endpoints and Qwen probe behavior.

Functions
- `test_health_returns_container_alive()`: Confirms `/health` returns the expected liveness payload.
- `test_ready_returns_200_when_probe_reports_ready(monkeypatch)`: Confirms `/ready` returns `200` and uses shallow readiness by default when probes report success.
- `test_ready_query_can_enable_deep_probe(monkeypatch)`: Confirms `/ready?deep=true` forwards the deep-probe flag into the Qwen readiness path.
- `test_diagnostics_deep_always_uses_deep_probe(monkeypatch)`: Confirms `/diagnostics/deep` always forces the deep readiness path.
- `test_probe_livekit_merges_worker_snapshot_rollups(monkeypatch, tmp_path)`: Confirms LiveKit readiness merges persisted worker counters, reconnect reasons, and latency rollups into the probe payload.
- `test_ready_returns_503_when_probe_reports_unreachable(monkeypatch)`: Confirms `/ready` returns `503` when the Qwen probe fails.
- `test_ready_returns_503_when_livekit_worker_is_draining(monkeypatch)`: Confirms readiness degrades with `503` when the LiveKit worker reports drain mode.
- `test_probe_qwen_reports_failed_when_realtime_session_probe_rejects(monkeypatch)`: Confirms Qwen startup probe failures are surfaced as `qwen_failed`.
- `test_probe_qwen_reports_failed_when_realtime_inference_probe_rejects(monkeypatch)`: Confirms deep realtime inference failures are surfaced as `qwen_failed`.
- `test_probe_qwen_explicit_non_deep_skips_realtime_inference_probe(monkeypatch)`: Confirms a non-deep readiness check does not run the expensive inference probe.
- `test_probe_qwen_reuses_recent_successful_inference_probe(monkeypatch)`: Confirms the inference-probe cache suppresses repeated deep checks within the TTL window.
- `test_prewarm_qwen_startup_retries_until_ready(monkeypatch)`: Confirms startup prewarm retries deep probes until Qwen reports ready.
- `test_app_startup_schedules_qwen_prewarm_when_enabled(monkeypatch)`: Confirms FastAPI startup schedules the background prewarm task when the feature flag is enabled.

## `apps/gateway/tests/test_livekit_control.py`
Purpose: Verifies LiveKit token helpers and the browser session-control route.

Functions
- `_decode_jwt_payload(token)`: Decodes the JWT payload segment so token grants can be asserted in tests.
- `test_build_browser_identity_uses_configured_prefix()`: Confirms browser participant identities use the configured prefix.
- `test_livekit_tokens_use_minimal_expected_video_grants()`: Confirms browser and worker tokens carry the expected room-join, publish, subscribe, and data grants.
- `test_livekit_session_returns_503_when_control_plane_is_not_configured(monkeypatch)`: Confirms the session route rejects requests when LiveKit control-plane config is missing.
- `test_livekit_session_returns_identity_token_and_topic(monkeypatch)`: Confirms the session route returns room details, control topic, participant identity, and a matching browser token.
- `test_livekit_session_returns_503_when_worker_is_draining(monkeypatch, tmp_path)`: Confirms the session route rejects new sessions while the LiveKit worker snapshot reports drain mode.

## `apps/gateway/tests/test_livekit_output.py`
Purpose: Verifies assistant audio publishing behavior including resampling, silence trimming, and smoothing.

Classes
- `FakeAudioSource`: Minimal stand-in for a LiveKit audio source used to inspect published frames.
  - `__init__()`: Initializes the captured-frame list, queue settings, and cleared flag.
  - `capture_frame(frame)`: Records one published frame.
  - `clear_queue()`: Records that the queue was cleared.
  - `wait_for_playout()`: Async no-op used to satisfy the publisher interface.
  - `queued_duration`: Reports the configured synthetic output-queue depth.

Functions
- `_constant_pcm16_base64(samples, amplitude)`: Builds a base64 PCM16 chunk filled with a constant sample value.
- `_pcm16_base64(values)`: Builds a base64 PCM16 chunk from an explicit sample list.
- `test_assistant_audio_publisher_resamples_to_48k_and_fades_partial_tail()`: Verifies 24 kHz input is resampled to 48 kHz and a short tail is padded with fade-out samples.
- `test_assistant_audio_publisher_clear_resets_state()`: Verifies `clear()` drops buffered frames and resets source state.
- `test_assistant_audio_publisher_calls_frame_sink_for_complete_frames()`: Verifies the optional frame sink receives the same bytes published to the audio source.
- `test_assistant_audio_publisher_emits_stable_frames_from_uneven_chunks()`: Verifies uneven chunk boundaries still produce stable published frame sizes.
- `test_assistant_audio_publisher_trims_turn_leading_silence()`: Verifies the first voiced chunk is trimmed to remove initial silence.
- `test_assistant_audio_publisher_applies_fade_in_after_leading_trim()`: Verifies trimmed leading speech is faded in to avoid a sharp click at turn start.
- `test_assistant_audio_publisher_smooths_large_chunk_boundary_jump()`: Verifies a large chunk-to-chunk amplitude jump is softened.
- `test_assistant_audio_publisher_does_not_apply_boundary_smoothing_when_it_worsens_transition()`: Verifies smoothing is skipped when it would make a boundary transition worse.
- `test_assistant_audio_publisher_smooths_isolated_spike()`: Verifies an isolated click spike is smoothed out.
- `test_assistant_audio_publisher_preserves_short_internal_silence_after_voice_started()`: Verifies a short natural internal pause is preserved after voiced audio has started.
- `test_assistant_audio_publisher_logs_queue_backpressure_warning()`: Verifies the publisher logs when queued assistant playout approaches the configured cap.
- `test_assistant_audio_publisher_drops_pending_frames_at_hard_queue_cap(caplog)`: Verifies the publisher drops complete frames when the output queue reaches its hard cap.
- `test_assistant_audio_publisher_rejects_mid_turn_sample_rate_change()`: Verifies assistant output sample rate is pinned for the entire turn.
- `test_assistant_audio_publisher_trims_post_start_chunk_leading_silence()`: Verifies leading silence is also trimmed from later chunks in the same turn.
- `test_assistant_audio_publisher_trims_post_start_chunk_trailing_silence()`: Verifies trailing silence is trimmed from later chunks while preserving voiced content.

## `apps/gateway/tests/test_livekit_worker.py`
Purpose: Verifies LiveKit worker/session bridging, control flow, audio upload, VAD handling, and stale-output suppression.

Classes
- `FakeRoom`: Minimal fake room object with a fake local participant and handler-registration shim.
  - `__init__()`: Creates the fake local participant used to capture published control messages.
  - `on(_event_name)`: Returns a decorator that leaves registered callbacks unchanged.
- `FakeLocalParticipant`: Captures `publish_data` calls made by the worker/session.
  - `__init__()`: Initializes the list of published payloads.
  - `publish_data(payload, reliable, destination_identities, topic)`: Validates the call shape and stores the decoded JSON payload.
- `FakeAudioSource`: Minimal stand-in for LiveKit's `AudioSource` used when constructing the worker.
  - `__init__(sample_rate, num_channels, queue_size_ms=1000, loop=None)`: Stores the source construction parameters for assertions.
- `FakeAudioPublisher`: Minimal assistant-audio publisher stand-in used during worker construction tests.
  - `__init__(audio_source, output_sample_rate, output_frame_ms, frame_sink=None)`: Stores constructor arguments for later inspection.
  - `set_log_context(**_kwargs)`: No-op context setter used to satisfy the real publisher interface.
- `RecordingAudioPublisher`: Test publisher that records enqueued audio and queue state across a turn.
  - `__init__()`: Initializes counters for enqueued audio, finalize calls, clear calls, and queue duration.
  - `set_log_context(**_kwargs)`: No-op context setter used to satisfy the real publisher interface.
  - `enqueue_base64(audio_base64, input_sample_rate)`: Records an assistant audio chunk and simulates queue growth.
  - `finalize_turn()`: Records that the turn was finalized.
  - `clear()`: Records that the queue was cleared and resets queued duration.
  - `queued_duration_seconds()`: Returns the simulated queued assistant-audio duration.
- `FakePublishingAudioSource`: Fake publishing source used to exercise the real publisher path without a full LiveKit room.
  - `__init__(queue_size_ms=150)`: Initializes captured frames and a configurable synthetic queue depth.
  - `capture_frame(frame)`: Records a published frame.
  - `clear_queue()`: Clears captured frames.
  - `wait_for_playout()`: Async no-op used to satisfy the source interface.
  - `queued_duration`: Reports the synthetic queued playout depth.
- `FakeRealtimeSession`: Test double for `RealtimeSession` that captures calls and emits canned assistant events.
  - `__init__(settings, emit_event)`: Stores the event sink and initializes counters for start, append, commit, cancel, and output-version state.
  - `start_session(payload)`: Records the requested session-start payload.
  - `append_audio_chunk(**kwargs)`: Records one uploaded audio chunk.
  - `append_audio_bytes(**kwargs)`: Records one uploaded raw-audio chunk.
  - `commit_audio()`: Simulates a completed assistant turn by emitting audio and done events.
  - `cancel_response()`: Records that cancel was requested.
  - `close()`: Async no-op used to satisfy the session interface.
  - `begin_turn(turn_id=None)`: Records the new turn id assigned by the bridge.
  - `mark_vad_speech_start()`: Records that VAD speech-start metrics were marked.
  - `mark_vad_speech_end()`: Records that VAD speech-end metrics were marked.
  - `mark_livekit_egress_started()`: Records that assistant egress was marked.
  - `mark_livekit_egress_started_with_queue_depth(queue_depth_ms)`: Records assistant egress plus the queue depth captured at first frame.
  - `mark_browser_first_audio_played(latency_ms)`: Records browser-reported first-audio-played latency.
  - `flush_metrics_update()`: Records that metrics were explicitly flushed.
  - `has_active_response()`: Returns the test-controlled active-response flag.
- `FakeInterruptingRealtimeSession`: `FakeRealtimeSession` variant whose cancel path also advances output version and clears active-response state.
  - `cancel_response()`: Simulates an interrupt that rotates output lineage immediately.
- `StubRealtimeQwenClient`: Minimal realtime-client stub used to exercise the real publisher path and delayed assistant output.
  - `__init__(output_sample_rate)`: Stores the output rate and creates the async event queue.
  - `connect()`: Async no-op used to satisfy startup.
  - `start_session(...)`: Async no-op used to satisfy session configuration.
  - `append_audio(pcm16_base64)`: Async no-op used to satisfy audio upload.
  - `commit_audio()`: Schedules a delayed assistant audio delta followed by `response_done`.
  - `cancel_response()`: Marks the stub closed and ends event iteration.
  - `close()`: Marks the stub closed and ends event iteration.
  - `iter_events()`: Yields queued stub events until closed.
- `FakeVADStream`: Async iterator used to feed synthetic VAD events into a participant session.
  - `__init__()`: Creates the event queue and a list of frames pushed into the stream.
  - `push_frame(frame)`: Records an audio frame sent into VAD.
  - `aclose()`: Pushes the sentinel that ends VAD iteration.
  - `emit(event)`: Queues a VAD event for later consumption.
  - `__aiter__()`: Returns the stream itself as an async iterator.
  - `__anext__()`: Waits for the next VAD event or ends iteration on the sentinel.
- `FakeVAD`: Minimal wrapper exposing the fake VAD stream through the same API as the real VAD object.
  - `__init__(stream)`: Stores the stream instance.
  - `stream()`: Returns the stored fake VAD stream.
- `FakeAudioStream`: Async iterator that simulates a LiveKit audio track delivering exactly one frame.
  - `__init__(track, sample_rate, num_channels, frame_size_ms)`: Records stream-construction parameters for assertions.
  - `__aiter__()`: Returns the fake stream itself.
  - `__anext__()`: Yields one synthetic frame event, then stops.
  - `aclose()`: Async no-op used to satisfy the audio-stream interface.
- `DummyClosableSession`: Minimal session double used to verify forced drain closes active sessions.
  - `__init__()`: Initializes the close-call counter.
  - `close()`: Records that forced drain closed the session.

Functions
- `_pcm_frame(sample_rate, samples)`: Builds a real `rtc.AudioFrame` from a list of PCM16 sample values.
- `test_livekit_worker_uses_configured_audio_queue(monkeypatch)`: Confirms the worker creates its shared audio source and publisher with the configured queue and frame settings.
- `test_livekit_worker_request_drain_sets_stop_event_without_sessions(monkeypatch)`: Confirms drain mode stops the worker immediately when no sessions are active.
- `test_livekit_worker_force_drains_active_sessions_after_timeout(monkeypatch)`: Confirms drain mode force-closes active sessions after the configured timeout.
- `test_livekit_worker_rejects_new_subscribed_track_while_draining(monkeypatch)`: Confirms new audio subscriptions are rejected once the worker enters drain mode.
- `test_livekit_worker_rejects_second_participant_audio_subscription(monkeypatch)`: Confirms an extra participant is rejected and removed before it can share the active session.
- `test_livekit_output_defaults_match_supported_transport()`: Confirms the default LiveKit/Qwen sample rates and queue settings line up with the supported transport.
- `test_livekit_worker_rejects_24k_transport_publish_rate()`: Confirms invalid 24 kHz LiveKit output transport settings are rejected.
- `test_assistant_track_publish_options_disable_dtx_and_enable_red()`: Confirms assistant track publishing disables DTX, enables RED, and sets the expected bitrate cap.
- `test_livekit_commit_keeps_pre_audio_control_events_until_first_audio(monkeypatch)`: Verifies transcript and metrics events are buffered until the first assistant audio chunk arrives.
- `test_livekit_rejects_manual_turn_control_when_server_vad_is_enabled(monkeypatch)`: Verifies manual speech start/commit events are rejected while server-side VAD is the active turn owner.
- `test_livekit_audio_stream_resamples_transport_input_to_qwen_rate(monkeypatch)`: Verifies subscribed browser audio is requested at Qwen's 16 kHz input rate.
- `test_livekit_commit_forwards_audio_metadata_only_in_debug_mode(monkeypatch)`: Verifies assistant audio metadata events are only published when debug mode is enabled.
- `test_livekit_drops_stale_audio_before_egress(monkeypatch)`: Verifies assistant audio from an old output version is dropped before it reaches LiveKit.
- `test_livekit_speech_start_hard_interrupts_queued_assistant_audio(monkeypatch)`: Verifies new user speech clears queued assistant audio and cancels the active response.
- `test_livekit_interrupt_drops_late_stale_audio_from_old_output_version(monkeypatch)`: Verifies late assistant audio from the canceled output version is ignored after an interrupt rotates lineage.
- `test_livekit_metrics_include_interrupt_clear_time_after_interrupt(monkeypatch)`: Verifies post-interrupt metrics expose queue depth and interrupt-clear timing.
- `test_livekit_duplicate_commit_is_ignored_after_turn_is_closed(monkeypatch)`: Verifies closely spaced duplicate commit signals do not double-commit upstream.
- `test_livekit_turn_ids_increment_monotonically_per_session(monkeypatch)`: Verifies committed LiveKit turn ids advance monotonically for each participant session.
- `test_livekit_browser_playback_telemetry_updates_session_metrics(monkeypatch)`: Verifies browser playback telemetry updates session metrics and triggers a metrics flush.
- `test_livekit_browser_idle_timeout_emits_structured_error_and_cleans_session(monkeypatch, tmp_path)`: Verifies idle browsers receive a structured timeout error and the session cleans up worker state and publisher buffers.
- `test_livekit_real_publisher_path_does_not_deadlock_first_egress_metrics(monkeypatch)`: Verifies the real publisher path can publish assistant audio and first-egress metrics without deadlocking.
- `test_livekit_vad_streams_audio_before_end_of_speech(monkeypatch)`: Verifies VAD starts streaming audio live on speech start and only commits on speech end.
- `test_livekit_caps_buffered_pre_audio_payloads(monkeypatch)`: Verifies the pending pre-audio payload queue stays bounded when metrics arrive before assistant audio.
- `test_livekit_max_turn_auto_commits_once_and_drops_tail_until_vad_end(monkeypatch)`: Verifies overlong turns auto-commit once, emit truncation metadata, and drop extra audio until VAD closes the turn.
- `test_configure_worker_logging_skips_artifact_file_when_disabled(monkeypatch, tmp_path)`: Verifies worker logging skips creating the artifact file when artifact logging is disabled.
- `test_configure_worker_logging_enables_artifact_file_when_requested(monkeypatch, tmp_path)`: Verifies worker logging installs the artifact file handler when artifact logging is enabled.

## `apps/gateway/tests/test_qwen3_omni_initial_codec_chunk.py`
Purpose: Verifies the patched Qwen runtime emits the first codec chunk at the intended frame boundary.

Classes
- `_FakeTransferManager`: Minimal fake transfer manager exposing the config and counters used by the extracted Qwen processor function.
  - `__init__(extra)`: Stores the config map and initializes chunk-tracking structures.

Functions
- `_load_talker2code2wav_async_chunk()`: Extracts and executes the `talker2code2wav_async_chunk` function from the installed patched `qwen3_omni.py`.
- `_make_request(request_id="req-1", finished=False)`: Builds a minimal fake request object with the attributes the processor expects.
- `_make_pooling_output(frame_seed)`: Builds synthetic codec-frame tensor output for one processor step.
- `_emit_frame(transfer_manager, request, frame_seed, is_finished=False)`: Runs one synthetic processor step and updates the fake transfer-manager chunk count when output is produced.
- `test_qwen3_omni_waits_for_regular_chunk_without_initial_override()`: Verifies the runtime waits for the normal codec chunk size before emitting the first payload when no override is configured.
- `test_qwen3_omni_emits_first_chunk_after_initial_codec_chunk_frames()`: Verifies the runtime can emit an earlier first payload when `initial_codec_chunk_frames` is configured.

## `apps/gateway/tests/test_qwen_chat_client.py`
Purpose: Verifies streamed chat events are converted into normalized text and audio deltas.

Functions
- `test_parse_stream_event_maps_text_delta()`: Confirms a streamed text delta becomes an `assistant_text_delta` event.
- `test_parse_stream_event_decodes_wav_audio_chunk()`: Confirms a streamed WAV audio delta is decoded into PCM16 bytes with the correct sample rate.

## `apps/gateway/tests/test_qwen_client.py`
Purpose: Verifies realtime websocket startup, event parsing, and input-stream behavior for the Qwen client.

Classes
- `FakeWebSocket`: Minimal fake websocket used to script recv/send/close behavior.
  - `__init__(messages)`: Stores queued incoming messages and initializes send/close state.
  - `recv()`: Returns the next scripted incoming message.
  - `send(payload)`: Records one outbound JSON payload.
  - `close()`: Records that the websocket was closed.
- `SlowSendWebSocket`: Fake websocket whose `send()` call intentionally stalls to trigger request-timeout behavior.
  - `__init__(send_delay_seconds)`: Stores the artificial send delay used by timeout tests.
  - `send(payload)`: Sleeps for the configured delay before forwarding to the base fake websocket.

Functions
- `test_probe_qwen_realtime_websocket_accepts_session_created(monkeypatch)`: Confirms the websocket probe succeeds when startup returns `session.created`.
- `test_qwen_realtime_client_connect_rejects_error_startup_event(monkeypatch)`: Confirms client connect fails cleanly when the first startup event is an error.
- `test_parse_event_uses_string_error_message()`: Confirms string-valued error payloads are preserved in normalized error events.
- `test_parse_event_treats_transcription_done_as_terminal_response()`: Confirms `transcription.done` ends the response and `response.audio.done` is ignored.
- `test_parse_event_separates_input_transcript_from_assistant_output_text()`: Confirms user transcript deltas and assistant text deltas are normalized into different event kinds.
- `test_qwen_client_starts_input_stream_before_first_append()`: Confirms the client sends the non-final commit marker before the first appended audio chunk.
- `test_qwen_client_cancel_closes_socket_when_backend_has_no_cancel()`: Confirms hard cancel closes the realtime socket when no upstream cancel event exists.
- `test_qwen_client_send_times_out_when_websocket_stalls()`: Confirms outbound websocket sends respect the configured request timeout.

## `apps/gateway/tests/test_security.py`
Purpose: Verifies URL secret redaction used by health payloads and logging.

Functions
- `test_redact_url_secrets_masks_userinfo_and_sensitive_query_values()`: Confirms URL userinfo and auth-like query values are replaced with redacted placeholders.
- `test_redact_url_secrets_leaves_safe_urls_unchanged()`: Confirms URLs without sensitive parts are returned unchanged.

## `apps/gateway/tests/test_session_runtime.py`
Purpose: Verifies realtime session startup, buffering, output gating, mode selection, cancel and timeout behavior, and recovery logic.

Classes
- `EventCollector`: Captures emitted session events for assertions.
  - `__init__()`: Initializes the captured event list.
  - `emit(payload)`: Appends one emitted event payload.
- `FailingQwenClient`: Realtime-client stub that fails during connect.
  - `__init__(**_kwargs)`: Accepts constructor args without storing them.
  - `connect()`: Raises an `unreachable` error to simulate an unavailable upstream service.
  - `close()`: Async no-op used to satisfy the session interface.
- `FakeQwenClient`: Realtime-client stub that records audio and emits a normal transcript, text, audio, and done sequence on commit.
  - `__init__(**_kwargs)`: Initializes the event queue, recorded audio chunks, and cancel flag.
  - `connect()`: Async no-op used to satisfy startup.
  - `start_session(speaker, modalities, input_sample_rate, output_audio)`: Records the requested upstream session settings.
  - `append_audio(pcm16_base64)`: Records one uploaded audio chunk.
  - `commit_audio()`: Emits transcript, assistant text, assistant audio, and final done events.
  - `cancel_response()`: Records that cancel was requested.
  - `close()`: Async no-op used to satisfy teardown.
  - `iter_events()`: Yields queued fake events until the sentinel is reached.
- `FakeEarlyAssistantQwenClient`: Variant that starts emitting transcript and audio before the downstream commit call arrives.
  - `append_audio(pcm16_base64)`: Emits early transcript and audio on the first chunk to test output gating.
  - `commit_audio()`: Emits the remaining assistant text and done events after commit.
- `FakePrecommitResponseQwenClient`: Variant that fully finishes the response before commit so the session can verify it does not double-commit upstream.
  - `__init__(**kwargs)`: Extends the base fake client and initializes a commit-call counter.
  - `append_audio(pcm16_base64)`: Emits transcript, audio, and done events before downstream commit.
  - `commit_audio()`: Only increments the commit counter instead of producing more events.
- `FakeRestartableQwenClient`: Variant that tracks created instances and marks itself closed when torn down.
  - `__init__(**kwargs)`: Registers the instance, assigns an incrementing id, and initializes close state.
  - `commit_audio()`: Emits one assistant audio delta and leaves the response open.
  - `close()`: Marks the instance closed and ends event iteration.
- `FakeStaleResponseQwenClient`: Restartable variant used to simulate a stale upstream session continuing across turns.
  - `commit_audio()`: Emits assistant audio without completing the response so the next turn forces a restart.
- `FakeAppendRecoveringQwenClient`: Restartable variant that fails on the first append to test turn replay recovery.
  - `append_audio(pcm16_base64)`: Fails once on the first instance, then behaves like the base fake client.
  - `commit_audio()`: Emits assistant audio and a final done event after recovery.
- `FakeCommitRecoveringQwenClient`: Restartable variant that fails on the first commit to test turn replay recovery.
  - `commit_audio()`: Fails once on the first instance, then emits assistant audio and done after recovery.
- `FakeReconnectRefusedOnBufferedCommitQwenClient`: Restartable variant that refuses the second connect attempt to test retry loops around buffered commits.
  - `connect()`: Raises `ConnectionRefusedError` on the second instance only.
  - `commit_audio()`: Emits assistant audio and done when the connect retry eventually succeeds.
- `FakeWarmReusableQwenClient`: Variant that keeps the realtime session warm across completed turns so reuse can be asserted.
  - `__init__(**kwargs)`: Registers the instance and initializes close state for reuse assertions.
  - `commit_audio()`: Emits transcript, assistant audio, and done while keeping the session open.
  - `close()`: Marks the instance closed and ends event iteration.
- `FakeNeverFirstAudioQwenClient`: Variant that never emits assistant audio so first-audio timeout logic can be tested.
  - `__init__(**kwargs)`: Registers the instance and initializes close state for timeout assertions.
  - `commit_audio()`: Leaves the response open without emitting audio.
  - `close()`: Marks the instance closed.
  - `iter_events()`: Sleeps until cancelled so the response appears hung.
- `FakeAudioThenHangQwenClient`: Variant that emits one assistant audio delta and then never finishes so total-response timeout logic can be tested.
  - `commit_audio()`: Emits a first audio delta without a terminal done event.
  - `iter_events()`: Yields queued events and then hangs waiting for more.
- `FakeQwenChatClient`: Chat-client stub for text-only completions.
  - `__init__(**_kwargs)`: Initializes the recorded request list and close flag.
  - `respond_text_only(audio_pcm16, sample_rate)`: Records the audio request and returns a canned brief reply.
  - `close()`: Marks the fake client closed.
- `FakeStreamingQwenChatClient`: Chat-client stub for streaming assistant audio and text completions.
  - `__init__(**kwargs)`: Extends the base chat fake and records the latest instance.
  - `stream_audio_response(audio_pcm16, sample_rate, speaker)`: Yields a canned text delta, audio delta, and done event.

Functions
- `_valid_audio_base64()`: Builds a known-good base64 PCM16 input chunk for session tests.
- `_audio_base64_for_samples(sample_count)`: Builds a base64 PCM16 chunk with an exact sample count for turn-limit tests.
- `_track_test_session(session)`: Tracks open sessions so the autouse fixture can close them after each test.
- `_cleanup_test_sessions()`: Autouse async fixture that closes any sessions opened during a test.
- `_session_settings(**overrides)`: Creates a `Settings` object with optional overrides for test scenarios.
- `_start_audio_session(collector, settings=None)`: Creates a realtime-audio session, starts it, and returns the ready session.
- `_start_chat_stream_audio_session(collector)`: Creates a session configured to use the chat-stream audio backend.
- `_drain_tasks()`: Yields to the event loop a few times so background session tasks can finish.
- `_wait_for_event(collector, event_type, attempts=20)`: Waits for a specific event type to appear in the collector.
- `test_session_requires_event_sink()`: Confirms a realtime session cannot be created without an event sink.
- `test_session_returns_structured_error_when_qwen_is_unavailable(monkeypatch)`: Confirms startup failures become structured `QWEN_UNAVAILABLE` events.
- `test_session_streams_normalized_events(monkeypatch)`: Confirms a normal realtime turn emits session-ready, backend metadata, metrics, transcript, text, and audio events.
- `test_session_buffers_assistant_output_until_commit(monkeypatch)`: Confirms assistant output that arrives before commit is buffered until the commit opens the output gate.
- `test_session_accepts_internal_pcm16_audio_bytes(monkeypatch)`: Confirms the session can accept already-decoded PCM16 bytes and forward them upstream as base64 audio chunks.
- `test_session_caps_buffered_precommit_assistant_events(monkeypatch)`: Confirms the precommit assistant-event buffer stays bounded and drops lower-priority entries first.
- `test_session_emits_metrics_only_on_first_egress_and_turn_close()`: Confirms metrics updates are emitted only when first egress or turn closure actually changes the snapshot.
- `test_session_does_not_double_commit_when_upstream_response_already_started(monkeypatch)`: Confirms the session skips an explicit upstream commit when the response already started and finished pre-commit.
- `test_session_text_mode_uses_chat_completions(monkeypatch)`: Confirms text-only sessions use the chat-completions client and mark the non-realtime backend explicitly in `session.ready`.
- `test_session_audio_mode_uses_streaming_chat_completions(monkeypatch)`: Confirms chat-stream audio sessions call the streaming chat client and mark the fallback backend explicitly in `session.ready`.
- `test_session_cancel_restarts_upstream_qwen_session(monkeypatch)`: Confirms cancelling assistant output bumps output version and recreates the upstream realtime session.
- `test_session_reuses_warm_qwen_session_across_completed_turns(monkeypatch)`: Confirms a completed realtime session can be reused for the next turn without reconnecting.
- `test_session_times_out_when_first_audio_never_arrives(monkeypatch)`: Confirms the first-audio watchdog emits `QWEN_FIRST_AUDIO_TIMEOUT` and restarts the upstream session.
- `test_session_times_out_when_response_never_finishes(monkeypatch)`: Confirms the total-response watchdog emits `QWEN_RESPONSE_TIMEOUT` when assistant output hangs.
- `test_session_rejects_turn_audio_that_exceeds_configured_turn_limit(monkeypatch)`: Confirms audio beyond the configured turn-length cap is rejected with `TURN_AUDIO_LIMIT_EXCEEDED`.
- `test_session_drops_stale_audio_delta_by_output_version()`: Confirms stale assistant audio is dropped when its output version is older than the active one.
- `test_session_new_turn_restarts_stale_upstream_qwen_session(monkeypatch)`: Confirms a new turn rotates a stale realtime session that never finished its prior response.
- `test_session_recovers_turn_after_qwen_append_restart(monkeypatch)`: Confirms append failures on transient restart errors are recovered by reopening Qwen and replaying the turn audio.
- `test_session_recovers_turn_after_qwen_commit_restart(monkeypatch)`: Confirms commit failures on transient restart errors are recovered by reopening Qwen and replaying the committed turn.
- `test_session_retries_buffered_commit_after_qwen_connect_refused(monkeypatch)`: Confirms buffered-turn commit recovery retries through a refused reconnect and eventually succeeds.

## `apps/gateway/tests/test_stub_qwen_server.py`
Purpose: Verifies the standalone stub Qwen server script exposes the expected health, realtime, and streaming chat behavior.

Functions
- `_load_stub_server_module()`: Dynamically loads `scripts/stub_qwen_server.py` so its FastAPI app can be exercised directly in tests.
- `test_stub_qwen_health_endpoint_reports_ready()`: Confirms the stub server health endpoint reports the ready stub backend payload.
- `test_stub_qwen_realtime_endpoint_emits_audio_done_sequence()`: Confirms the realtime websocket emits the expected transcript, text, audio, and done sequence after a committed turn.
- `test_stub_qwen_chat_stream_endpoint_emits_text_and_audio_chunks()`: Confirms the streaming chat endpoint emits both text and audio chunks followed by the SSE `[DONE]` marker.

## `apps/gateway/tests/test_web_transport_debug_contract.py`
Purpose: Verifies the transport-debug UI contract in the web client stays flag-gated and non-blocking.

Functions
- `test_transport_debug_overlay_is_flag_gated()`: Confirms the debug overlay only activates behind the `transportDebug` query flag and appends events conditionally.
- `test_transport_debug_ui_uses_deferred_non_blocking_updates()`: Confirms the debug UI uses deferred React state and transitions for non-blocking updates.
- `test_transport_stats_sampler_is_flag_gated()`: Confirms assistant-track stats sampling only runs when transport debug mode is enabled.
- `test_metrics_hud_surfaces_rollups_and_operator_counters()`: Confirms the metrics HUD still surfaces latency rollups, counters, and reconnect summaries.
