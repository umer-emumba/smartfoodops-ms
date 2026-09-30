FROM python:3.12-slim
RUN pip install --no-cache-dir uv

WORKDIR /workspace
COPY pyproject.toml /workspace/pyproject.toml
COPY libs /workspace/libs
COPY services/restaurant-service /workspace/services/restaurant-service

WORKDIR /workspace/services/restaurant-service
RUN uv sync --no-dev
RUN chmod +x entrypoint.sh

EXPOSE 8000
CMD ["./entrypoint.sh"]
