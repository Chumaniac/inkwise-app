FROM ghcr.io/astral-sh/uv:0.11.32 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy \
    LANGSMITH_TRACING=false ANONYMIZED_TELEMETRY=false
COPY pyproject.toml uv.lock README.md /app/
COPY src /app/src
RUN uv sync --frozen --no-dev --extra postgres --extra retrieval --extra kafka --extra language
COPY web /app/web
COPY teaching /app/teaching
RUN useradd --create-home --uid 10001 inkwise && mkdir -p /app/data/private /app/knowledge/releases \
    && chown -R inkwise:inkwise /app/data /app/knowledge
USER inkwise
EXPOSE 8766
CMD ["/app/.venv/bin/inkwise", "serve", "--host", "0.0.0.0", "--port", "8766"]
