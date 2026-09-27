"""OpenTelemetry wiring — traces and logs go to the OTLP collector at
OTEL_EXPORTER_OTLP_ENDPOINT (standard OTel env var, read by the exporters
themselves). A no-op if that's not set — nothing breaks running without a
collector.

ADK's own tool/LLM spans (google.adk.telemetry.tracing) already use the
standard opentelemetry.trace API with proper GenAI semantic-convention
attributes (gen_ai.tool.name, gen_ai.conversation.id, model, etc.) — nothing
to instrument there ourselves, just point the global TracerProvider
somewhere real and they show up. setup() should run once, as early in the
process as practical (module import time in main.py).

Attribute size: the collector's backend caps attribute values at 4KB, so
OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT is set here rather than left to truncate
downstream — the SDK enforces it on every span/log attribute itself.
"""

import logging
import os

from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

os.environ.setdefault("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "4096")  # backend caps attributes at 4KB

_configured = False


def enabled() -> bool:
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))


def setup(service_name: str = "velesha-api") -> None:
    global _configured
    if _configured or not enabled():
        return
    resource = Resource.create({"service.name": service_name})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)
    logging.getLogger().addHandler(LoggingHandler(logger_provider=logger_provider))
    # every stdlib `logging` call (uvicorn's, google_adk's own loggers) now also
    # ships as an OTLP log record — our own print(..., flush=True) lines don't,
    # left as-is since journalctl already covers them locally

    RequestsInstrumentor().instrument()  # ha_client/grocy_client/jellyfin_client all ride on requests
    _configured = True


def instrument_fastapi(app) -> None:
    if not enabled():
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(app)
