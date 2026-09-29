"""OpenTelemetry wiring — traces and logs go to the OTLP collector at
OTEL_EXPORTER_OTLP_ENDPOINT (standard OTel env var, read by the exporters
themselves). A no-op if that's not set — nothing breaks running without a
collector.

ADK's own tool/LLM spans (google.adk.telemetry.tracing) carry GenAI
semantic-convention attributes (gen_ai.tool.name, gen_ai.conversation.id,
model, etc.) — enough for MLflow. Phoenix needs OpenInference attributes
instead, so GoogleADKInstrumentor adds those on top (see setup()). setup() should run once, as early in the
process as practical (module import time in main.py).

Attribute size: OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT is set to 32KB — 4KB cut
the long system prompt in llm.input_messages.*. The SDK enforces it on every
span/log attribute itself; setdefault, so the env var still overrides it.
"""

import logging
import os

from openinference.instrumentation.google_adk import GoogleADKInstrumentor
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

os.environ.setdefault("OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT", "32768")  # 4KB cut the system prompt (ADR-0059)

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
    # Phoenix only understands OpenInference attributes (openinference.span.kind,
    # input.value, llm.input_messages, ...), not the gen_ai.* ones MLflow reads.
    # This swaps ADK's own tracer for one that emits OpenInference spans while
    # still running ADK's trace_call_llm/trace_tool_call, so each span carries
    # both sets and both backends parse it.
    GoogleADKInstrumentor().instrument(tracer_provider=tracer_provider)
    _configured = True


def instrument_fastapi(app) -> None:
    if not enabled():
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(app)
