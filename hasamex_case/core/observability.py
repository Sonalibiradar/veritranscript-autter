import os
import uuid
import psutil
from typing import Optional

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.metrics._internal.instrument import Histogram
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_ON,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)


class LlmAwareSampler(Sampler):
    """Always sample GenAI / LLM spans; delegate everything else to the base ratio sampler."""

    def __init__(self, delegate: Sampler):
        self._delegate = delegate

    def should_sample(
        self,
        parent_context,
        trace_id,
        name,
        kind=None,
        attributes=None,
        links=None,
        trace_state=None,
    ):
        target = self._delegate
        if name.startswith(("chat ", "embeddings ", "gen_ai.", "ai.")) or any(
            str(key).startswith("gen_ai.") for key in (attributes or {})
        ):
            target = ALWAYS_ON
        return target.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return f"LlmAware({self._delegate.get_description()})"


_initialized = False


def init_observability(
    service_name: str = "hasamex-case", api_key: Optional[str] = None
) -> Optional[TracerProvider]:
    """Initialize OpenTelemetry SDK with OTLP exporters pointing to Autter Runtime."""
    global _initialized
    if _initialized:
        return trace.get_tracer_provider()

    if not api_key:
        api_key = os.environ.get("AUTTER_RUNTIME_KEY")

    if not api_key:
        # If no Autter Runtime key is configured, telemetry remains uninitialized
        return None

    # Construct service resource metadata
    resource = Resource.create({
        "service.name": os.environ.get("AUTTER_SERVICE_NAME", service_name),
        "service.instance.id": os.environ.get("AUTTER_RUNTIME_INSTANCE_ID") or str(uuid.uuid4()),
        "service.version": os.environ.get("GIT_SHA", os.environ.get("SERVICE_VERSION", "1.0.0")),
        "deployment.environment.name": os.environ.get("DEPLOYMENT_ENVIRONMENT", "production"),
    })
    headers = {"authorization": f"Bearer {api_key.strip()}"}

    # Trace Sampling: 1% for standard traces, but LlmAwareSampler forces 100% for GenAI calls.
    # Setting OTEL_TRACES_SAMPLER_ARG=1 forces 100% for all traces during verification.
    ratio = float(os.environ.get("OTEL_TRACES_SAMPLER_ARG", "0.01"))
    base_sampler = ParentBased(TraceIdRatioBased(ratio))
    sampler = LlmAwareSampler(base_sampler)

    # Tracer Provider & OTLP Span Exporter
    tracer_provider = TracerProvider(
        resource=resource,
        sampler=sampler,
    )
    span_exporter = OTLPSpanExporter(
        endpoint="https://otlp.autter.dev/v1/traces",
        headers=headers,
    )
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # Meter Provider & OTLP Metric Exporter with Delta temporality
    metric_exporter = OTLPMetricExporter(
        endpoint="https://otlp.autter.dev/v1/metrics",
        headers=headers,
        preferred_temporality={Histogram: AggregationTemporality.DELTA},
    )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                metric_exporter,
                export_interval_millis=60_000,
            )
        ],
    )
    metrics.set_meter_provider(meter_provider)

    # Continuous process memory pressure gauge (autter.process.memory.rss)
    try:
        process = psutil.Process()
        metrics.get_meter("autter-process-memory").create_observable_gauge(
            "autter.process.memory.rss",
            callbacks=[lambda options: [Observation(process.memory_info().rss)]],
            unit="By",
            description="Process resident set size memory",
        )
    except Exception as e:
        # psutil gauge failure should never crash application startup
        pass

    _initialized = True
    return tracer_provider


def capture_warning(name: str, message: str, exception_type: str = "Warning"):
    """Capture a warning with autter.severity='warning'."""
    tracer = trace.get_tracer("autter-warnings")
    with tracer.start_as_current_span(name) as span:
        span.add_event(
            "exception",
            {
                "exception.type": exception_type,
                "exception.message": message,
                "autter.severity": "warning",
            },
        )
        span.set_status(trace.Status(trace.StatusCode.ERROR, message))
