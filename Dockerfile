# Pinned to match the FFmpeg version embedded in m3u-editor (Alpine edge ffmpeg).
# Bump both together.
FROM linuxserver/ffmpeg:8.1.2

# Install Python and system dependencies
RUN apt-get update && apt-get install -y \
    # Add common utilities
    pciutils \
    wget \
    nano \
    # Python dependencies
    python3 \
    python3-pip \
    python3-venv \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Deno (default JS runtime for yt-dlp's EJS challenge solver — no extra flags needed)
COPY --from=denoland/deno:latest /usr/bin/deno /usr/local/bin/deno

# Create symlink for python command
RUN ln -s /usr/bin/python3 /usr/bin/python

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN python3 -m pip install --no-cache-dir -r requirements.txt --break-system-packages

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
