FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_DISABLE_XET=1 \
    VLLM_USE_V1=0 \
    HF_HOME=/workspace/.cache/huggingface \
    TRANSFORMERS_CACHE=/workspace/.cache/huggingface

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu129
ARG VLLM_WHEEL_URL=https://github.com/vllm-project/vllm/releases/download/v0.20.2/vllm-0.20.2%2Bcu129-cp38-abi3-manylinux_2_31_x86_64.whl

RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --upgrade pip setuptools wheel \
    && python3 -m pip install \
        torch==2.11.0 \
        torchvision==0.26.0 \
        torchaudio==2.11.0 \
        --index-url ${TORCH_INDEX_URL} \
    && python3 -m pip install ${VLLM_WHEEL_URL} \
    && python3 -m pip install vllm-omni==0.20.0

WORKDIR /workspace
