# PrimeMover Orchestrator Dockerfile
#
# Build: docker build -t primemover-orchestrator .
# Run:   docker run --rm -it primemover-orchestrator --help
#
# For development with mounted code:
#   docker run --rm -it -v $(pwd):/app primemover-orchestrator --help

FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency files first (for better caching)
COPY pyproject.toml README.md ./

# Create minimal package structure for pip install
RUN mkdir -p orchestrator pmspec && \
    touch orchestrator/__init__.py pmspec/__init__.py

# Install Python dependencies
RUN pip install --no-cache-dir -e .

# Copy application code
COPY orchestrator/ ./orchestrator/
COPY pmspec/ ./pmspec/

# Copy additional files needed for runtime
COPY .env.example .env

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash orchestrator && \
    chown -R orchestrator:orchestrator /app

USER orchestrator

# Default entrypoint
ENTRYPOINT ["orchestrator"]
CMD ["--help"]
