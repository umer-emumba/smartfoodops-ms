FROM python:3.12-slim
RUN pip install --no-cache-dir uv

WORKDIR /workspace
# uv.lock is copied so builds install exactly the locked versions (--frozen),
# not whatever resolves on the day of the build.
COPY pyproject.toml uv.lock /workspace/
COPY libs /workspace/libs
COPY services/notification-service /workspace/services/notification-service

WORKDIR /workspace/services/notification-service
RUN uv sync --frozen --no-dev --no-cache \
    && chmod +x entrypoint.sh

# Run the venv's binaries directly: no uv at runtime, so container start never
# re-resolves or installs anything. Non-root so a compromised process can't
# modify the image's code or dependencies.
ENV PATH="/workspace/.venv/bin:$PATH"
RUN useradd --system --uid 10001 app
USER app

EXPOSE 8000
CMD ["./entrypoint.sh"]
