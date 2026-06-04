from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

import pytest
import torch


REPO_ROOT = Path(__file__).resolve().parents[3]
QWEN_PROCESSOR_PATH = (
    REPO_ROOT
    / ".venv-qwen"
    / "lib"
    / "python3.12"
    / "site-packages"
    / "vllm_omni"
    / "model_executor"
    / "stage_input_processors"
    / "qwen3_omni.py"
)

if not QWEN_PROCESSOR_PATH.exists():
    pytest.skip("qwen runtime stage processor not available", allow_module_level=True)

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_talker2code2wav_async_chunk():
    source = QWEN_PROCESSOR_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"def talker2code2wav_async_chunk\([\s\S]*?\n\n(?=def talker2code2wav\()",
        source,
    )
    if match is None:
        raise AssertionError("Could not locate talker2code2wav_async_chunk in patched qwen3_omni.py")

    namespace: dict[str, Any] = {
        "Any": Any,
        "OmniEngineCoreRequest": object,
        "torch": torch,
    }
    exec(match.group(0), namespace)
    return namespace["talker2code2wav_async_chunk"]


talker2code2wav_async_chunk = _load_talker2code2wav_async_chunk()


class _FakeTransferManager:
    def __init__(self, extra: dict[str, int]) -> None:
        self.connector = SimpleNamespace(config={"extra": extra})
        self.code_prompt_token_ids = defaultdict(list)
        self.put_req_chunk = defaultdict(int)


def _make_request(*, request_id: str = "req-1", finished: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        external_req_id=request_id,
        additional_information=None,
        is_finished=lambda: finished,
    )


def _make_pooling_output(frame_seed: int) -> dict[str, dict[str, torch.Tensor]]:
    return {
        "codes": {
            "audio": torch.tensor([[frame_seed + offset for offset in range(8)]], dtype=torch.long),
        }
    }


def _emit_frame(
    transfer_manager: _FakeTransferManager,
    request: SimpleNamespace,
    *,
    frame_seed: int,
    is_finished: bool = False,
) -> dict | None:
    payload = talker2code2wav_async_chunk(
        transfer_manager=transfer_manager,
        pooling_output=_make_pooling_output(frame_seed),
        request=request,
        is_finished=is_finished,
    )
    if payload is not None:
        transfer_manager.put_req_chunk[request.external_req_id] += 1
    return payload


def test_qwen3_omni_waits_for_regular_chunk_without_initial_override() -> None:
    transfer_manager = _FakeTransferManager(
        {
            "codec_chunk_frames": 4,
            "codec_left_context_frames": 25,
        }
    )
    request = _make_request()

    assert _emit_frame(transfer_manager, request, frame_seed=1) is None
    assert _emit_frame(transfer_manager, request, frame_seed=9) is None
    assert _emit_frame(transfer_manager, request, frame_seed=17) is None

    payload = _emit_frame(transfer_manager, request, frame_seed=25)

    assert payload is not None
    assert payload["meta"]["left_context_size"] == 0
    assert len(payload["codes"]["audio"]) == 32


def test_qwen3_omni_emits_first_chunk_after_initial_codec_chunk_frames() -> None:
    transfer_manager = _FakeTransferManager(
        {
            "codec_chunk_frames": 4,
            "initial_codec_chunk_frames": 1,
            "codec_left_context_frames": 25,
        }
    )
    request = _make_request()

    first_payload = _emit_frame(transfer_manager, request, frame_seed=1)

    assert first_payload is not None
    assert first_payload["meta"]["left_context_size"] == 0
    assert len(first_payload["codes"]["audio"]) == 8

    assert _emit_frame(transfer_manager, request, frame_seed=9) is None
    assert _emit_frame(transfer_manager, request, frame_seed=17) is None
    assert _emit_frame(transfer_manager, request, frame_seed=25) is None

    second_payload = _emit_frame(transfer_manager, request, frame_seed=33)

    assert second_payload is not None
    assert second_payload["meta"]["left_context_size"] == 1
    assert len(second_payload["codes"]["audio"]) == 40
