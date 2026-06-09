from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
WEB_APP_PATH = REPO_ROOT / "apps" / "web" / "src" / "App.tsx"
WEB_CLIENT_PATH = REPO_ROOT / "apps" / "web" / "src" / "realtime" / "client.ts"
METRICS_HUD_PATH = REPO_ROOT / "apps" / "web" / "src" / "components" / "MetricsHud.tsx"


def test_transport_debug_overlay_is_flag_gated() -> None:
    source = WEB_APP_PATH.read_text(encoding="utf-8")

    assert 'const override = new URLSearchParams(window.location.search).get("transportDebug");' in source
    assert 'return override === "1" || override === "true";' in source
    assert "<PerfOverlay" in source
    assert "enabled={transportDebugEnabled}" in source
    assert "{transportDebugEnabled ? (" in source
    assert "function appendDebugEvent(event: DebugEvent): void {" in source
    assert "if (!transportDebugEnabled) {" in source


def test_transport_debug_ui_uses_deferred_non_blocking_updates() -> None:
    source = WEB_APP_PATH.read_text(encoding="utf-8")

    assert 'import { startTransition, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";' in source
    assert "const deferredTranscriptText = useDeferredValue(transcriptText);" in source
    assert "const deferredAssistantText = useDeferredValue(assistantText);" in source
    assert "const deferredDebugEvents = useDeferredValue(debugEvents);" in source
    assert "startTransition(() => {" in source


def test_transport_stats_sampler_is_flag_gated() -> None:
    source = WEB_CLIENT_PATH.read_text(encoding="utf-8")

    assert 'debug_audio_metadata: this.isTransportDebugEnabled(),' in source
    assert "if (!this.isTransportDebugEnabled() || this.assistantRemoteTrack === null) {" in source
    assert "this.assistantTrackStatsIntervalId = window.setInterval(() => {" in source
    assert "void this.emitAssistantTrackStats(\"sample\");" in source


def test_metrics_hud_surfaces_rollups_and_operator_counters() -> None:
    source = METRICS_HUD_PATH.read_text(encoding="utf-8")

    assert "<h2>Latency Dashboard</h2>" in source
    assert "Speech Egress P95" in source
    assert "Commit to Qwen P95" in source
    assert "Queue Depth P95" in source
    assert "Duplicate Commits" in source
    assert "Reconnects" in source
