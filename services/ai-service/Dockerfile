FROM python:3.12-slim
RUN pip install --no-cache-dir uv

WORKDIR /workspace
# uv.lock is copied so builds install exactly the locked versions (--frozen),
# not whatever resolves on the day of the build.
COPY pyproject.toml uv.lock /workspace/
COPY libs /workspace/libs
# Dependencies and the embedding models first, the service's code after:
# a code change then rebuilds only the last layers, not the ~70 MB download.
COPY services/ai-service/pyproject.toml /workspace/services/ai-service/pyproject.toml

WORKDIR /workspace/services/ai-service
RUN uv sync --frozen --no-dev --no-cache --no-install-project

# The embedding models (docs/week4-plan.md B4-D3), baked into the image so a
# container never downloads at start-up; HF_HUB_OFFLINE makes sure of it.
# Readable by all: the download runs as root, the service as `app`.
# DENSE_MODEL/SPARSE_MODEL are also what app.config reads at runtime, so the
# models in the image and the ones the service loads can't drift apart.
ARG DENSE_MODEL=BAAI/bge-small-en-v1.5
ARG SPARSE_MODEL=Qdrant/bm25
ENV DENSE_MODEL=${DENSE_MODEL} \
    SPARSE_MODEL=${SPARSE_MODEL} \
    EMBEDDING_CACHE_DIR=/opt/fastembed
RUN /workspace/.venv/bin/python -c "import os; from fastembed import SparseTextEmbedding, TextEmbedding; c = os.environ['EMBEDDING_CACHE_DIR']; TextEmbedding(os.environ['DENSE_MODEL'], cache_dir=c); SparseTextEmbedding(os.environ['SPARSE_MODEL'], cache_dir=c)" \
    && chmod -R a+rX /opt/fastembed
ENV HF_HUB_OFFLINE=1

COPY services/ai-service /workspace/services/ai-service
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
