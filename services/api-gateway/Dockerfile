FROM python:3.12-slim
RUN pip install --no-cache-dir uv

WORKDIR /workspace
# uv.lock is copied so builds install exactly the locked versions (--frozen),
# not whatever resolves on the day of the build.
COPY pyproject.toml uv.lock /workspace/
COPY libs /workspace/libs
COPY services/api-gateway /workspace/services/api-gateway

WORKDIR /workspace/services/api-gateway
RUN uv sync --frozen --no-dev --no-cache

# Run the venv's binaries directly: no uv at runtime, so container start never
# re-resolves or installs anything. Non-root so a compromised process can't
# modify the image's code or dependencies.
ENV PATH="/workspace/.venv/bin:$PATH"
RUN useradd --system --uid 10001 app
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
