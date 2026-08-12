# The queue runner, and nothing else.
#
# This image is infrastructure, not a base for training jobs. A job brings its
# own image built from its own Dockerfile, and this one starts it via the Docker
# SDK (PyPI `docker`) talking to the host daemon through the socket mounted at
# runtime by sparkup.
#
# It deliberately does NOT contain torch, CUDA or anything a model needs. The
# whole point of supervision living outside the training container is that a
# project's image stays a project's image.

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build

# uv resolves and installs into a venv we then copy wholesale, which keeps the
# build tooling out of the image that ships.
COPY --from=ghcr.io/astral-sh/uv:0.9.2 /uv /usr/local/bin/uv

WORKDIR /src
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# Dependencies first, as their own layer: they change far less often than the
# source, and rebuilding them on every commit is the difference between a
# 4-second image and a 90-second one.
COPY pyproject.toml uv.lock PYPI.md ./
RUN uv sync --locked --no-dev --no-install-project --no-editable

COPY src/ ./src/
# --no-editable, or the venv gets a link back to /src, which does not exist in
# the image that ships. It fails at import, not at build.
RUN uv sync --locked --no-dev --no-editable


FROM python:${PYTHON_VERSION}-slim

RUN set -eux; \
    apt-get update; \
    # rsync: how a retry clones an existing job's data tree on the box.
    # ca-certificates: for pulling job images from the local registry over HTTPS
    # mirrors if the daemon is configured that way; cheap either way.
    apt-get install -y --no-install-recommends rsync ca-certificates; \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# NVML, for the GPU energy counter. `utility` is what nvidia-container-runtime
# needs to inject libnvidia-ml; without it the sampler degrades to zero and
# every run records unmeasured energy, silently.
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=utility

# Unbuffered, because this process's whole output is a log somebody reads while
# waiting to find out what their job is doing.
ENV PYTHONUNBUFFERED=1

# Root, and not by oversight: the socket is root-owned, and the runner drops to
# each job's own uid before starting it. See engine.credentials.
USER root

ENTRYPOINT ["fire"]
CMD []
