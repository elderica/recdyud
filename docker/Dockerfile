# syntax=docker/dockerfile:1
#
# mirakc with recdyud (DY-UD200 tuner command).
#
#   git submodule update --init --recursive
#   docker build -f docker/Dockerfile -t recdyud-mirakc .
#
# The build context must be the repository root.

ARG MIRAKC_IMAGE=docker.io/mirakc/mirakc:debian
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12

FROM ${UV_IMAGE} AS uv

# Build in the same base image as the runtime so that glibc matches.
FROM ${MIRAKC_IMAGE} AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/recdyud/python \
    UV_PROJECT_ENVIRONMENT=/opt/recdyud/venv \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /src
COPY . .
RUN test -f vendor/libaribb25/aribb25/arib_std_b25.c -a -f vendor/libusb-cmake/CMakeLists.txt \
    || { echo 'vendor/ is empty: run "git submodule update --init --recursive"' >&2; exit 1; }
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
 && /opt/recdyud/venv/bin/python -c "import recdyud.b25 as b; b._load()"

FROM ${MIRAKC_IMAGE}
LABEL org.opencontainers.image.description="mirakc with recdyud for the DY-UD200 tuner"
COPY --from=build /opt/recdyud /opt/recdyud
RUN ln -s /opt/recdyud/venv/bin/recdyud /usr/local/bin/recdyud \
 && ln -s /opt/recdyud/venv/bin/recdyud-diag /usr/local/bin/recdyud-diag \
 && mkdir -p /var/lib/mirakc/epg \
 && recdyud --version
