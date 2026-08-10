# The image for the example fine-tune.
#
#   sparks submit --context ./examples --data ./examples/data \
#     --name lora-r16 -- python /app/lora_finetune.py --epochs 12
FROM python:3.12-slim

WORKDIR /app

# cu130 to match the box's driver (CUDA 13.0 on GB10, compute capability 12.1).
# A cu128 build has no kernels for sm_121 and falls back to the CPU silently,
# which looks like a very slow but healthy run.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu130 \
    torch==2.13.0

RUN pip install --no-cache-dir \
    "transformers==4.57.1" \
    "peft==0.18.0" \
    "accelerate==1.11.0"

# Weights are baked in rather than downloaded per run: a job container that
# reaches Hugging Face on every start turns an outage there into a failed run.
ENV HF_HOME=/opt/hf
RUN python -c "\
from transformers import AutoModelForCausalLM, AutoTokenizer; \
name='HuggingFaceTB/SmolLM2-135M'; \
AutoTokenizer.from_pretrained(name); \
AutoModelForCausalLM.from_pretrained(name)" \
 && chmod -R a+rX /opt/hf

# torch compiles Triton kernels at runtime, on the first backward pass, so the
# image needs a C compiler even though nothing here builds at image time. Late
# in the file on purpose: everything above it is gigabytes that a change here
# would otherwise force a rebuild and a re-push of.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*

# The job runs as whoever submitted it, not as root, so every path the run
# writes to has to be writable by an arbitrary uid. Without HOME, torch's
# Triton cache lands on /.triton and the run dies after the first backward
# pass. Offline because the weights are already here; it also stops the hub
# writing cache-miss markers into the read-only tree above.
ENV HOME=/tmp \
    TRITON_CACHE_DIR=/tmp/triton \
    HF_HUB_OFFLINE=1

# Pinned to a commit, not to a branch. Docker keys this layer on the text of
# the instruction, so a branch URL never changes and the cache serves whatever
# sparks it first built with, however far main has moved. That failed a run
# with ImportError on a name main had had for an hour. Its own layer, below
# everything expensive, so bumping it rebuilds seconds rather than gigabytes.
RUN pip install --no-cache-dir \
    "https://github.com/vtemian/sparks/archive/4a1c25f51a41586d0606e5be9ec1bead15d2ae3b.tar.gz"

COPY *.py /app/
