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
        + _segment(3_840, 120)
        + _segment(1_200, 4)
        + _segment(3_840, -120)
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
    )

    assert analysis["status"] == "warning"
    assert analysis["edge_silence_runs_ge_threshold"] == 0
    assert analysis["internal_silence_runs_ge_threshold"] == 1
    assert analysis["longest_internal_silence_ms"] == 25.0
    assert analysis["suspicious_internal_silence_runs_ge_threshold"] == 1
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
    )

    assert analysis["status"] == "ok"
    assert analysis["internal_silence_runs_ge_threshold"] == 1
    assert analysis["longest_internal_silence_ms"] == 22.5
    assert analysis["suspicious_internal_silence_runs_ge_threshold"] == 0
    assert analysis["warnings"] == []
