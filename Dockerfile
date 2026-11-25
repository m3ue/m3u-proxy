# Pinned to match the FFmpeg version embedded in m3u-editor (Alpine edge ffmpeg).
# Bump both together.
FROM linuxserver/ffmpeg:9.0.2

# Install system dependencies
RUN apt-get update && apt-get install -y \
    # Add common utilities
    pciutils \
    wget \
    nano \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Deno (default JS runtime for yt-dlp's EJS challenge solver — no extra flags needed)
COPY --from=denoland/deno:latest /usr/bin/deno /usr/local/bin/deno

# Python comes from uv (a standalone CPython build) instead of apt, so its version
# doesn't depend on the Ubuntu release the FFmpeg image is built on. Keep
# PYTHON_VERSION on the same minor as the python3 in m3u-editor's Alpine base
# image (Alpine 3.24: 3.14), which runs this same code embedded. The venv's bin
# is first on PATH so python3, streamlink and yt-dlp all resolve to it.
ARG PYTHON_VERSION=3.14
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH
RUN uv venv --python ${PYTHON_VERSION} /opt/venv

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN uv pip install --no-cache -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY main.py .
COPY .env.example .env

# Copy static files (logo and favicon)
COPY static/ ./static/

# Copy Docker scripts
COPY docker/ ./docker/

# Create directories
RUN mkdir -p /tmp/m3u-proxy-streams

# Make scripts executable
RUN chmod +x /app/docker/entrypoint.sh /app/docker/check-hwaccel.sh /app/docker/verify-hwaccel.sh

# Environment variables
ENV PYTHONPATH=/app

# Override the default entrypoint and run the application
ENTRYPOINT ["/app/docker/entrypoint.sh"]
