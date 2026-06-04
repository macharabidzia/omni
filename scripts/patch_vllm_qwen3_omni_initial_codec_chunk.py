#!/usr/bin/env python3

from __future__ import annotations

import argparse
from pathlib import Path


ORIGINAL = '''def talker2code2wav_async_chunk(
    transfer_manager: Any,
    pooling_output: dict[str, Any],
    request: OmniEngineCoreRequest,
    is_finished: bool = False,
):
    """
    Pooling version.
    """
    if not isinstance(pooling_output, dict):
        return None
    talker_codes = pooling_output.get("codes", {})
    if not isinstance(talker_codes, dict):
        return None
    code_predictor_codes = talker_codes.get("audio")
    if code_predictor_codes is None:
        return None

    connector = getattr(transfer_manager, "connector", None)
    raw_cfg = getattr(connector, "config", {}) or {}
    cfg = raw_cfg.get("extra", raw_cfg) if isinstance(raw_cfg, dict) else {}
    chunk_size_config = int(cfg.get("codec_chunk_frames", 25))
    left_context_size_config = int(cfg.get("codec_left_context_frames", 25))

    if code_predictor_codes is None:
        return None
    if isinstance(code_predictor_codes, torch.Tensor):
        if code_predictor_codes.numel() == 0:
            return None
    elif hasattr(code_predictor_codes, "__len__"):
        if len(code_predictor_codes) == 0:
            return None

    if isinstance(code_predictor_codes, torch.Tensor):
        if not code_predictor_codes.any():
            return None
    else:
        code_tensor = torch.tensor(code_predictor_codes, dtype=torch.long)
        if not code_tensor.any():
            return None

    codec_codes = code_predictor_codes.to(torch.long).transpose(0, 1).cpu().to(torch.long).reshape(-1).tolist()
    if sum(codec_codes) == 0:
        return None

    request_id = request.external_req_id
    transfer_manager.code_prompt_token_ids[request_id].append(codec_codes)
    length = len(transfer_manager.code_prompt_token_ids[request_id])

    chunk_length = length % chunk_size_config
    if chunk_length != 0 and not is_finished:
        return None

    context_length = chunk_length if chunk_length != 0 else chunk_size_config
    # ensure left context does not exceed available length
    left_context_size = max(0, min(length - context_length, left_context_size_config))
    end_index = min(length, left_context_size + context_length)

    codes = (
        torch.tensor(transfer_manager.code_prompt_token_ids[request_id][-end_index:])
        .transpose(0, 1)
        .reshape(-1)
        .tolist()
    )

    return {
        "codes": {"audio": codes},
        "meta": {"left_context_size": left_context_size, "finished": torch.tensor(is_finished, dtype=torch.bool)},
    }
'''


PATCHED = '''def talker2code2wav_async_chunk(
    transfer_manager: Any,
    pooling_output: dict[str, Any],
    request: OmniEngineCoreRequest,
    is_finished: bool = False,
):
    """
    Pooling version.
    """
    if not isinstance(pooling_output, dict):
        return None
    talker_codes = pooling_output.get("codes", {})
    if not isinstance(talker_codes, dict):
        return None
    code_predictor_codes = talker_codes.get("audio")
    if code_predictor_codes is None:
        return None

    connector = getattr(transfer_manager, "connector", None)
    raw_cfg = getattr(connector, "config", {}) or {}
    cfg = raw_cfg.get("extra", raw_cfg) if isinstance(raw_cfg, dict) else {}
    chunk_size_config = int(cfg.get("codec_chunk_frames", 25))
    left_context_size_config = int(cfg.get("codec_left_context_frames", 25))
    initial_chunk_size = int(cfg.get("initial_codec_chunk_frames", 0))

    additional_information = getattr(request, "additional_information", None)
    if (
        additional_information is not None
        and hasattr(additional_information, "entries")
        and "initial_codec_chunk_frames" in additional_information.entries
    ):
        entry = additional_information.entries["initial_codec_chunk_frames"]
        if entry.list_data is not None and len(entry.list_data) == 1:
            initial_chunk_size = int(entry.list_data[0])

    if chunk_size_config <= 0 or left_context_size_config < 0 or initial_chunk_size < 0:
        raise ValueError(
            f"Invalid codec chunk config: codec_chunk_frames={chunk_size_config}, "
            f"codec_left_context_frames={left_context_size_config}, "
            f"initial_codec_chunk_frames={initial_chunk_size}"
        )
    if initial_chunk_size > chunk_size_config:
        initial_chunk_size = chunk_size_config

    if isinstance(code_predictor_codes, torch.Tensor):
        if code_predictor_codes.numel() == 0:
            return None
    elif hasattr(code_predictor_codes, "__len__"):
        if len(code_predictor_codes) == 0:
            return None

    if isinstance(code_predictor_codes, torch.Tensor):
        if not code_predictor_codes.any():
            return None
    else:
        code_tensor = torch.tensor(code_predictor_codes, dtype=torch.long)
        if not code_tensor.any():
            return None

    codec_codes = code_predictor_codes.to(torch.long).transpose(0, 1).cpu().to(torch.long).reshape(-1).tolist()
    if sum(codec_codes) == 0:
        return None

    request_id = request.external_req_id
    transfer_manager.code_prompt_token_ids[request_id].append(codec_codes)
    length = len(transfer_manager.code_prompt_token_ids[request_id])
    chunk_id = transfer_manager.put_req_chunk[request_id]

    use_initial_chunk = chunk_id == 0 and 0 < initial_chunk_size < chunk_size_config
    if use_initial_chunk:
        if length < initial_chunk_size and not is_finished:
            return None
        context_length = min(length, initial_chunk_size)
    else:
        initial_coverage = initial_chunk_size if 0 < initial_chunk_size < chunk_size_config else 0
        adjusted_length = length - initial_coverage
        if adjusted_length <= 0:
            return None

        chunk_length = adjusted_length % chunk_size_config
        if chunk_length != 0 and not is_finished:
            return None
        context_length = chunk_length if chunk_length != 0 else chunk_size_config

    # ensure left context does not exceed available length
    left_context_size = max(0, min(length - context_length, left_context_size_config))
    end_index = min(length, left_context_size + context_length)

    codes = (
        torch.tensor(transfer_manager.code_prompt_token_ids[request_id][-end_index:])
        .transpose(0, 1)
        .reshape(-1)
        .tolist()
    )

    return {
        "codes": {"audio": codes},
        "meta": {"left_context_size": left_context_size, "finished": torch.tensor(is_finished, dtype=torch.bool)},
    }
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Patch the local vllm-omni qwen3_omni async codec stage so the first "
            "audio emission can use initial_codec_chunk_frames before the steady "
            "codec_chunk_frames cadence."
        )
    )
    parser.add_argument(
        "--venv-path",
        type=Path,
        default=Path(".venv-qwen"),
        help="Path to the Qwen virtualenv root.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    processor_path = (
        args.venv_path
        / "lib"
        / "python3.12"
        / "site-packages"
        / "vllm_omni"
        / "model_executor"
        / "stage_input_processors"
        / "qwen3_omni.py"
    )
    if not processor_path.exists():
        raise SystemExit(f"error: qwen3_omni.py not found at {processor_path}")

    current = processor_path.read_text(encoding="utf-8")
    if 'cfg.get("initial_codec_chunk_frames", 0)' in current:
        print(f"already patched: {processor_path}")
        return
    if ORIGINAL not in current:
        raise SystemExit("error: expected qwen3_omni async chunk block was not found")

    processor_path.write_text(current.replace(ORIGINAL, PATCHED, 1), encoding="utf-8")
    print(f"patched: {processor_path}")


if __name__ == "__main__":
    main()
