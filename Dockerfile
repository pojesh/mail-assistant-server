# ---- Builder: install dependencies with uv into /app/.venv ----
FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# ---- Runtime: slim image, non-root user ----
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH" PORT=8080
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini entrypoint.sh ./
COPY alembic ./alembic
COPY app ./app
COPY prompts ./prompts
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh
USER appuser
EXPOSE 8080
CMD ["./entrypoint.sh"]
