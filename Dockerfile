FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management
RUN pip install uv

# Copy and install dependencies
COPY pyproject.toml uv.lock ./
# Note: In Option 1, we share the services folder. 
# We need to make sure the root pyproject.toml includes all necessary deps.
RUN uv sync --frozen

# Generate Prisma Client
COPY prisma ./prisma
RUN uv run prisma py generate --schema prisma/schema.prisma

# Copy service code
COPY services ./services

# Ensure PYTHONPATH is set so 'services' is importable
ENV PYTHONPATH=/app

# Default is worker, but will be overridden by docker-compose
CMD ["uv", "run", "celery", "-A", "services.common.celery_app", "worker", "--loglevel=info"]
