# Tracing is set up before any other app module is imported: some
# instrumentation (PyMongo's) only sees clients created after it
# (sfo_observability.tracing).
from sfo_observability import setup_tracing

from app.config import settings

setup_tracing(settings.service_name)
