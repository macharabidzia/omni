# tasks.md — Low-latency continuous audio readiness plan

**Scope:** `apps/gateway` audio path based on the uploaded `apps/` Python overview from 2026-06-09.

**Goal:** keep the current direct WebRTC/LiveKit → gateway worker → Qwen realtime → LiveKit audio-output path, make it consistently smooth, and protect the already-fast warm path without overengineering.

**Primary latency target:** warm-session `speech_end_to_first_assistant_egress_ms <= 500 ms` at p95, measured from the final user speech frame accepted by the server/VAD to the first assistant audio frame captured into LiveKit.

**Secondary UX target:** browser-heard first audio should stay close to server egress. Track it separately because browser jitter buffering, network jitter, autoplay state, and device output can add delay outside the Python gateway.

**Definition of done:**

- Warm p95 `speech_end_to_first_assistant_egress_ms <= 500 ms`.
- Warm p50 `speech_end_to_first_assistant_egress_ms <= 350 ms`.
- `commit_to_qwen_first_audio_ms` is visible per turn and is the main upstream model budget.
- `qwen_first_audio_to_livekit_first_frame_ms <= 30 ms` p95.
- Assistant playout queue stays below the configured cap during normal responses.
- No audible click/pop regressions in captured assistant audio.
- No repeated reconnects, duplicate commits, stale assistant audio, or old output after interrupt.
- The browser, worker, and Qwen timestamps can be correlated by `session_id`, `participant_identity`, `turn_id`, and `output_version`.

**Local execution note (2026-06-09):**

- Items proven by repository code, config, docs, and safe tests are marked done below.
- Live-model latency proof, browser telemetry proof, and benchmark tasks remain open until a real Qwen backend is available.

---

## What is already good in the current overview

Keep these pieces. Do not rebuild them unless a benchmark proves they are the bottleneck.

- Settings already centralize environment-backed config and reject unsupported audio-rate / unsafe frame-queue settings.
- Health/readiness already separate shallow readiness from expensive deep inference probes with caching.
- LiveKit session auth/control already returns the browser token and room details.
- `AssistantAudioPublisher` already decodes model audio, resamples, trims silence, smooths artifacts, pads/fades the final frame, tracks queued duration, and warns on queue backpressure.
- `ParticipantBridgeSession` already owns turn lifecycle, preroll, server VAD, LiveKit audio ingestion, Qwen upload, interruption, stale output filtering, and control events.
- `RealtimeSession` already owns Qwen session lifecycle, websocket retries/restarts, output gating, model-backend selection, metrics, and structured errors.
- Tests already cover config validation, health, audio helpers, capture analysis, LiveKit output smoothing, LiveKit worker turn behavior, Qwen realtime parsing, session runtime, stale output, recovery, and the initial codec-chunk patch.

---

## P0 — lock the target and prove the current path

These tasks are the highest priority because they protect the already-fast path before changing code.

### Measurement and acceptance gates

- [x] **T001 — Define the exact latency contract in code and docs.**
  - Add constants or documented labels for `speech_end_to_first_assistant_egress_ms`, `commit_to_qwen_first_audio_ms`, `qwen_first_audio_to_livekit_first_frame_ms`, `livekit_queue_depth_ms`, and `browser_jitter_buffer_ms`.
  - Acceptance: every latency graph and test uses the same names.

- [x] **T002 — Add `turn_id` to every server event, log line, metric, and audio artifact.**
  - Keep existing `output_version`; add a monotonic `turn_id` starting at `1` per session.
  - Acceptance: one grep can reconstruct a full turn timeline.

- [x] **T003 — Extend `SessionMetrics` with a full hot-path timeline.**
  - Add timestamps for: `session_start_received`, `qwen_ws_connected`, `qwen_session_ready`, `vad_speech_start`, `first_user_audio_uploaded`, `last_user_audio_uploaded`, `vad_speech_end`, `commit_sent`, `qwen_first_transcript`, `qwen_first_text`, `qwen_first_audio`, `first_livekit_frame_captured`, `assistant_done`, `turn_closed`.
  - Acceptance: missing timestamps are explicit `null`, not silently omitted.

- [x] **T004 — Add derived latency fields.**
  - Compute: `vad_end_to_commit_ms`, `commit_to_qwen_first_audio_ms`, `qwen_first_audio_to_livekit_first_frame_ms`, `speech_end_to_first_assistant_egress_ms`, `speech_start_to_commit_ms`, `turn_total_ms`, `queue_depth_at_first_frame_ms`.
  - Acceptance: all values are non-negative and unit-tested.

- [x] **T005 — Add one JSON metrics event per turn, not per audio frame.**
  - Emit at assistant first audio and at turn done.
  - Acceptance: hot path does not log on every 10 ms / 20 ms frame.

- [x] **T006 — Add browser-side latency telemetry.**
  - Browser sends client timestamps for: LiveKit connected, mic track published, assistant track subscribed, first assistant track unmuted/playing, interrupt clicked.
  - Acceptance: server can correlate browser telemetry by `session_id` and `turn_id`.

- [x] **T007 — Add browser WebRTC `getStats()` sampler.**
  - Sample only 1x/sec in debug or perf mode.
  - Track inbound audio `jitterBufferDelay / jitterBufferEmittedCount`, packets lost, concealed samples, jitter, current RTT if available.
  - Acceptance: normal production mode does not spam data packets.

- [x] **T008 — Create `scripts/benchmark_realtime_turn.py`.**
  - Local status: the repo now has an in-process warm-turn benchmark that exercises the real worker/session/publisher path against a stub realtime backend and prints the latency timeline.
  - Acceptance: exits non-zero if p95 target fails after N warm iterations.

- [x] **T009 — Create `scripts/benchmark_qwen_ttfb.py`.**
  - Local status: the repo now has a Qwen realtime TTFB benchmark that defaults to a stub websocket and can target a live realtime URL when one is available.
  - Acceptance: proves whether latency is gateway-side or model-side.

- [x] **T010 — Create `scripts/benchmark_livekit_egress.py`.**
  - It feeds synthetic assistant PCM through the real `AssistantAudioPublisher` and measures first `capture_frame`, queue depth, and frame cadence.
  - Acceptance: output adapter p95 overhead stays under 30 ms.

- [x] **T011 — Add perf CI for warm-path regressions.**
  - Local status: the safe gateway pytest suite now runs stub-backed benchmark scripts with threshold gates for Qwen first audio, gateway turn p95, and LiveKit egress overhead. Real-backend/nightly proof still depends on external runner availability.
  - Acceptance: PRs fail if gateway-only first egress exceeds the configured threshold.

- [x] **T012 — Add a small latency dashboard.**
  - Minimum: p50/p95 for the core metrics, queue depth, error count, reconnect count, duplicate-commit count, interruption count.
  - Acceptance: one dashboard shows whether p95 > 500 ms is Qwen, VAD, queue, or browser/network.

### Do-not-regress tests

- [x] **T013 — Add a golden “no reconnect during normal turn” test.**
  - Acceptance: a normal second turn reuses the warm websocket unless the previous response is stale/unfinished or a configured restart is required.

- [x] **T014 — Add a golden “first audio before transcript UI noise” test.**
  - Acceptance: transcript and metrics events do not delay audio publishing or cause UI churn before first audio.

- [x] **T015 — Add a golden “barge-in clears all old audio” test.**
  - Acceptance: after hard interrupt, queued duration becomes zero, `output_version` increments, and old audio cannot be published.

- [x] **T016 — Add a golden “smooth cadence” test.**
  - Feed uneven model chunks, assert LiveKit frames are emitted at the configured frame size with no partial-frame jitter except final padded frame.

- [x] **T017 — Add a golden “browser stats debug mode only” test.**
  - Acceptance: browser stats are disabled by default and enabled via debug/perf flag.

---

## P0 — keep the hot path simple and direct

### Process and service layout

- [x] **T018 — Keep FastAPI out of the realtime media path.**
  - FastAPI should only handle token/session creation, health, readiness, diagnostics, and static metadata.
  - Acceptance: after token issuance, mic audio and assistant audio flow over LiveKit media, not through HTTP routes.

- [x] **T019 — Run the LiveKit worker as a separate process/service from the FastAPI API.**
  - Do not run the stateful worker inside multiple FastAPI workers.
  - Acceptance: API can scale independently; worker owns room connection and audio source lifecycle.

- [x] **T020 — Keep one active browser participant per worker room.**
  - Your overview already enforces this; keep it.
  - Acceptance: second participant gets a clear `ROOM_BUSY` path and cannot share audio state.

- [x] **T021 — Keep one `ParticipantBridgeSession` per active participant.**
  - Avoid a separate “conversation manager” layer unless multiple active participants per room becomes a real product requirement.
  - Acceptance: turn state remains local and easy to reason about.

- [x] **T022 — Keep one `RealtimeSession` per participant session.**
  - It owns Qwen runtime state, output version, retries, metrics, and selected backend.
  - Acceptance: no duplicate Qwen clients are created per turn unless recovery/restart requires it.

### Qwen realtime path

- [x] **T023 — Open/prewarm Qwen realtime websocket on `session.start`.**
  - Do not wait for VAD end to open the upstream socket.
  - Acceptance: `qwen_ws_connected` is normally before `vad_speech_start` or before `commit_sent`.

- [x] **T024 — Keep the upstream Qwen websocket warm across turns.**
  - Restart only on explicit cancel, stale previous response, transient send failure recovery, or close/error.
  - Acceptance: normal multi-turn conversation shows no per-turn reconnect.

- [x] **T025 — Keep realtime audio as the default backend.**
  - Chat-stream audio is a fallback/debug mode, not the normal fast path.
  - Acceptance: `session.ready.backend` clearly says `qwen_realtime_audio` for the production path.

- [x] **T026 — Keep Qwen input as PCM16 mono 16 kHz.**
  - Do not wrap realtime chunks as WAV; WAV is only for chat-completion fallback or offline tools.
  - Acceptance: realtime append path sends raw base64 PCM16 only.

- [x] **T027 — Use small, bounded input chunks.**
  - Start with the currently tested 80 ms chunk size; benchmark 40 ms only if p95 needs it.
  - Acceptance: chunk duration is explicit in settings and validated.

- [x] **T028 — Stream user audio to Qwen while speech is still ongoing.**
  - Your worker tests already cover this; keep it as a hard invariant.
  - Acceptance: append happens before VAD end for normal speech.

- [x] **T029 — Commit immediately on VAD end.**
  - No post-processing, no full-utterance re-upload, no separate STT pass on the hot path.
  - Acceptance: `vad_end_to_commit_ms <= 20 ms` p95.

- [x] **T030 — Limit model response length for realtime mode.**
  - Local status: the deployed Qwen runtime used by `scripts/run_qwen_model.sh` keeps stage-0 voice generation at `default_sampling_params.max_tokens: 16`, and the fallback chat/text path remains capped separately by `QWEN_TEXT_MAX_COMPLETION_TOKENS=64`.
  - Acceptance: realtime voice default produces short, speakable responses unless the user asks for long form.

- [x] **T031 — Keep the initial codec chunk patch covered by tests.**
  - This is a direct first-packet latency lever.
  - Acceptance: test verifies early first chunk behavior after dependency or runtime changes.

- [x] **T032 — Add reconnect counters and reasons.**
  - Count `startup`, `interrupt`, `stale_previous_response`, `append_recovery`, `commit_recovery`, `upstream_closed`, `shutdown`.
  - Acceptance: dashboard can explain reconnect spikes.

### VAD and turn-taking

- [x] **T033 — Keep server-side VAD as the turn owner.**
  - Manual browser start/commit remains disabled in normal mode.
  - Acceptance: no double ownership between browser VAD and server VAD.

- [ ] **T034 — Tune VAD for latency with real clips.**
  - Start target: `min_silence_duration_ms` around 120–180 ms, `min_speech_duration_ms` around 80–160 ms, with your existing preroll.
  - Acceptance: no frequent cutoffs, and `vad_end_to_commit_ms` remains low.

- [ ] **T035 — Keep a short preroll buffer.**
  - Target 200–300 ms max; enough to avoid clipping speech starts, not enough to add lag.
  - Local status: default server-VAD preroll is 200 ms and worker tests prove preroll frames flush into the live turn before commit, but captured first-phoneme proof is still pending.
  - Acceptance: first phoneme is not clipped in capture tests.

- [x] **T036 — Hard interrupt on new speech when assistant output is active or queued.**
  - Your overview already has this; keep it.
  - Acceptance: new user speech clears queued assistant audio and cancels/restarts upstream as designed.

- [x] **T037 — Keep duplicate commit suppression.**
  - Acceptance: VAD duplicate end events or close-spaced commits cannot produce two model responses.

- [x] **T038 — Keep stale output filtering by `output_version`.**
  - Acceptance: old Qwen audio/text from a previous interrupted output never reaches LiveKit or browser UI.

- [x] **T039 — Add turn-state transition logging at state changes only.**
  - Log `idle -> speaking -> committed -> assistant_streaming -> closed` with reasons.
  - Acceptance: no per-frame logs.

---

## P0 — smooth assistant audio output

### Rate and frame contract

- [x] **T040 — Freeze the production rate contract.**
  - Browser/LiveKit input consumed by worker: 16 kHz mono for Qwen.
  - Qwen realtime output: model native rate, typically 24 kHz.
  - LiveKit assistant publish: 48 kHz mono PCM frames.
  - Acceptance: settings reject accidental unsupported overrides.

- [x] **T041 — Use one output adapter only: `AssistantAudioPublisher`.**
  - It is the only place that decodes, resamples, trims, smooths, accumulates frames, and publishes to LiveKit.
  - Acceptance: no second resampler or silence trimmer exists elsewhere.

- [x] **T042 — Keep output frame size small and stable.**
  - Target 10 ms frames for LiveKit publish; benchmark 20 ms only if CPU/capture overhead requires it.
  - Acceptance: frame duration is configured once and validated.

- [x] **T043 — Keep LiveKit `AudioSource` queue bounded for latency.**
  - Target 150–200 ms queue in production; never leave the default 1000 ms for realtime voice.
  - Acceptance: startup validation rejects unsafe queue values.

- [x] **T044 — Convert queue warnings into a real safety policy.**
  - Today warnings are useful; add a cap action if queue exceeds threshold repeatedly.
  - Action options: stop enqueueing stale chunks after interrupt, drop old queued assistant chunks on barge-in, or fail fast in debug.
  - Acceptance: queue cannot grow into seconds of delayed speech.

- [x] **T045 — Keep RED enabled for assistant audio.**
  - RED costs bandwidth but helps avoid distracting audio glitches.
  - Acceptance: publish options test continues to assert RED enabled.

- [x] **T046 — Keep DTX disabled for assistant TTS.**
  - Avoid discontinuities and clipped low-energy speech unless a real bandwidth requirement proves otherwise.
  - Acceptance: publish options test continues to assert DTX disabled.

### Artifact prevention

- [x] **T047 — Keep streaming resampler state across chunks.**
  - Flush only at turn finalization.
  - Acceptance: no per-chunk resampler reset.

- [x] **T048 — Keep integer sample accounting.**
  - Avoid float drift when accumulating frame sizes.
  - Acceptance: long-response frame counts remain exact.

- [x] **T049 — Trim only safe leading silence.**
  - Remove turn-leading silence and post-start chunk-leading artifacts, but preserve intentional pauses.
  - Acceptance: current silence-trim tests stay green.

- [x] **T050 — Keep conditional boundary smoothing.**
  - Smooth only when it improves the transition.
  - Acceptance: tests cover both smoothing and “do not smooth when worse”.

- [x] **T051 — Keep isolated-spike smoothing.**
  - Acceptance: click-spike capture test stays green.

- [x] **T052 — Keep short internal pauses.**
  - Acceptance: natural short pauses are preserved after speech has started.

- [x] **T053 — Keep fade-in after trim and fade-out on final padded frame.**
  - Acceptance: no click/pop in synthetic boundary tests.

- [ ] **T054 — Add a real assistant-output golden WAV.**
  - Local status: still blocked on a real model output capture; synthetic artifact tests and analyzer coverage exist locally, but this task explicitly calls for a real assistant-output golden file.
  - Use one real model output capture with known good quality.
  - Acceptance: analyzer reports no new internal silence/click/noise warnings after code changes.

- [ ] **T055 — Capture and analyze output during manual perf runs.**
  - Local status: analyzer tooling, debug-only artifact logging, and stub-backed smoke tooling are in place, but this remains open until manual perf runs produce captured assistant audio from a live backend.
  - Enable artifact logs only in debug/perf mode.
  - Acceptance: production does not write audio artifacts by default.

---

## P1 — browser/client work that helps without replacing LiveKit

- [x] **T056 — Use LiveKit media tracks for mic and assistant playback.**
  - Do not build a parallel websocket PCM-upload path unless LiveKit cannot meet the target.
  - Acceptance: browser sends microphone as LiveKit audio track.

- [x] **T057 — Keep browser audio constraints explicit but simple.**
  - Request echo cancellation for two-way voice.
  - Request noise suppression/auto gain only if supported and measured as helpful for your users.
  - Acceptance: browser logs actual selected audio settings in debug mode.

- [x] **T058 — Avoid `MediaRecorder` for realtime turn audio.**
  - It batches and is not needed for a LiveKit media track path.
  - Acceptance: no `MediaRecorder` in hot-path code.

- [x] **T059 — Do not add `AudioWorklet` unless you need local metering or custom DSP.**
  - AudioWorklet is good for low-latency custom processing, but it is overengineering if LiveKit already publishes the mic track directly.
  - Acceptance: no AudioWorklet dependency in the basic voice path.

- [x] **T060 — Ensure assistant track is subscribed/render-ready before first response.**
  - Acceptance: browser joins room, subscribes to assistant track, and unlocks playback before user speaks.

- [x] **T061 — Handle browser autoplay/audio unlock clearly.**
  - Add a visible “Start voice” action that resumes audio context / allows playback before conversation begins.
  - Acceptance: first assistant audio is not lost due to autoplay restrictions.

- [ ] **T062 — Keep local UI state independent from audio playout.**
  - Transcript rendering, waveform rendering, and metrics panels must never block track playback.
  - Local status: transcript/text/debug rendering is now deferred, the debug panel is gated behind `transportDebug`, and stub-backed realtime smoke exists for no-model verification; browser p95 proof with repeated end-to-end runs still needs to be collected.
  - Acceptance: disabling UI effects does not change latency, and enabling them does not degrade p95.

- [x] **T063 — Add a simple browser perf overlay.**
  - Show connection state, assistant track subscribed, queue/jitter stats, last turn latency.
  - Acceptance: debug-only overlay; no heavy charting in hot path.

---

## P1 — data/control events

- [x] **T064 — Keep audio on media tracks, not data packets.**
  - Data packets/tracks are for control, telemetry, and state only.
  - Acceptance: no assistant PCM is sent to the browser over JSON/data events in production.

- [x] **T065 — Use reliable delivery for critical control.**
  - Critical: `session.ready`, `assistant.interrupted`, `assistant.done`, `error`, `room.busy`.
  - Acceptance: reliable flag is explicit for critical control packets.

- [x] **T066 — Use best-effort/unreliable delivery for noisy telemetry if supported by current API path.**
  - Telemetry: browser stats, frequent meters, debug-only queue samples.
  - Acceptance: telemetry loss cannot break conversation state.

- [x] **T067 — Keep pre-audio payload buffering.**
  - Do not let transcript/metrics packets jump ahead in a way that makes the UI feel laggy or out of sync with speech.
  - Acceptance: current worker behavior remains tested.

- [x] **T068 — Define a small public event schema.**
  - Types: `session.ready`, `turn.started`, `turn.committed`, `assistant.audio_started`, `assistant.done`, `assistant.interrupted`, `transcript.delta`, `metrics.update`, `error`, `room.busy`.
  - Acceptance: unknown internal Qwen event shapes never leak to the browser.

---

## P1 — code cleanup without heavy abstraction

- [x] **T069 — Add lightweight typed event contracts.**
  - Use `TypedDict` or dataclasses for internal runtime events.
  - Avoid Pydantic validation per audio frame/chunk on the hot path.
  - Acceptance: static typing improves clarity without per-chunk overhead.

- [x] **T070 — Keep Pydantic settings at process startup only.**
  - Acceptance: config is parsed once via cached settings.

- [ ] **T071 — Avoid unnecessary bytes copies.**
  - Use NumPy views/memoryviews where safe; decode base64 once; avoid re-encoding unless the browser debug path needs it.
  - Local status: the worker/session hot path now streams raw PCM bytes internally and only base64-encodes once at the Qwen boundary, but broader profiler proof for the remaining publisher-loop allocations is still pending.
  - Acceptance: profiler shows no large avoidable allocation in publisher loop.

- [x] **T072 — Keep resampling in one library/path.**
  - Acceptance: no inconsistent resampler quality or delay between modules.

- [x] **T073 — Make all hot-path queues bounded.**
  - Queues: VAD event queue, audio preroll, Qwen event pump, pending pre-audio payloads, assistant output queue.
  - Acceptance: backpressure is visible and cannot grow unbounded.

- [x] **T074 — Keep shutdown cancellation explicit.**
  - Acceptance: participant close cancels VAD task, audio stream task, Qwen pump task, and clears publisher queue.

- [x] **T075 — Add structured log context.**
  - Include `session_id`, `participant_identity`, `room`, `turn_id`, `output_version`, `backend`, and `reason`.
  - Acceptance: no manual string parsing needed.

- [x] **T076 — Remove or quarantine dead debug code.**
  - Keep debug capture and raw event dumps behind explicit flags.
  - Acceptance: production hot path has no unused branches doing work.

---

## P1 — readiness, recovery, and deployment

- [x] **T077 — Keep shallow `/ready` cheap.**
  - It should verify dependencies enough for routing without running full inference every probe.
  - Acceptance: normal readiness does not create load spikes.

- [x] **T078 — Keep deep diagnostics manual or TTL-cached.**
  - Deep Qwen inference probe is valuable but expensive.
  - Acceptance: deep probe has TTL cache and clear operator intent.

- [x] **T079 — Prewarm Qwen on deploy/startup.**
  - Local status: the gateway lifespan can run a retrying deep Qwen prewarm pass, and `scripts/run_gateway_dev.sh` enables it by default for deploy/dev startup flows.
  - Acceptance: first real user turn is not paying cold-start cost when possible.

- [x] **T080 — Keep services in the same region/network zone.**
  - Browser ↔ LiveKit region and worker ↔ Qwen latency both matter.
  - Acceptance: deployment docs specify region affinity.

- [x] **T081 — API worker scaling: scale API separately from audio workers.**
  - In Kubernetes/container setups prefer one Uvicorn process per API container and scale replicas; outside containers use explicit worker counts.
  - Acceptance: no accidental stateful LiveKit worker duplication via API worker count.

- [x] **T082 — Worker scaling: one active room/session per worker instance unless benchmarked otherwise.**
  - Keep concurrency simple until multi-room load proves safe.
  - Acceptance: deployment config maps room/session ownership clearly.

- [x] **T083 — Add GPU/model capacity notes.**
  - Track max concurrent Qwen realtime sessions per GPU, warm memory, and max output tokens.
  - Local status: `docs/vast.md` now includes the operator worksheet, default local voice token cap, and overload posture; measured per-GPU concurrency and warm-memory values still need real-model fills.
  - Acceptance: runbook says what happens when capacity is exceeded.

- [x] **T084 — Add graceful drain.**
  - Stop accepting new sessions, let active turn finish or interrupt cleanly, then close LiveKit/Qwen connections.
  - Acceptance: deploy restart does not leave ghost assistant tracks.

- [x] **T085 — Add timeout policy.**
  - Timeouts: Qwen connect, append, commit, first audio, total response, browser idle, participant disconnect.
  - Local status: connect retries plus websocket send, first-audio, total-response, drain timeout, and browser-idle cleanup are implemented and test-covered; participant disconnect already closes the session path.
  - Acceptance: every timeout sends a structured error and cleanup path.

- [x] **T086 — Add retry policy table.**
  - Retry connect/recover on transient upstream closure; do not retry invalid payload/config/auth errors.
  - Acceptance: code and docs agree on retryable error classes.

---

## P1 — security and production hygiene

- [x] **T087 — Keep LiveKit token grants minimal.**
  - Browser: join, publish mic, subscribe assistant, data as needed.
  - Worker: room join, publish assistant track, subscribe browser track, data.
  - Acceptance: token tests assert grants.

- [x] **T088 — Restrict CORS origins in production.**
  - Acceptance: no wildcard origins outside local/dev.

- [x] **T089 — Never log tokens or raw secrets.**
  - Acceptance: structured logs redact LiveKit/Qwen secrets.

- [x] **T090 — Gate debug raw events/audio artifact dumps.**
  - Acceptance: raw audio capture cannot be accidentally enabled in production.

- [x] **T091 — Add resource limits.**
  - Max participants, max turn duration, max queued audio, max audio chunk duration, max websocket message size.
  - Acceptance: invalid input fails fast with structured errors.

---

## P2 — optional only after p95 data says it is needed

These are not first-line tasks. Add them only if benchmarks show a real bottleneck.

- [ ] **T092 — Benchmark 40 ms Qwen input chunks vs 80 ms.**
  - Only keep 40 ms if it improves first audio without increasing overhead or backend instability.

- [ ] **T093 — Benchmark LiveKit output frame 20 ms vs 10 ms.**
  - Only switch if `capture_frame` overhead matters more than playout responsiveness.

- [ ] **T094 — Experiment with lower `AudioSource` queue values.**
  - Try 100 ms, then lower only in a controlled benchmark.
  - Acceptance: no underruns or jitter artifacts.

- [ ] **T095 — Add optional local/browser VAD only for UI hints.**
  - It must not own commit decisions unless server VAD is disabled.

- [ ] **T096 — Add AudioWorklet metering only if built-in LiveKit/browser stats are insufficient.**
  - Keep it debug-only or visual-only.

- [ ] **T097 — Add multi-room worker scheduling.**
  - Only after one-room-per-worker becomes too expensive and profiling proves safe shared concurrency.

- [ ] **T098 — Add advanced adaptive VAD thresholds.**
  - Only after collecting enough noisy/quiet real-world clips.

- [ ] **T099 — Add packet-loss simulation to nightly CI.**
  - Useful later; not required before baseline p95 is stable.

- [ ] **T100 — Add model-side streaming runtime tuning beyond initial codec chunk.**
  - Only after Qwen TTFB benchmark proves model generation is the bottleneck.

---

## Things to explicitly avoid

- [ ] **A001 — Do not add a message broker to the audio hot path.**
  - It adds latency and another failure mode. Use direct async calls inside the worker.

- [ ] **A002 — Do not send assistant audio to the browser as base64 JSON.**
  - LiveKit media track is the correct audio transport.

- [ ] **A003 — Do not add a second media pipeline beside LiveKit unless the LiveKit benchmark fails.**
  - A parallel websocket/WebAudio pipeline doubles complexity.

- [ ] **A004 — Do not run expensive deep readiness probes on every health check.**
  - It creates artificial load and can worsen latency.

- [ ] **A005 — Do not make both browser and server VAD commit turns.**
  - One owner only. Keep server VAD as the source of truth.

- [ ] **A006 — Do not log every audio chunk/frame in production.**
  - Use sampled metrics and one turn summary.

- [ ] **A007 — Do not add Pydantic validation around every audio chunk.**
  - Validate boundaries and settings; keep hot-path chunk handling lightweight.

- [ ] **A008 — Do not optimize blindly before measurement.**
  - Keep the current architecture and profile the actual p95 gap.

---

## Recommended implementation order

1. T001–T012: metrics, scripts, dashboard, gates.
2. T013–T017: do-not-regress tests.
3. T018–T032: prove Qwen warm path and no per-turn reconnect.
4. T033–T039: tune VAD and turn lifecycle.
5. T040–T055: lock smooth output and queue policy.
6. T056–T068: browser and control-event cleanup.
7. T069–T091: code hygiene, deployment, security.
8. T092–T100 only if p95 still misses target.

