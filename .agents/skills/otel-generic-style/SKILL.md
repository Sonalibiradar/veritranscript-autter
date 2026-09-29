---
name: otel-generic-style
version: 1.2.1
description: Fallback guide for wiring Autter Runtime into any backend language/framework not covered by a dedicated style skill (Java, .NET, PHP, Ruby, Elixir, Kotlin, etc.) using standard OpenTelemetry — errors, usage, and LLM tracing.
tags: [autter, telemetry, opentelemetry, otlp, generic, llm]
author: autter
---

# Generic / any-language style

For continuous detection, enable available HTTP, dependency, database, and
background worker instrumentations. ERROR spans, exception events, and
HTTP 5xx are grouped without application logs. Report known bad normal
returns as an `autter.outcome` event with `autter.outcome.status=error`,
stable `autter.outcome.name`, and short `autter.outcome.message`. Supported
profilers can upload symbolized pprof to `/v1/profiles` with a server key;
any caught exception hook remains opt in because handled throws are noisy.

For memory pressure in any backend language, enable a process/runtime metric
instrument on the existing meter provider. Export **current** RSS or managed
heap as an observable gauge with a `service.instance.id` unique to one
process lifetime; use Runtime's portable names and units in
`docs/MEMORY-PRESSURE.md`. Export GC counters only when that runtime supports
them. An OTel exporter alone does not measure process memory. Forward
ECS/Kubernetes OOM and restart events with a server key; an OOM proves
exhaustion, not a leak. Self-hosted ingesters need 1.3.3+ for these signals.
Redeploy the application after adding its memory instrument. Metric-based
pressure detection can work without a platform forwarder; OOM/restart
correlation cannot. Check that the ingester and backend/frontend memory
changes are deployed before describing the feature as available to customers.

Autter Runtime's server ingest is standard **OTLP/HTTP** — nothing
Autter-specific to install for languages without a dedicated style skill.
Every mainstream OpenTelemetry SDK (Java, .NET, PHP, Ruby, Elixir, Kotlin,
Swift, C++, ...) can export to it out of the box.

## Step 1: Install that language's official OTel SDK

First inspect the installed SDK and reuse its providers and exporters. Do not add duplicate initialization.

### Endpoint regression requirements

The general environment setup below is not sufficient for endpoint detection on every SDK. Configure explicit-bucket **delta** HTTP duration histograms and an export interval of at most two minutes. Verify temporality settings against the installed language SDK. Do not assume that one exporter environment variable works in every language.

Include route templates, HTTP methods, the deployed commit SHA, stable service and environment names, and a unique service instance ID. Keep normal traces and add supported slow-request retention and dependency child spans where needed. `retainTracesAboveMs` applies only to the Node/Next.js SDK. Never calculate endpoint p95 from sampled traces.

Self-hosted ingesters require 1.3.1 or later. See the [telemetry contract](https://github.com/Autter-dev/autter-runtime/blob/main/docs/ENDPOINT-REGRESSIONS.md). The platform rollout does not update application settings. Fixes remain draft pull requests for human review. Use existing production telemetry for verification; run selftests only in an isolated test environment.

Search for `"<language> opentelemetry sdk"` if you don't already know the
package name — e.g.:

- Java: `io.opentelemetry:opentelemetry-sdk` + the Java agent
  (`opentelemetry-javaagent.jar`) for zero-code auto-instrumentation
- .NET: `OpenTelemetry`, `OpenTelemetry.Exporter.OpenTelemetryProtocol`,
  `OpenTelemetry.Instrumentation.AspNetCore`
- PHP: `open-telemetry/opentelemetry`, `open-telemetry/exporter-otlp`
- Ruby: `opentelemetry-sdk`, `opentelemetry-exporter-otlp`
- Elixir: `opentelemetry`, `opentelemetry_exporter`

## Step 2: Point it at Autter with standard env vars

This is the zero-code path and works identically across nearly every OTel
SDK, since these env vars are part of the OTel spec itself, not
per-language config:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp.autter.dev
OTEL_EXPORTER_OTLP_HEADERS="authorization=Bearer ${AUTTER_RUNTIME_KEY}"
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_SERVICE_NAME=<service name>
OTEL_RESOURCE_ATTRIBUTES=service.instance.id=${PROCESS_INSTANCE_ID},service.version=${GIT_SHA},deployment.environment.name=production
OTEL_TRACES_SAMPLER=parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG=0.01
```

For Java specifically, this pairs with the Java agent for fully
zero-code instrumentation:

```bash
java -javaagent:opentelemetry-javaagent.jar -jar app.jar
```

with the same env vars set in the process environment.

If the SDK doesn't respect `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` (some
older SDK versions default to gRPC on port 4317), fall back to
`http/json` — the ingester accepts both protobuf and JSON on
`/v1/traces` and `/v1/metrics`. Check that specific SDK's docs for the
protocol env var name if it differs.

Metrics ride the same env vars: SDKs with HTTP-server metric support
(the Java agent and .NET's AspNetCore instrumentation emit
`http.server.request.duration` out of the box) export to `/v1/metrics`
automatically. That histogram — or the older `http.server.duration` — is
the only instrument Autter folds into request rollups. The portable process
RSS/heap/GC metrics in `docs/MEMORY-PRESSURE.md` are stored separately for
memory incidents. Other custom instruments are not stored. If the ecosystem's
instrumentation doesn't emit it, say so to the user — their usage stats
will be trace-derived (1%-sampled) instead.

For JVM, `MemoryMXBean.getHeapMemoryUsage().getUsed()` supplies current heap
and `.getMax()` the runtime heap limit; `GarbageCollectorMXBean` supplies
cumulative collection count/time. For .NET, `Process.WorkingSet64` supplies
RSS; `GC.GetGCMemoryInfo()` supplies managed heap evidence. PHP, Ruby,
Elixir, Kotlin, Swift, and C++ can use their OTel meter API plus an OS or
runtime process collector. Preserve the metric meanings: virtual memory,
peak RSS, reserved heap, and allocation totals are not current RSS/heap.
Set `PROCESS_INSTANCE_ID` separately for each worker; a pod UID alone does
not distinguish container restarts or multiple workers. A missing RSS gauge
is acceptable when current heap usage is available; Runtime can detect heap
growth without RSS.

## Step 3: If the SDK needs explicit code instead of env vars

Every OTel SDK exposes roughly the same shape — find the equivalent of:

1. Create a `Resource` with `service.name` (and optionally
   `service.version`, `deployment.environment`).
2. Create a `TracerProvider`/`SdkTracerProvider` with:
   - An OTLP/HTTP span exporter pointed at
     `https://otlp.autter.dev/v1/traces`, with an `authorization: Bearer`
     header whose value is read from the `AUTTER_RUNTIME_KEY` env var at
     runtime — never a literal key in source.
   - A parent-based sampler with a 1% ratio for the root sampler (errors
     should bypass this — see Step 4).
3. Register that provider as the global tracer provider.
4. Enable that language's HTTP-framework auto-instrumentation package if
   one exists (nearly every ecosystem has one) so requests get spans
   without manual wiring.

## Step 4: Reporting errors

Regardless of language, the OTel data model is the same: an error surfaces
as an Autter issue when a span either:

- Records an exception event (that SDK's equivalent of
  `span.recordException(err)` / `span.record_exception(err)`), or
- Ends with an `ERROR` status
  (`span.setStatus(StatusCode.ERROR, message)`).

Find that language's method name for these two operations (they exist in
every OTel SDK) and call them in the top-level error handler / middleware
so all errors are captured centrally, rather than in every call site.

**Grouping**: recording the exception attaches its stack trace to the
event. Autter parses native stack formats server-side — JVM (Java, Kotlin,
Scala), .NET, and others — and groups by the top function and file, so the
same defect stays one issue across re-deploys (line numbers are ignored)
and two different defects with the same message stay separate. Keep the
stack on the event; do not strip or truncate it away. If a language is not
one Autter parses directly, a structured stack still groups by a templated
signature rather than collapsing every same-message error into one issue.

**Warnings**: add an `autter.severity` attribute (`"fatal" | "error" |
"warning" | "info"`) to the exception event. Autter stores warnings in the
same table as errors with that severity, so they group and aggregate
identically without inflating error counts. Use it for deprecations,
recoverable failures, and degraded-dependency paths; keep messages
PII-free and template-stable (numbers/ids are normalised out server-side
for grouping).

## Step 5: Sampling guidance

Keep successful-trace sampling low (~1%, `OTEL_TRACES_SAMPLER_ARG=0.01`).
Errors should be recorded regardless of the sampling decision — if that
language's SDK doesn't have a separate "always capture errors" tracer
(most don't expose this as a first-class feature the way Autter's own
Node package does), sending errors as their own always-on span with an
`AlwaysOn` sampler + a separate `TracerProvider` replicates the same
guarantee. Only do this if the user's error volume matters at their
current sample rate — for most services 1% sampling still surfaces errors
adequately once traffic is non-trivial, since errors correlate with a
particular request shape that recurs.

## Step 6: Instrumenting slow processes

Autter's dashboard flags processes that are slow AND repeating a lot
(performance incidents with an automated optimization analysis and, when
safe, an automated fix PR). HTTP routes are covered automatically via
unsampled request metrics; non-HTTP work — background jobs, queue
consumers, cron ticks — is only visible where a span exists. Wrap each
recurring unit of work in a span named after the job using that
language's OTel API. At a 1% ratio sampler these spans would mostly be
dropped — give job spans an always-on tracer (the same separate-provider
pattern from Step 5) or accept that their counts are a lower bound. Use
stable, low-cardinality span names; ids go in attributes.

## Step 7: LLM calls (when the service calls LLM APIs)

Autter recognises spans following the OTel GenAI semconv automatically
and records each as an LLM call — model, tokens, latency, USD cost
(estimated ingest-side unless the span reports `autter.llm.cost_usd`).
If an ecosystem GenAI instrumentation exists (Java agent ≥ 2.x covers
several SDKs), enable it; otherwise wrap each model call in a CLIENT
span named `"<operation> <model>"` (e.g. `chat gpt-5-mini`) carrying:

- `gen_ai.operation.name` — "chat", "embeddings", …
- `gen_ai.system` — "openai", "anthropic", …
- `gen_ai.request.model`, and after the response
  `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`
- optionally `autter.user_id` (opaque id) and `autter.llm.cost_usd`
  (exact cost, when the app computes it)

Never put prompts, completions, or PII in attributes. These spans must
**bypass the 1% sampler** or cost numbers are garbage: emit them from a
tracer on a separate always-on provider (the Step 5 pattern), or wrap the
root sampler to always sample spans with `gen_ai.*` creation attributes.

## Step 8: Selftest path (temporary — delete after verification)

Add a throwaway route — `/__autter-selftest` — that, using that
language's OTel API: starts a child span named `autter.selftest`, adds an
exception event on it (`exception.type: "Message"`, `exception.message:
"autter selftest"`, `autter.severity: "info"`), sets the span status to
ERROR (that status is what makes the ingester store the occurrence; on
the internal child span it doesn't mark the request as failed), ends it,
and — if the SDK exposes it — force-flushes the tracer and meter
providers before responding. Hitting the route also feeds the HTTP
duration histogram, so one request exercises both pipelines. For
services wired for LLM tracing (Step 7), also emit one fake LLM span from
the same route — name `chat autter-selftest`, `gen_ai.system` and
`gen_ai.request.model` both `autter-selftest`, 1 input + 1 output token,
`autter.llm.cost_usd: 0` — and return its trace id in the response; no
real model is touched. Never commit or deploy the route; it's
unauthenticated and triggers telemetry sends.

## Verify

1. Run the service with the real `AUTTER_RUNTIME_KEY` and these
   verification overrides — standard OTel spec env vars, so they work
   across SDKs; unset them afterwards:

   ```bash
   OTEL_TRACES_SAMPLER=parentbased_always_on   # beat the 1% sampling
   OTEL_METRIC_EXPORT_INTERVAL=5000            # don't wait 60s for metrics
   ```

2. `curl` the selftest route once and give it ~10s (immediate if the
   route force-flushes).
3. Check the SDK's own startup/export logs for auth errors — a 401 means
   the key is missing/malformed; connection errors usually mean an
   outbound network/proxy issue reaching `otlp.autter.dev`. No export
   errors means both `/v1/traces` and `/v1/metrics` were accepted.
4. Ground truth in the dashboard (~1–2 min): an `autter.selftest` span,
   one info-severity "autter selftest" issue, and request metrics for
   `/__autter-selftest` where the SDK emits the HTTP duration histogram.
   Traces arriving without metrics means the metrics side isn't wired or
   the SDK doesn't emit the instrument — flag which one to the user.
5. LLM-wired services: the fake `autter-selftest` call appears under
   **Runtime → LLM** (self-hosted: a `runtime_llm_calls` row with the
   returned trace id). Remember the selftest ran with sampling forced up —
   real LLM calls only survive the default 1% run with the Step 7
   exemption in place; double-check it before trusting this signal.
6. Trigger one real error too and confirm the SDK's exception-recording
   call fired on that span — the selftest proves transport, not your
   error-handler wiring.
7. **Delete the selftest route** and unset the override env vars.
