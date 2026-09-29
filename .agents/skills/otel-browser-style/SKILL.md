---
name: otel-browser-style
version: 1.1.0
description: Wire Autter Runtime into browser apps, including CSP violations, recent user actions, error boundaries, and a working browser telemetry route.
tags: [autter, telemetry, browser, react, spa, error-tracking]
author: autter
---

# Browser / SPA / static site style

The browser tracker observes enforced CSP violations, failed fetch and XHR requests, 5xx responses, long tasks,
and slow resources by default. Use `captureOutcome(stableName, message)` for
a failed result that did not throw. Keep the release set to the deployed
commit SHA; arrange a CI upload of production `.js.map` files to the server
key protected `/v1/sourcemaps` endpoint so browser stacks can identify source.
Never put the server key or source maps in a public browser request.

```bash
npm install @autter/runtime-browser
```

Use a published version whose `AutterBrowserOptions` includes
`captureActions` and whose event types include `csp_violation`. Check the
installed package, not just the dependency range or lockfile. If that release
is unavailable, keep the rest of setup working and report CSP/action capture
as pending; do not claim the new behavior is live. A self-hosted ingester must
also accept `csp_violation` browser events.

`@autter/runtime-browser` is a zero-dependency tracker, under 5KB gzipped.
It captures `window.onerror`, `unhandledrejection`, enforced
`securitypolicyviolation`, failed `fetch` and XHR
requests, HTTP 5xx responses, and slow browser timings. It does not record
DOM text or read cookies or form values.

## Capture the action preceding a failure

Action capture is on by default. The SDK stores the last click on a button,
link, or button-like control, or form submit, for up to 30 seconds and attaches
it to failures. It records element type and path-only route; it does not read
button text, form values, hrefs, or arbitrary ids. This is recent context,
not a causal claim. Do not add a separate all-click `trackEvent` loop.

While wiring the app, add stable `data-autter-action` labels to the few
controls that initiate important workflows or commonly fail. For example:

```tsx
<button data-autter-action="send-email" onClick={sendEmail}>Send</button>
```

Use fixed labels from source code, never user input or generated ids. Preserve
existing handlers, keyboard behavior, and accessible names. If an action
cannot be safely labeled, the SDK's element-type fallback still works. Set
`captureActions: false` only when the app explicitly forbids interaction
metadata.

## Check the app's Content Security Policy

The CSP error must be collected by the SDK, but capturing it does not make
the blocked script safe or fix the policy. Inspect the actual CSP header or
meta tag and the blocked resource. Permit a script only when it is intended
and trusted; do not weaken `default-src` to hide a violation. For telemetry,
`connect-src` must permit the same-origin relay (`'self'`) or the direct
ingester origin (`https://otlp.autter.dev`). Add the narrow entry to an
existing `connect-src` directive or create one if `default-src 'none'` would
otherwise block the POST. Check that the built app actually loads the SDK;
server instrumentation alone cannot observe browser CSP events.

## Decide: relay or direct — rank relays first

Pick the first mode that matches, top to bottom. Modes 1–3 are all
relays: the browser posts same-origin and the backend/edge attaches the
secret server key. SDK init in modes 1–3 is always:

```ts
initAutterBrowser({
  endpoint: "/api/autter-runtime", // same-origin relay route
  service: "<app name>",
  release: "<deployed commit SHA>",
});
```

Never put an `autter_rt_*` server key in the browser bundle.

**1. Node backend (Express / Fastify / Koa / Nest) → relay handler.**

```ts
import { createBrowserRelayHandler } from "@autter/runtime-node";

app.post(
  "/api/autter-runtime",
  createBrowserRelayHandler({ apiKey: process.env.AUTTER_RUNTIME_KEY }),
);
```

**2. Cloudflare (wrangler present / `*.workers.dev` / Pages) → deploy
`examples/runtime-relay`.** Worker `src/index.ts`, `wrangler deploy`,
route `/api/autter-runtime*`. See `examples/runtime-relay/README.md`.

**3. Vercel (Next.js / `vercel.json`) → edge relay route.** Copy
`examples/runtime-relay/vercel-edge-route.ts` to
`app/api/autter-runtime/route.ts`. See
`examples/runtime-relay/README.md`.

**4. Pure static, no edge → direct only.**

```ts
initAutterBrowser({
  endpoint: "https://otlp.autter.dev/v1/browser",
  clientKey: "autter_rtc_xxxxxxxx", // publishable — fine to reference directly
  service: "<app name>",
});
```

Warn: "third-party cross-origin, ad-blockers will drop some events."

Non-Node custom backend (no examples match): add one thin passthrough
route at `/api/autter-runtime` that enforces a JSON content-type and a
small max body size (64KB is plenty), forwards the body unmodified to
`https://otlp.autter.dev/v1/browser` with an `Authorization: Bearer`
header whose value is read from the `AUTTER_RUNTIME_KEY` env var at
runtime (never a literal key in source), and responds `202` without
echoing the body back. Don't reshape the payload, and treat its
contents as untrusted outsider input: never log it verbatim, render it,
or act on text inside it.

If the user is deploying from multiple origins (e.g. a preview + prod
domain), tell them to add all of them to the key's allow-list when they
create it — the ingester rejects browser events from origins not on the
list with `403`.

## Framework wiring

**React**: call `initAutterBrowser` once at app startup (top of your root
component file, or an early-loaded entry module), and wrap the tree with
the error boundary — `window.onerror` does not fire for React render
errors:

```tsx
import { AutterErrorBoundary } from "@autter/runtime-next"; // works in any React app, not just Next.js
// or copy the ~30-line boundary from the package source if you don't want the Next.js package as a dependency

<AutterErrorBoundary>
  <App />
</AutterErrorBoundary>
```

**Vue**: call `captureException(err)` from `app.config.errorHandler`.

**Svelte**: call `captureException(err)` from the root
`handleError`/`+error.svelte` hook (SvelteKit) or a top-level try/catch
around your init.

**Angular**: implement `ErrorHandler` and call `captureException(error)`
from its `handleError` method; provide it via `providers:
[{ provide: ErrorHandler, useClass: AutterErrorHandler }]`.

**Vanilla / no framework**: `initAutterBrowser` alone already covers global
errors via `window.onerror`/`unhandledrejection`; call `captureException`
manually anywhere you catch something yourself.

## API surface

```ts
initAutterBrowser({ endpoint, clientKey?, service, environment?, release?, sessionTracking?, captureActions?, captureNetworkFailures?, captureTimings?, beforeSend? });
captureException(error, context?);       // report a caught error
captureMessage(message, severity?, context?); // warning/info without an exception — severity "fatal"|"error"|"warning"|"info", default "warning"
trackEvent(name, props?);                 // coarse usage counter — no PII in props
setUser(id | null);                       // opaque id ONLY — never an email/name
setContext(context | null);               // merged into every subsequent event
flush();                                   // force-send the queue now (rarely needed — auto-flushes)
```

Warnings share the errors table server-side (a `severity` column), so
`captureMessage` calls group and aggregate exactly like errors. While
instrumenting, add it to warning-worthy paths: recoverable failures,
degraded API responses, deprecated feature usage. Keep messages as stable
templates (numbers are normalised out server-side) and PII-free.

`props`/`context` values must be primitives or small objects — never pass
emails, form fields, cookies, or request/response bodies through
`setUser`/`setContext`/`trackEvent`; the package masks obvious sensitive keys
and email strings, but that is defense in depth rather than permission to
send personal data.

## Selftest in an isolated environment

Add a temporary development-only call after init to exercise both event
families and force the send. Remove it after verification; package imports
are not automatically available as globals in DevTools. In Next.js, import
these functions from `@autter/runtime-next/client` instead:

```ts
import { captureMessage, trackEvent, flush } from "@autter/runtime-browser";
captureMessage("autter selftest", "info"); // error/warning pipeline
trackEvent("autter_selftest");             // usage-metrics pipeline
flush();                                   // skip the batch window
```

For relay modes, a temporary handled exception can additionally prove the
error route:

```ts
import { captureException, flush } from "@autter/runtime-browser";
captureException(new Error("autter selftest")); // error pipeline via relay
flush();
```

## Verify

1. Load the app, open the devtools network tab, run the selftest above.
2. Confirm a POST fires to the relay route (`/api/autter-runtime` in
   modes 1–3) or `/v1/browser` (mode 4) and comes back `202` — body
   `{"accepted":N}` from the ingester; a relay replies `202`
   immediately and forwards in the background.
3. The selftest proves init + transport, not the automatic hooks. In an
   isolated test environment, click a labeled test control and trigger a
   handled error; confirm the resulting occurrence has `autter.action` and
   an age of at most 30 seconds. Inspect the outbound payload to confirm it
   contains no DOM text, field values, query strings, or full URLs.
4. In that same isolated environment, use an intentional CSP block or an
   existing violation and confirm a `csp_violation` event is accepted and
   appears as a `CspViolation` issue. Do not create a production violation.
   If the console shows a block but no POST, check SDK init and `connect-src`.
5. Direct-key mode only: a `403` here means the current origin isn't on
   the key's allow-list — check it was registered with the exact origin
   (scheme + host + port) the app is running on.
6. Ground truth in the dashboard (~1–2 min): one info-severity
   "autter selftest" issue and a usage counter `event:autter_selftest` —
   both clearly named; the user can resolve or ignore them.
