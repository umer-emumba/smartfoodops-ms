FROM python:3.12-slim
RUN pip install --no-cache-dir uv

WORKDIR /workspace
COPY pyproject.toml /workspace/pyproject.toml
COPY libs /workspace/libs
COPY services/api-gateway /workspace/services/api-gateway

WORKDIR /workspace/services/api-gateway
RUN uv sync --no-dev

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
