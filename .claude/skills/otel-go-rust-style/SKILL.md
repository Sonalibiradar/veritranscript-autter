---
name: otel-go-rust-style
version: 1.2.1
description: How to wire Autter Runtime into Go and Rust backends using each language's official OpenTelemetry SDK — errors, usage, and LLM tracing; no Autter-specific package needed.
tags: [autter, telemetry, go, rust, opentelemetry, llm]
author: autter
---

# Go / Rust style

For continuous detection, instrument outbound requests, database calls,
and queue or job work, not only the inbound router. Mark failed spans ERROR
and record exceptions where the SDK supports it; HTTP 5xx is grouped even
without an exception event. For a known bad normal return, add an
`autter.outcome` event with `autter.outcome.status=error`, stable
`autter.outcome.name`, and short `autter.outcome.message`. A supported
profiler may send symbolized pprof to `/v1/profiles` with a server key and
service, environment, release headers. Caught exception sampling is opt in.

There is no Autter package for Go or Rust — the ingester speaks standard
OTLP/HTTP, so each language's own OTel SDK talks to it directly.

## Go

Before setup, inspect and reuse existing providers and exporters. Do not initialize another SDK.

## Endpoint regression requirements

For Go and Rust, configure the installed metric exporter's temporality selector for **delta**, not cumulative. Use explicit HTTP duration buckets and an export interval of at most two minutes. The general metric examples below need this exporter configuration before endpoint detection can use them. Check the API for the installed SDK version rather than assuming a shared environment variable.

For memory pressure, use that same metric exporter and a unique
`service.instance.id` for each process lifetime. Go's `runtime.MemStats`
provides current `HeapAlloc` plus cumulative `NumGC` and `PauseTotalNs`;
it does not provide process RSS or a container memory limit. Rust needs a
process/allocator collector appropriate to the application. Emit the
portable names and units in Runtime's `docs/MEMORY-PRESSURE.md` and forward
platform OOM/restart events with the server key. Memory sums may be delta or
cumulative; the delta requirement above applies to endpoint histograms.
Self-hosted ingesters need 1.3.3+ for memory signals.
Redeploy the Go/Rust service after wiring those instruments; exporter
configuration alone does not produce process metrics. OOM/restart correlation
requires an external ECS/Kubernetes event forwarder that reports the same
process instance ID to `/v1/platform-events`.

Include route templates, HTTP methods, the deployed commit SHA, stable service and environment names, and a unique service instance ID. Keep normal traces and configure supported slow-request retention where needed. The Node/Next.js `retainTracesAboveMs` option does not apply to Go or Rust. Add dependency child spans; do not calculate endpoint p95 from sampled traces.

Self-hosted ingesters require 1.3.1 or later. See the [telemetry contract](https://github.com/Autter-dev/autter-runtime/blob/main/docs/ENDPOINT-REGRESSIONS.md). The platform rollout does not change application settings. Fixes remain draft pull requests for human review. Use existing production telemetry for verification; run selftests only in an isolated test environment.

## Go packages

```bash
go get go.opentelemetry.io/otel \
       go.opentelemetry.io/otel/sdk \
       go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp
```

```go
import (
    "context"
    "os"

    "go.opentelemetry.io/otel"
    "go.opentelemetry.io/otel/attribute"
    "go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
    "go.opentelemetry.io/otel/sdk/resource"
    sdktrace "go.opentelemetry.io/otel/sdk/trace"
    semconv "go.opentelemetry.io/otel/semconv/v1.24.0"
)

func initObservability(ctx context.Context, serviceName string) (func(context.Context) error, error) {
    exp, err := otlptracehttp.New(ctx,
        otlptracehttp.WithEndpointURL("https://otlp.autter.dev/v1/traces"),
        otlptracehttp.WithHeaders(map[string]string{
            "authorization": "Bearer " + os.Getenv("AUTTER_RUNTIME_KEY"),
        }),
    )
    if err != nil {
        return nil, err
    }
    res, _ := resource.New(ctx, resource.WithAttributes(
        semconv.ServiceName(serviceName),
        attribute.String("service.instance.id", os.Getenv("AUTTER_RUNTIME_INSTANCE_ID")),
        attribute.String("service.version", os.Getenv("GIT_SHA")),
        attribute.String("deployment.environment.name", os.Getenv("DEPLOYMENT_ENVIRONMENT")),
    ))
    tp := sdktrace.NewTracerProvider(
        sdktrace.WithBatcher(exp),
        sdktrace.WithResource(res),
        sdktrace.WithSampler(sdktrace.ParentBased(sdktrace.TraceIDRatioBased(0.01))), // 1%
    )
    otel.SetTracerProvider(tp)
    return tp.Shutdown, nil
}
```

Call `initObservability(ctx, "my-service")` at process start, and call the
returned shutdown func on graceful termination.

**HTTP auto-instrumentation**: wrap the router/mux with
`go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp` (or the
framework-specific contrib package — e.g. `otelgin` for Gin, `otelecho` for
Echo) so every request gets a span without manual wiring.

**Reporting errors**:

```go
span := trace.SpanFromContext(ctx)
span.RecordError(err)
span.SetStatus(codes.Error, err.Error())
```

Do this in error-handling middleware so every handler gets it for free,
rather than sprinkling it through business logic.

**Reporting warnings** — same mechanism, plus an `autter.severity`
attribute on the exception event; Autter stores it in the errors table
with `severity: warning` so it groups/aggregates like an error without
being counted as one:

```go
span.AddEvent("exception", trace.WithAttributes(
    attribute.String("exception.type", "DeprecationWarning"),
    attribute.String("exception.message", "legacy /orders lookup used"),
    attribute.String("autter.severity", "warning"), // fatal|error|warning|info
))
span.SetStatus(codes.Error, "deprecated path") // ERROR status makes the ingester pick it up
```

## Rust

```toml
# Cargo.toml
opentelemetry = "0.30"
opentelemetry_sdk = "0.30"
opentelemetry-otlp = { version = "0.30", features = ["http-proto"] }
```

```rust
use opentelemetry_otlp::WithExportConfig;
use std::collections::HashMap;

fn init_observability(service_name: &str) -> anyhow::Result<opentelemetry_sdk::trace::TracerProvider> {
    let exporter = opentelemetry_otlp::SpanExporter::builder()
        .with_http()
        .with_endpoint("https://otlp.autter.dev/v1/traces")
        .with_headers(HashMap::from([(
            "authorization".to_string(),
            format!("Bearer {}", std::env::var("AUTTER_RUNTIME_KEY")?),
        )]))
        .build()?;

    let provider = opentelemetry_sdk::trace::TracerProvider::builder()
        .with_batch_exporter(exporter, opentelemetry_sdk::runtime::Tokio)
        .with_sampler(opentelemetry_sdk::trace::Sampler::ParentBased(Box::new(
            opentelemetry_sdk::trace::Sampler::TraceIdRatioBased(0.01), // 1%
        )))
        .with_resource(opentelemetry_sdk::Resource::new(vec![
            opentelemetry::KeyValue::new("service.name", service_name.to_string()),
            opentelemetry::KeyValue::new("service.instance.id", std::env::var("AUTTER_RUNTIME_INSTANCE_ID").unwrap_or_default()),
            opentelemetry::KeyValue::new("service.version", std::env::var("GIT_SHA").unwrap_or_default()),
            opentelemetry::KeyValue::new("deployment.environment.name", std::env::var("DEPLOYMENT_ENVIRONMENT").unwrap_or_default()),
        ]))
        .build();

    opentelemetry::global::set_tracer_provider(provider.clone());
    Ok(provider)
}
```

Use the `http-proto` feature (protobuf, matches Autter's default) unless
the user's existing exporter setup already uses `http-json`.

**HTTP auto-instrumentation**: for Axum/Actix/Tower-based services, use
`tower-http`'s `TraceLayer` or the framework's tracing middleware, bridged
to OTel via `tracing-opentelemetry`, rather than hand-instrumenting every
handler.

**Reporting errors**:

```rust
let span = tracing::Span::current();
span.record("error", true);
// or, with the raw OTel API on a span directly:
span.record_exception(&err);
span.set_status(opentelemetry::trace::Status::error(err.to_string()));
```

**Grouping (both languages)**: `RecordError` (Go) and `record_exception`
(Rust) attach the exception type, message, and backtrace. Autter parses the
Go and Rust stack formats server-side and groups by the top function and
file — so the same panic stays one issue across re-deploys (line numbers
and pointer offsets are ignored) and two different panics that share a
message stay separate. Keep the backtrace on the event; do not strip it.
For Rust, build with a backtrace available (`RUST_BACKTRACE=1`, or capture
it in your error type) so the frames reach the exception event.

## Request metrics (both languages)

The trace setup above alone gives Autter only 1%-sampled trace-derived
usage rollups. Unsampled request stats — what the slow-process monitor
uses for accurate HTTP coverage — come from the OTel HTTP-server duration
histogram (`http.server.request.duration`, or the older
`http.server.duration`; Autter's ingester folds exactly these two and
does not treat memory gauges as request metrics), which only exists once a **meter provider** is
registered.

**Go** — add alongside the tracer provider; `otelhttp` then records the
histogram automatically:

```go
import (
    "go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
    sdkmetric "go.opentelemetry.io/otel/sdk/metric"
)

mexp, err := otlpmetrichttp.New(ctx,
    otlpmetrichttp.WithEndpointURL("https://otlp.autter.dev/v1/metrics"),
    otlpmetrichttp.WithHeaders(map[string]string{
        "authorization": "Bearer " + os.Getenv("AUTTER_RUNTIME_KEY"),
    }),
)
if err != nil {
    return nil, err
}
mp := sdkmetric.NewMeterProvider(
    sdkmetric.WithResource(res),
    sdkmetric.WithReader(sdkmetric.NewPeriodicReader(mexp)), // 60s default
)
otel.SetMeterProvider(mp)
// call mp.Shutdown(ctx) alongside the tracer shutdown
```

Go can then add heap and GC measurements to that provider (after importing
`runtime` and `go.opentelemetry.io/otel/metric` as `metric`):

```go
meter := otel.Meter("autter-process-memory")
heap, _ := meter.Int64ObservableGauge("autter.process.memory.heap.used", metric.WithUnit("By"))
gcCount, _ := meter.Int64ObservableCounter("autter.process.gc.count", metric.WithUnit("{collection}"))
gcMs, _ := meter.Int64ObservableCounter("autter.process.gc.duration", metric.WithUnit("ms"))
_, err = meter.RegisterCallback(func(ctx context.Context, observer metric.Observer) error {
    var stats runtime.MemStats
    runtime.ReadMemStats(&stats)
    observer.ObserveInt64(heap, int64(stats.HeapAlloc))
    observer.ObserveInt64(gcCount, int64(stats.NumGC))
    observer.ObserveInt64(gcMs, int64(stats.PauseTotalNs / 1_000_000))
    return nil
}, heap, gcCount, gcMs)
if err != nil { return nil, err }
```

This provides heap-based growth detection without RSS. Add a current RSS
gauge from an OS collector when container-pressure diagnosis is needed; do
not substitute `HeapSys` for a limit. Configure the resource with a full
release SHA and process instance ID, not just a service name.

**Rust** — there is no ubiquitous HTTP-server metrics middleware; either
record the histogram yourself in a small middleware, or tell the user
their usage stats stay trace-derived (a 1%-sampled lower bound). Manual
recording that Autter folds correctly:

```rust
// once, after building an SdkMeterProvider with an OTLP exporter pointed
// at https://otlp.autter.dev/v1/metrics (mirror the trace exporter config):
let hist = opentelemetry::global::meter("http-server")
    .f64_histogram("http.server.request.duration")
    .with_unit("s")
    .build();
// per request, in middleware:
hist.record(elapsed_secs, &[
    KeyValue::new("http.request.method", method),
    KeyValue::new("http.route", route),           // the pattern, not the raw path
    KeyValue::new("http.response.status_code", status as i64),
]);
```

(Metric-SDK builder APIs move between `opentelemetry_sdk` versions —
check the docs of the version you pinned.)

## Instrumenting slow processes (both languages)

Autter's dashboard flags processes that are slow AND repeating a lot
(performance incidents with an automated optimization analysis and, when
safe, an automated fix PR). HTTP routes are covered automatically via
unsampled request metrics; non-HTTP work — background jobs, queue
consumers, cron ticks — is only visible where a span exists. Wrap each
recurring unit of work in a span named after the job (Go:
`tracer.Start(ctx, "invoice.rebuild")` around the job body; Rust: a
`tracing` span bridged via `tracing-opentelemetry`). At the default 1%
ratio sampler these spans would mostly be dropped — give job spans an
always-on tracer provider (same pattern as the error path) or accept that
counts are a lower bound. Stable, low-cardinality names; ids go in
attributes.

## LLM calls (both languages)

Autter recognises spans following the OTel GenAI semconv (`gen_ai.*`
attributes) automatically and records each as an LLM call — model, tokens,
latency, USD cost (estimated ingest-side unless the span reports
`autter.llm.cost_usd`). If the service calls LLM APIs (openai-go,
anthropic SDKs, Bedrock, raw HTTP), wrap each model call in a client span
with the semconv attributes. Go shown; Rust mirrors it with the
`opentelemetry` span API or a `tracing` span whose fields map through
`tracing-opentelemetry`:

```go
ctx, span := tracer.Start(ctx, "chat gpt-5-mini",
    trace.WithSpanKind(trace.SpanKindClient),
    trace.WithAttributes(
        attribute.String("gen_ai.operation.name", "chat"),
        attribute.String("gen_ai.system", "openai"),
        attribute.String("gen_ai.request.model", "gpt-5-mini"),
        attribute.String("autter.user_id", userID), // opaque — never an email
    ))
out, err := client.Chat.Completions.New(ctx, params)
if err != nil {
    span.RecordError(err)
    span.SetStatus(codes.Error, err.Error())
} else {
    span.SetAttributes(
        attribute.Int64("gen_ai.usage.input_tokens", out.Usage.PromptTokens),
        attribute.Int64("gen_ai.usage.output_tokens", out.Usage.CompletionTokens),
    )
}
span.End()
```

Never put prompts, completions, or PII in attributes — model ids, token
counts, and opaque user ids only.

**Sampling exemption — required.** At the 1% ratio sampler, 99% of LLM
spans are dropped and cost numbers become garbage. Either emit LLM spans
from a tracer built on a separate **always-on** provider pointed at the
same exporter (the pattern the slow-process section already uses), or
wrap the root sampler with one that returns "record and sample" whenever
the span name starts with `chat `/`embeddings `/`gen_ai.` or the creation
attributes contain a `gen_ai.*` key, delegating everything else
(implement `sdktrace.Sampler` in Go / `ShouldSample` in Rust — Autter's
Node package does exactly this internally).

## Selftest path (temporary — delete after verification)

A throwaway route that proves both pipelines in one hit — add, verify,
delete; never commit or deploy it. Go shown; mirror the same shape in
Rust:

```go
// TEMPORARY autter selftest — delete after verification.
mux.HandleFunc("/__autter-selftest", func(w http.ResponseWriter, r *http.Request) {
    _, span := otel.Tracer("autter-selftest").Start(r.Context(), "autter.selftest")
    span.AddEvent("exception", trace.WithAttributes(
        attribute.String("exception.type", "Message"),
        attribute.String("exception.message", "autter selftest"),
        attribute.String("autter.severity", "info"),
    ))
    span.SetStatus(codes.Error, "autter selftest")
    span.End()

    // LLM selftest — only when the service is wired for LLM tracing: one
    // fake call (no real model touched) proves gen_ai spans land.
    _, llmSpan := otel.Tracer("autter-selftest").Start(r.Context(), "chat autter-selftest",
        trace.WithSpanKind(trace.SpanKindClient),
        trace.WithAttributes(
            attribute.String("gen_ai.operation.name", "chat"),
            attribute.String("gen_ai.system", "autter-selftest"),
            attribute.String("gen_ai.request.model", "autter-selftest"),
            attribute.Int64("gen_ai.usage.input_tokens", 1),
            attribute.Int64("gen_ai.usage.output_tokens", 1),
            attribute.Float64("autter.llm.cost_usd", 0),
            attribute.Bool("autter.selftest", true),
        ))
    llmTraceID := llmSpan.SpanContext().TraceID().String()
    llmSpan.End()

    tp.ForceFlush(r.Context()) // the TracerProvider from initObservability
    mp.ForceFlush(r.Context()) // the MeterProvider, if wired
    w.Write([]byte(`{"ok":true,"llmTraceId":"` + llmTraceID + `"}`))
})
```

The ERROR status lives on the internal child span (that status is what
makes the ingester store the info-severity occurrence), so the request
itself doesn't count as failed in traffic stats. The `ForceFlush` calls
export immediately — no waiting on batch/interval timers. (Rust:
`provider.force_flush()` on both providers.)

At the default 1% ratio the selftest span inherits the request span's
sampling decision and is usually dropped before export: for the
verification run only, set the root sampler to 100%
(`TraceIDRatioBased(1.0)` in Go, `Sampler::TraceIdRatioBased(1.0)` in
Rust) and revert it together with the route.

## Verify (both languages)

1. Build and run with the real `AUTTER_RUNTIME_KEY` value set and the
   root sampler temporarily at 100% (revert after).
2. `curl` the selftest route once — the force-flushes run before it
   returns.
3. Confirm the exporters don't log a connection/auth error on export (a
   401 in exporter logs means the key is missing or wrong; a network error
   means the endpoint URL or outbound egress is blocked). No errors means
   both `/v1/traces` and `/v1/metrics` were accepted.
4. Ground truth in the dashboard (~1–2 min): an `autter.selftest` span,
   one info-severity "autter selftest" issue, and — if the meter provider
   is wired — request metrics for `/__autter-selftest`. Traces arriving
   without metrics means no meter provider: either wire it (section
   above) or tell the user usage stats are trace-derived at 1%.
5. LLM-wired services: the returned `llmTraceId` call appears under
   **Runtime → LLM** as provider/model `autter-selftest` (self-hosted: a
   `runtime_llm_calls` row). Remember the selftest ran with sampling at
   100% — real LLM calls only survive the default 1% run with the
   exemption from the LLM section in place; double-check it before
   trusting this signal.
6. Trigger one real error path too and confirm
   `RecordError`/`record_exception` was called on that span — the
   selftest proves transport, not your error-handler wiring.
7. Delete the selftest route and revert the sampling override.
