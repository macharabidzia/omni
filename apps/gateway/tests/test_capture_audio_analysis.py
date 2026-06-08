import array
import importlib.util
from pathlib import Path


def _load_analysis_module():
    repo_root = Path(__file__).resolve().parents[3]
    module_path = repo_root / "scripts" / "analyze_capture_audio.py"
    spec = importlib.util.spec_from_file_location("analyze_capture_audio", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _segment(sample_count: int, amplitude: int) -> list[int]:
    return [amplitude] * sample_count


def _repeat_upsample(values: list[int], factor: int) -> list[int]:
    upsampled: list[int] = []
    for value in values:
        upsampled.extend([value] * factor)
    return upsampled


def test_analyze_samples_ignores_edge_silence_runs() -> None:
    analysis_module = _load_analysis_module()
    sample_rate = 48_000
    samples = array.array(
        "h",
        _segment(2_400, 0)
        + _segment(240, 80)
        + _segment(1_200, 4)
        + _segment(4_800, 120)
        + _segment(2_400, 0),
    )

    analysis = analysis_module.analyze_samples(
        samples,
        sample_rate=sample_rate,
        input_path=Path("synthetic.wav"),
        expected_sample_rate=sample_rate,
        frame_ms=20,
        active_threshold=24,
        silence_threshold=8,
        max_internal_silence_ms=20.0,
        edge_padding_ms=60.0,
        context_window_ms=10.0,
        context_active_threshold=32,
        silence_context_rms_threshold=500.0,
    )

    assert analysis["status"] == "ok"
    assert analysis["edge_silence_runs_ge_threshold"] == 1
    assert analysis["longest_edge_silence_ms"] == 25.0
    assert analysis["internal_silence_runs_ge_threshold"] == 0
    assert analysis["suspicious_internal_silence_runs_ge_threshold"] == 0
    assert analysis["warnings"] == []


def test_analyze_samples_flags_internal_silence_runs() -> None:
    analysis_module = _load_analysis_module()
    sample_rate = 48_000
    samples = array.array(
        "h",
        _segment(2_400, 0)
        + _segment(3_840, 800)
        + _segment(1_200, 4)
        + _segment(3_840, -800)
        + _segment(2_400, 0),
    )

    analysis = analysis_module.analyze_samples(
        samples,
        sample_rate=sample_rate,
        input_path=Path("synthetic.wav"),
        expected_sample_rate=sample_rate,
        frame_ms=20,
        active_threshold=24,
        silence_threshold=8,
        max_internal_silence_ms=20.0,
        edge_padding_ms=60.0,
        context_window_ms=10.0,
        context_active_threshold=32,
        silence_context_rms_threshold=500.0,
    )

    assert analysis["status"] == "warning"
    assert analysis["edge_silence_runs_ge_threshold"] == 0
    assert analysis["internal_silence_runs_ge_threshold"] == 1
    assert analysis["longest_internal_silence_ms"] == 25.0
    assert analysis["suspicious_internal_silence_runs_ge_threshold"] == 1
    assert analysis["suspicious_internal_silence_runs"][0]["pre_rms"] >= 200
    assert analysis["suspicious_internal_silence_runs"][0]["post_rms"] >= 200
    assert analysis["warnings"] == [
        "suspicious internal silence run 25.0 ms exceeded 20.0 ms"
    ]


def test_analyze_samples_does_not_flag_low_energy_internal_pause() -> None:
    analysis_module = _load_analysis_module()
    sample_rate = 48_000
    samples = array.array(
        "h",
        _segment(2_400, 0)
        + _segment(2_400, 120)
        + _segment(480, 12)
        + _segment(1_080, 4)
        + _segment(480, 12)
        + _segment(2_400, -120)
        + _segment(2_400, 0),
    )

    analysis = analysis_module.analyze_samples(
        samples,
        sample_rate=sample_rate,
        input_path=Path("synthetic.wav"),
        expected_sample_rate=sample_rate,
        frame_ms=20,
        active_threshold=24,
        silence_threshold=8,
        max_internal_silence_ms=20.0,
        edge_padding_ms=60.0,
        context_window_ms=10.0,
        context_active_threshold=32,
        silence_context_rms_threshold=500.0,
    )

    assert analysis["status"] == "ok"
    assert analysis["internal_silence_runs_ge_threshold"] == 1
    assert analysis["longest_internal_silence_ms"] == 22.5
    assert analysis["suspicious_internal_silence_runs_ge_threshold"] == 0
    assert analysis["warnings"] == []


def test_analyze_samples_flags_rough_noisy_frames_and_click_spikes() -> None:
    analysis_module = _load_analysis_module()
    sample_rate = 48_000
    smooth = [800] * 2_400
    noisy = [1_800 if index % 2 == 0 else -1_800 for index in range(2_400)]
    noisy[600] = 24_000
    noisy[1_200] = -24_000
    samples = array.array(
        "h",
        _segment(2_400, 0) + smooth + noisy + smooth + _segment(2_400, 0),
    )

    analysis = analysis_module.analyze_samples(
        samples,
        sample_rate=sample_rate,
        input_path=Path("synthetic.wav"),
        expected_sample_rate=sample_rate,
        frame_ms=20,
        active_threshold=24,
        silence_threshold=8,
        max_internal_silence_ms=20.0,
        edge_padding_ms=60.0,
        context_window_ms=10.0,
        context_active_threshold=32,
        silence_context_rms_threshold=500.0,
        voiced_rms_threshold=200.0,
        roughness_threshold=1.0,
        zero_crossing_threshold=0.35,
        max_noisy_frame_ratio=0.12,
        click_derivative_threshold=12000.0,
        click_rms_multiplier=8.0,
        max_click_spikes_per_second=0.5,
    )

    assert analysis["status"] == "warning"
    assert analysis["noisy_frames"]["ratio"] > 0.12
    assert analysis["click_spikes"]["per_second"] > 0.5
    assert "rough/noisy voiced frame ratio" in analysis["warnings"][0]


def test_analyze_samples_normalizes_artifact_metrics_across_sample_rates() -> None:
    analysis_module = _load_analysis_module()
    sample_rate = 24_000
    smooth = [800] * 1_200
    noisy = [1_800 if index % 2 == 0 else -1_800 for index in range(1_200)]
    noisy[300] = 24_000
    noisy[600] = -24_000
    base = _segment(1_200, 0) + smooth + noisy + smooth + _segment(1_200, 0)
    native_samples = array.array("h", base)
    upsampled_samples = array.array("h", _repeat_upsample(base, 2))

    native_analysis = analysis_module.analyze_samples(
        native_samples,
        sample_rate=sample_rate,
        input_path=Path("native.wav"),
        expected_sample_rate=sample_rate,
        frame_ms=20,
        active_threshold=24,
        silence_threshold=8,
        max_internal_silence_ms=20.0,
        edge_padding_ms=60.0,
        context_window_ms=10.0,
        context_active_threshold=32,
        silence_context_rms_threshold=500.0,
        voiced_rms_threshold=200.0,
        roughness_threshold=1.0,
        zero_crossing_threshold=0.35,
        max_noisy_frame_ratio=0.12,
        click_derivative_threshold=12000.0,
        click_rms_multiplier=8.0,
        max_click_spikes_per_second=0.5,
        artifact_analysis_rate=24_000,
    )
    upsampled_analysis = analysis_module.analyze_samples(
        upsampled_samples,
        sample_rate=48_000,
        input_path=Path("upsampled.wav"),
        expected_sample_rate=48_000,
        frame_ms=20,
        active_threshold=24,
        silence_threshold=8,
        max_internal_silence_ms=20.0,
        edge_padding_ms=60.0,
        context_window_ms=10.0,
        context_active_threshold=32,
        silence_context_rms_threshold=500.0,
        voiced_rms_threshold=200.0,
        roughness_threshold=1.0,
        zero_crossing_threshold=0.35,
        max_noisy_frame_ratio=0.12,
        click_derivative_threshold=12000.0,
        click_rms_multiplier=8.0,
        max_click_spikes_per_second=0.5,
        artifact_analysis_rate=24_000,
    )

    assert native_analysis["artifact_analysis"]["effective_rate"] == 24_000
    assert upsampled_analysis["artifact_analysis"]["effective_rate"] == 24_000
    assert upsampled_analysis["artifact_analysis"]["normalization"] == "block_average_decimate_x2"
    assert abs(native_analysis["noisy_frames"]["ratio"] - upsampled_analysis["noisy_frames"]["ratio"]) < 0.01
    assert abs(
        native_analysis["click_spikes"]["per_second"]
        - upsampled_analysis["click_spikes"]["per_second"]
    ) < 0.05


def test_analyze_samples_defaults_artifacts_to_native_rate() -> None:
    analysis_module = _load_analysis_module()
    sample_rate = 48_000
    smooth = [800] * 2_400
    noisy_24k = [1_800 if index % 2 == 0 else -1_800 for index in range(1_200)]
    repeated_48k = _repeat_upsample(noisy_24k, 2)
    samples = array.array(
        "h",
        _segment(2_400, 0) + smooth + repeated_48k + smooth + _segment(2_400, 0),
    )

    analysis = analysis_module.analyze_samples(
        samples,
        sample_rate=sample_rate,
        input_path=Path("transport.wav"),
        expected_sample_rate=sample_rate,
        frame_ms=20,
        active_threshold=24,
        silence_threshold=8,
        max_internal_silence_ms=20.0,
        edge_padding_ms=60.0,
        context_window_ms=10.0,
        context_active_threshold=32,
        silence_context_rms_threshold=500.0,
        voiced_rms_threshold=200.0,
        roughness_threshold=1.0,
        zero_crossing_threshold=0.35,
        max_noisy_frame_ratio=0.12,
        click_derivative_threshold=12000.0,
        click_rms_multiplier=8.0,
        max_click_spikes_per_second=0.5,
    )

    assert analysis["artifact_analysis"]["effective_rate"] == 48_000
    assert analysis["artifact_analysis"]["normalization"] == "native"
