FROM python:3.12.14-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY apps/api apps/api
COPY apps/worker apps/worker
RUN pip install --no-cache-dir uv==0.12.24 && uv sync --frozen --no-dev
COPY alembic.ini ./
COPY scripts scripts
COPY analytics analytics
COPY infra/certs infra/certs
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --uid 10001 --create-home meridian && mkdir /data && chown -R meridian:meridian /app /data
USER meridian
CMD ["uvicorn", "meridian.main:app", "--host", "0.0.0.0", "--port", "8017", "--no-access-log"]
