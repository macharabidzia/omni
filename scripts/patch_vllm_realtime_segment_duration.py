#!/usr/bin/env python3
import argparse
from pathlib import Path


QWEN3_OMNI_IMPORT = "import asyncio\n"
QWEN3_OMNI_IMPORT_PATCH = "import asyncio\nimport os\n"
QWEN3_OMNI_SEGMENT = """        # Use a small segment size for low-latency streaming.
        segment_duration_s = 5.0
"""
QWEN3_OMNI_SEGMENT_PATCH = """        # Keep the upstream default at 5.0s unless explicitly overridden so
        # local runtime tuning can reduce realtime buffering without forking
        # the serving surface.
        segment_duration_s = float(
            os.environ.get("VLLM_QWEN_REALTIME_SEGMENT_DURATION_S", "5.0")
        )
        if segment_duration_s <= 0:
            segment_duration_s = 5.0
"""

QWEN3_ASR_IMPORT = "import asyncio\n"
QWEN3_ASR_IMPORT_PATCH = "import asyncio\nimport os\n"
QWEN3_ASR_SEGMENT = """        # Use a small segment size for low-latency streaming.
        segment_duration_s = 5.0
"""
QWEN3_ASR_SEGMENT_PATCH = """        # Keep the upstream default at 5.0s unless explicitly overridden so
        # local runtime tuning can reduce realtime buffering without forking
        # the serving surface.
        segment_duration_s = float(
            os.environ.get("VLLM_QWEN_REALTIME_SEGMENT_DURATION_S", "5.0")
        )
        if segment_duration_s <= 0:
            segment_duration_s = 5.0
"""


def _patch_file(
    *,
    path: Path,
    import_original: str,
    import_patched: str,
    segment_original: str,
    segment_patched: str,
) -> str:
    current = path.read_text(encoding="utf-8")
    updated = current

    if import_patched not in updated:
        if import_original not in updated:
            raise RuntimeError(f"Could not find import block in {path}")
        updated = updated.replace(import_original, import_patched, 1)

    if segment_patched not in updated:
        if segment_original not in updated:
            raise RuntimeError(f"Could not find segment_duration block in {path}")
        updated = updated.replace(segment_original, segment_patched, 1)

    if updated == current:
        return "already patched"

    path.write_text(updated, encoding="utf-8")
    return "patched"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Patch local vLLM/vLLM-Omni realtime models to make segment_duration_s env-configurable."
    )
    parser.add_argument("--venv-path", type=Path, required=True)
    args = parser.parse_args()

    venv_path = args.venv_path.resolve()
    qwen3_omni_path = (
        venv_path
        / "lib"
        / "python3.12"
        / "site-packages"
        / "vllm_omni"
        / "model_executor"
        / "models"
        / "qwen3_omni"
        / "qwen3_omni.py"
    )
    qwen3_asr_path = (
        venv_path
        / "lib"
        / "python3.12"
        / "site-packages"
        / "vllm"
        / "model_executor"
        / "models"
        / "qwen3_asr_realtime.py"
    )

    omni_result = _patch_file(
        path=qwen3_omni_path,
        import_original=QWEN3_OMNI_IMPORT,
        import_patched=QWEN3_OMNI_IMPORT_PATCH,
        segment_original=QWEN3_OMNI_SEGMENT,
        segment_patched=QWEN3_OMNI_SEGMENT_PATCH,
    )
    asr_result = _patch_file(
        path=qwen3_asr_path,
        import_original=QWEN3_ASR_IMPORT,
        import_patched=QWEN3_ASR_IMPORT_PATCH,
        segment_original=QWEN3_ASR_SEGMENT,
        segment_patched=QWEN3_ASR_SEGMENT_PATCH,
    )

    print(f"{qwen3_omni_path}: {omni_result}")
    print(f"{qwen3_asr_path}: {asr_result}")


if __name__ == "__main__":
    main()
