# External sources for repository Runtime

Use this workflow when connecting existing provider logs instead of, or alongside, SDK instrumentation. Confirm that the target deployment exposes repository Runtime data sources before reporting it available. The polling and analysis workers live in the Autter platform; the open-source OTLP ingester does not run them.

## Configure

1. Identify the connected repository, provider project/service and environments. Reuse this scope on the settings page; do not fall back to an organization-wide default repository.
2. Open **Repository → Settings → Runtime → Data sources**. An owner/admin supplies provider read credentials through the settings form or its secret manager. Do not request or print credentials in chat, record private webhook URLs, or put provider keys in browser/application source.
3. Configure the provider:
   - Sentry: API base URL, organization/project slugs and a token with project-event read access.
   - PostHog: regional API base URL, project ID and personal key with project/event read access; optionally a named warning event. A browser project key is insufficient.
   - Grafana: Loki endpoint, scoped LogQL query, read token and optional Basic username. Optional Tempo URL/read credentials enrich linked traces.
   - Datadog: regional API endpoint, API and application keys with logs-read access and a service/environment-scoped search. Custom monitor webhooks use the generic record shape.
   - Webhooks: use the private URL returned once on save, with an optional shared-secret header. Send stable event IDs, timestamps, message/title, severity, service/environment and available stack/trace context.
4. Test access using the intended configuration. A successful read with zero samples proves access, not correct source filtering or delivery. HTTP 401/403 needs credential/permission repair; do not repeatedly retry it as a transient outage.
5. Preserve the user's automation choice. Collection-only stores records; investigation adds RCA; autofix allows eligible draft fixes. Warnings are always stored, but warning analysis is opt-in. Default autofix threshold is high/critical; medium requires an explicit policy choice. Do not lower severity thresholds merely to force a test PR.

## Verify each boundary

- Delivery: a unique occurrence appears in the correct repository with the original severity, message and available diagnostics. Duplicate retries must not inflate counts. Webhook `202` confirms committed ingestion on this new connector route; it does not mean RCA or a PR is complete. Polling is periodic (normally 60 seconds) plus provider indexing delay.
- Evidence: check retained stacks/source-map availability, release and trace context. A trace ID or metric alert alone does not justify a code fix. Provider logs supplement available evidence; they do not automatically supply full application traces.
- Analysis: inspect the persisted root cause, fix plan and source files or the explicit low-value/needs-evidence result. Do not manufacture production provenance for a replay or local reproduction.
- Fix: check repository autofix is enabled, the connection is active/in autofix mode, severity/environment are eligible and budget is available. Report queued/running/failed separately. Claim a created fix only after checking a real PR URL and draft state.
- An authorized bounded test may use an existing defect reproduced locally. Label it verification traffic, avoid deliberately breaking production, and clean up temporary connectors/secrets while retaining the diagnostic result.

## Operational boundaries

Historical backfill is stored without automatic investigations. Recovery requires a resolved webhook; absence of fresh errors does not imply recovery. Pausing/disconnecting stops new claims and is checked before publishing a running fix. Retained records remain visible after disconnect.

Project IDs are currently entered manually. Datadog retains supplied trace context without a separate APM lookup. API monitor-state reconciliation is not implemented. Preserve legacy organization connections until a tested repository connection replaces their destination; do not silently reassign old history. Keep old/new webhook destinations from running indefinitely together.

For self-hosted platform deployments, verify the API and independent collector/analysis workers share a persistent `CONNECTOR_ENCRYPTION_KEY` and that the existing GitHub/AI fix runner is deployed. Never rotate that key casually: existing encrypted connections depend on it.

Public setup guide: https://github.com/Autter-dev/docs/blob/main/runtime/external-sources.mdx
Runtime repository reference: https://github.com/Autter-dev/autter-runtime/blob/main/docs/EXTERNAL-SOURCES.md
