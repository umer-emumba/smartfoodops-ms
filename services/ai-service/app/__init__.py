# Turns on OpenTelemetry tracing for this process: names it in Jaeger and
# starts sending spans there. Done first, before any other app module is
# imported, because some instrumentation (PyMongo's) only sees clients created
# after it (sfo_observability.tracing).
from sfo_observability import setup_tracing

from app.config import settings

setup_tracing(settings.service_name)
