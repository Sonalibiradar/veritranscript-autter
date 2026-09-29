---
name: otel-python-style
version: 1.2.1
description: How to wire Autter Runtime into Python backends (FastAPI, Flask, Django, plain WSGI/ASGI) using the standard OpenTelemetry SDK — errors, usage, and LLM tracing; no Autter-specific package needed.
tags: [autter, telemetry, python, fastapi, flask, django, opentelemetry, llm]
author: autter
---

# Python style

For continuous detection, add database, outbound HTTP, and queue
instrumentations in addition to framework spans. A failed normal return can
be reported with an `autter.outcome` OTel event carrying
`autter.outcome.status=error`, a stable `autter.outcome.name`, and a short
`autter.outcome.message`; set the span status to ERROR. Symbolized pprof
from a supported profiler can be uploaded to `/v1/profiles`. The optional
`adapters/python/caught_exceptions.py` hook samples handled exceptions but
uses Python tracing and must be enabled explicitly for targeted diagnosis.

There is no `autter` Python package — Autter Runtime's ingester speaks
standard **OTLP/HTTP**, so any language with an OTel SDK works by pointing
it at the ingester. For Python, use the official `opentelemetry-sdk` +
`opentelemetry-exporter-otlp-proto-http` (or `-grpc`, if the user already
has a gRPC-friendly deployment — HTTP is simpler and what Autter's ingester
documents first).

## Install

Inspect existing initialization first. Reuse its providers and exporters; do not add a second SDK.

## Endpoint regression requirements

The general setup below also needs explicit-bucket **delta** HTTP duration histograms for endpoint detection. Configure the installed metric exporter for delta temporality; do not leave its cumulative default unchanged. Export at most two minutes apart. Set route templates, HTTP methods, the deployed commit SHA, stable service and environment names, and a unique service instance ID.

For memory pressure, the Python OTel exporter is only transport. Add a current
RSS gauge from `psutil` (or an existing process metrics instrument) to the
same meter provider using `autter.process.memory.rss`, unit `By`. Configure
this inside each worker after fork, so `service.instance.id` identifies one
process lifetime. See Runtime's `docs/MEMORY-PRESSURE.md` for the shared
contract, supported GC counters, and OOM event forwarding. Do not map
`resource.getrusage().ru_maxrss` or `tracemalloc` to current RSS.
Self-hosted ingesters need 1.3.3+ for memory signals.
Redeploy the Python workers after adding the gauge; an OTLP exporter alone
does not start measuring RSS. Metric-based detection works without a platform
forwarder, but OOM/restart correlation requires one that sends the matching
process instance ID to `/v1/platform-events`.

Keep normal trace sampling. Add slow-request retention only through a supported SDK or collector policy; `retainTracesAboveMs` is a Node/Next.js option, not a Python option. Capture database and dependency child spans for trace comparison. Do not infer p95 from sampled traces. The platform rollout does not change these application settings.

Self-hosted ingesters require 1.3.1 or later. See the [telemetry contract](https://github.com/Autter-dev/autter-runtime/blob/main/docs/ENDPOINT-REGRESSIONS.md). Draft fixes need human review. Verify with existing production telemetry, not artificial errors or requests. Run any selftest below only in an isolated test environment.

## Packages

```bash
pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
pip install psutil  # only when using the RSS gauge below
# Framework auto-instrumentation (pick what matches):
pip install opentelemetry-instrumentation-fastapi   # FastAPI
pip install opentelemetry-instrumentation-flask     # Flask
pip install opentelemetry-instrumentation-django    # Django
```

## Fastest path: zero-code env vars

If the user just wants it working with minimal code changes, use
`opentelemetry-instrument` (from `opentelemetry-distro`,
`pip install opentelemetry-distro opentelemetry-instrumentation`) as a
process wrapper, with standard OTel env vars:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp.autter.dev
OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer ${AUTTER_RUNTIME_KEY}"
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_SERVICE_NAME=<service name>
OTEL_RESOURCE_ATTRIBUTES=service.instance.id=${PROCESS_INSTANCE_ID},service.version=${GIT_SHA},deployment.environment.name=production
OTEL_TRACES_SAMPLER=parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG=0.01
```

```bash
opentelemetry-instrument python app.py
# or: opentelemetry-instrument gunicorn app:app
```

Only use the environment resource ID when the deployment supplies a distinct
`PROCESS_INSTANCE_ID` to **each worker**. With a prefork server and one shared
environment value, configure the resource after fork in worker startup instead.

This auto-instruments whatever frameworks it detects (FastAPI, Flask,
requests, urllib, psycopg2, etc.) with zero code changes. Prefer this when
the user wants the least invasive setup.

HTTP metrics ride along on this path: `opentelemetry-instrument`
also wires a meter provider from the same env vars, and the framework
instrumentations emit the `http.server.duration` histogram — the one
instrument Autter folds into unsampled request rollups (and what gives
the slow-process monitor accurate HTTP coverage). It does not guarantee a
process memory gauge; add the gauge below if no existing instrument emits it.

## Explicit setup (when the user wants code they can see/modify)

```python
# observability.py
import os
import uuid

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

def init_observability(service_name: str, api_key: str):
    resource = Resource.create({
        "service.name": service_name,
        "service.instance.id": os.environ.get("AUTTER_RUNTIME_INSTANCE_ID") or str(uuid.uuid4()),
        "service.version": os.environ.get("GIT_SHA", ""),
        "deployment.environment.name": os.environ.get("DEPLOYMENT_ENVIRONMENT", "production"),
    })
    headers = {"authorization": f"Bearer {api_key}"}

    # 1% of successful traces. Reading the ratio from the standard env var
    # lets a verification run force 100% (OTEL_TRACES_SAMPLER_ARG=1)
    # without touching code.
    ratio = float(os.environ.get("OTEL_TRACES_SAMPLER_ARG", "0.01"))
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(ratio)),
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint="https://otlp.autter.dev/v1/traces",
        headers=headers,
    )))
    trace.set_tracer_provider(provider)

    # Metrics are not optional: the framework instrumentors only emit the
    # http.server.duration histogram — what feeds Autter's unsampled
    # request rollups and slow-process monitor — once a meter provider is
    # registered. Without this block, usage stats degrade to 1%-sampled
    # trace rollups.
    metrics.set_meter_provider(MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(
            OTLPMetricExporter(
                endpoint="https://otlp.autter.dev/v1/metrics",
                headers=headers,
            ),
            export_interval_millis=60_000,
        )],
    ))
    return provider
```

Add this after the meter provider is registered in each worker. It uses the
same exporter and does not initialize another SDK:

```python
import psutil
from opentelemetry import metrics
from opentelemetry.metrics import Observation

process = psutil.Process()
metrics.get_meter("autter-process-memory").create_observable_gauge(
    "autter.process.memory.rss",
    callbacks=[lambda options: [Observation(process.memory_info().rss)]],
    unit="By",
)
```

For OOM correlation, set `AUTTER_RUNTIME_INSTANCE_ID` to a platform ID that
the ECS/Kubernetes event forwarder can report. Each worker needs a distinct
ID; a pod UID alone is insufficient for multiple workers or restarts.

Call `init_observability(...)` once per process, before it starts serving
traffic (after fork in prefork servers, or in an ASGI lifespan startup hook) — and
**before** the framework instrumentor runs, since instrumentors bind to
whatever providers are registered at instrument time.

**FastAPI**: `FastAPIInstrumentor.instrument_app(app)` after creating the
app — do this instead of hand-writing middleware.

**Flask**: `FlaskInstrumentor().instrument_app(app)` right after
`app = Flask(__name__)`.

**Django**: `DjangoInstrumentor().instrument()` before `django.setup()` /
at the top of `wsgi.py`/`asgi.py`.

## Reporting errors manually

Automatic instrumentation records exceptions on the current span
automatically for most frameworks. For code outside a request context
(background jobs, CLI scripts, Celery tasks), wrap manually:

```python
tracer = trace.get_tracer(__name__)

with tracer.start_as_current_span("job.process_payment") as span:
    try:
        do_work()
    except Exception as e:
        span.record_exception(e)
        span.set_status(trace.Status(trace.StatusCode.ERROR, str(e)))
        raise
```

Errors surface as Autter issues whenever a span records an exception
(`span.record_exception`) or ends with `ERROR` status — this is standard
OTel behavior, not something Autter needs configured separately.
`record_exception` attaches the traceback; Autter parses the Python stack
format server-side and groups by the top function and file, so the same
defect stays one issue across re-deploys (line numbers are ignored) and two
different defects that share a message stay separate. Keep the traceback on
the event; do not strip it.

## Reporting warnings

Autter stores warnings alongside errors with a `severity` column
(`fatal | error | warning | info`) — declared via the `autter.severity`
attribute on the exception event. Use this for warning-worthy paths
(deprecations, degraded dependencies, recoverable failures) so they can
be aggregated later without being counted as errors:

```python
with tracer.start_as_current_span("orders.legacy_lookup") as span:
    span.add_event("exception", {
        "exception.type": "DeprecationWarning",
        "exception.message": "Legacy /orders lookup used",
        "autter.severity": "warning",
    })
    span.set_status(trace.Status(trace.StatusCode.ERROR, "deprecated path"))
```

(The ERROR status is what makes the ingester pick it up; `autter.severity`
downgrades it to a warning in storage.) Keep messages as stable templates
— ids and numbers are normalised out server-side for grouping — and never
include PII.

## Instrumenting slow processes (jobs, consumers, crons)

Autter's dashboard flags processes that are slow AND repeating a lot
(performance incidents with an automated optimization analysis and, when
safe, an automated fix PR). HTTP routes are covered automatically via
unsampled request metrics; non-HTTP work is only visible where a span
exists. Wrap recurring units of work — Celery tasks, cron jobs, queue
consumers, batch scripts — in a span named after the job:

```python
with tracer.start_as_current_span("invoice.rebuild"):
    rebuild_invoices()
```

At the default 1% ratio sampler these job spans would mostly be dropped —
route them through an always-on tracer (a separate `TracerProvider` with
an `ALWAYS_ON` sampler, same pattern the error path uses) or accept that
counts are a lower bound. Use stable, low-cardinality span names; put ids
in attributes.

## LLM calls

Autter recognises spans following the OTel GenAI semconv (`gen_ai.*`
attributes) automatically and records each as an LLM call — model, tokens,
latency, USD cost (estimated ingest-side unless the span reports
`autter.llm.cost_usd`) — watched for spend spikes, failing models, and
budget breaches. If the service calls LLM APIs (deps: `openai`,
`anthropic`, `litellm`, `langchain`, `google-genai`, `boto3` Bedrock),
wire this alongside the rest.

**Emitting the spans.** Prefer the official GenAI instrumentations:

```bash
pip install opentelemetry-instrumentation-openai-v2   # openai client
# anthropic/bedrock/vertex equivalents exist under opentelemetry-instrumentation-*
```

Under `opentelemetry-instrument` they activate automatically; in explicit
setups call `OpenAIInstrumentor().instrument()` after
`init_observability(...)`. Leave
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` unset (default off) —
prompts/completions must not leave the service. For clients without an
instrumentation, a manual span with the semconv attributes does the same:

```python
from opentelemetry.trace import SpanKind

with tracer.start_as_current_span(
    "chat gpt-5-mini",
    kind=SpanKind.CLIENT,
    attributes={
        "gen_ai.operation.name": "chat",
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-5-mini",
        "autter.user_id": user_id,  # opaque id — never an email
    },
) as span:
    out = client.chat.completions.create(...)
    span.set_attribute("gen_ai.usage.input_tokens", out.usage.prompt_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", out.usage.completion_tokens)
```

**Sampling exemption — required.** At the 1% ratio sampler, 99% of LLM
spans are dropped and the cost numbers become garbage. Exempt GenAI spans
in the sampler (Autter's Node package does exactly this internally); in
`init_observability`, wrap the sampler:

```python
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Sampler

class LlmAwareSampler(Sampler):
    """Always sample GenAI spans; delegate everything else."""
    def __init__(self, delegate):
        self._delegate = delegate

    def should_sample(self, parent_context, trace_id, name, kind=None,
                      attributes=None, links=None, trace_state=None):
        target = self._delegate
        if name.startswith(("chat ", "embeddings ", "gen_ai.", "ai.")) or any(
            key.startswith("gen_ai.") for key in (attributes or {})
        ):
            target = ALWAYS_ON
        return target.should_sample(parent_context, trace_id, name, kind,
                                    attributes, links, trace_state)

    def get_description(self):
        return f"LlmAware({self._delegate.get_description()})"

# in init_observability:
#   sampler=LlmAwareSampler(ParentBased(TraceIdRatioBased(ratio)))
```

The zero-code `opentelemetry-instrument` path can't take a custom sampler
from env vars — for services where LLM tracing matters, use the explicit
setup (above) so the exemption exists; otherwise tell the user their LLM
calls are 1%-sampled.

## Selftest path (temporary — delete after verification)

A throwaway route that proves both pipelines in one hit. Add it, verify,
delete it — never commit or deploy it.

```python
# TEMPORARY autter selftest — delete after verification.
# (FastAPI shown; Flask/Django: same body in their route syntax.)
@app.get("/__autter-selftest")
def autter_selftest():
    from opentelemetry import metrics, trace

    tracer = trace.get_tracer("autter-selftest")
    with tracer.start_as_current_span("autter.selftest") as span:
        span.add_event("exception", {
            "exception.type": "Message",
            "exception.message": "autter selftest",
            "autter.severity": "info",
        })
        span.set_status(trace.Status(trace.StatusCode.ERROR, "autter selftest"))

    # LLM selftest — only when the service is wired for LLM tracing: one
    # fake call (no real model touched) proves gen_ai spans land.
    llm_trace_id = None
    with tracer.start_as_current_span("chat autter-selftest", attributes={
        "gen_ai.operation.name": "chat",
        "gen_ai.system": "autter-selftest",
        "gen_ai.request.model": "autter-selftest",
        "gen_ai.usage.input_tokens": 1,
        "gen_ai.usage.output_tokens": 1,
        "autter.llm.cost_usd": 0,
        "autter.selftest": True,
    }) as llm_span:
        llm_trace_id = format(llm_span.get_span_context().trace_id, "032x")

    trace.get_tracer_provider().force_flush()
    metrics.get_meter_provider().force_flush()
    return {"ok": True, "llm_trace_id": llm_trace_id}
```

The `force_flush()` calls make it deterministic — both exports fire
before the response returns, no waiting on batch/interval timers. The
ERROR status sits on the internal child span (that status is what makes
the ingester store the info-severity occurrence), not on the request
span, so the route doesn't show up as a failed request in traffic stats.

One catch: the selftest span is a child of the request span, so at the
default 1% sampling it's usually dropped before export. Force sampling up
for the verification run only:

```bash
OTEL_TRACES_SAMPLER_ARG=1 opentelemetry-instrument python app.py
# explicit setup: init_observability reads the same env var
```

## Verify

1. Start the app with `OTEL_TRACES_SAMPLER_ARG=1` set for this run.
2. `curl` the selftest route once — it returns `{"ok": true}` only after
   both force-flushes ran.
3. Check the process output: the OTLP exporters log failed exports
   (a `401` means the key is missing/wrong; connection errors mean
   egress to `otlp.autter.dev` is blocked). No export errors means both
   `/v1/traces` and `/v1/metrics` were accepted.
4. Ground truth in the dashboard (~1–2 min): an `autter.selftest` span,
   one info-severity "autter selftest" issue, and request metrics for
   `/__autter-selftest`. Traces arriving without metrics means the meter
   provider isn't registered (or the instrumentor ran before
   `init_observability`) — exactly the gap the selftest exists to catch.
5. LLM-wired services: the `llm_trace_id` call appears under **Runtime →
   LLM** as provider/model `autter-selftest` (self-hosted: a
   `runtime_llm_calls` row). Remember the selftest ran with sampling forced
   to 100% — real LLM calls only survive the default 1% run if the
   `LlmAwareSampler` exemption is in place, so double-check it's wired
   before trusting this signal.
6. Trigger one real exception too and confirm `span.record_exception`
   fired (wrap it manually per above if the automatic instrumentation
   doesn't cover that code path, e.g. a background job) — the selftest
   proves transport, not your error-handler wiring.
7. Delete the selftest route and drop the temporary env override.
