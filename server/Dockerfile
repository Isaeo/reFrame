FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files from repo root
COPY pyproject.toml uv.lock ./

# Install dependencies only (no project install needed)
RUN uv sync --frozen --no-install-project --no-dev

COPY server/server.py .

EXPOSE 5001

CMD ["uv", "run", "gunicorn", "--bind", "0.0.0.0:5001", "--workers", "1", "--threads", "4", "server:app"]
