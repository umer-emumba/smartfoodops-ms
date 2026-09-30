FROM python:3.12-slim
RUN pip install --no-cache-dir uv

WORKDIR /workspace
COPY pyproject.toml /workspace/pyproject.toml
COPY libs /workspace/libs
COPY services/order-service /workspace/services/order-service

WORKDIR /workspace/services/order-service
RUN uv sync --no-dev
RUN chmod +x entrypoint.sh

EXPOSE 8000
CMD ["./entrypoint.sh"]
