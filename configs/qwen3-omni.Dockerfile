FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    VLLM_USE_V1=0

RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    git \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --upgrade pip setuptools wheel

RUN git clone -b qwen3_omni https://github.com/wangxiongts/vllm.git /opt/vllm

WORKDIR /opt/vllm

RUN python3 -m pip install -r requirements/build.txt \
    && python3 -m pip install -r requirements/cuda.txt

ENV VLLM_PRECOMPILED_WHEEL_LOCATION=https://wheels.vllm.ai/a5dd03c1ebc5e4f56f3c9d3dc0436e9c582c978f/vllm-0.9.2-cp38-abi3-manylinux1_x86_64.whl

RUN VLLM_USE_PRECOMPILED=1 python3 -m pip install -e . -v --no-build-isolation \
    || python3 -m pip install -e . -v

RUN python3 -m pip install git+https://github.com/huggingface/transformers \
    accelerate \
    qwen-omni-utils \
    flash-attn --no-build-isolation

WORKDIR /workspace
