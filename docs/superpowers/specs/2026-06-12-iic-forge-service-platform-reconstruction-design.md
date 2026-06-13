# IIC-Forge Service Platform Reconstruction Design

| Field | Value |
| --- | --- |
| Date | 2026-06-12 |
| Status | Design approved, pending written-spec review |
| Scope | Full service platform reconstruction, bounded for first production launch |
| Repository | `IIC-Forge` fork on branch `claude/iic-forge-05-impl` |

## 1. Purpose

IIC-Forge has finished the major F1-F5 and local-model development work, but the
runtime is not yet a coherent production platform. The latest branch contains the
important IIC-Forge 05 work, while the old host-oriented runtime is still shaped
around the earlier `TradingAgents` tree, hardcoded systemd paths, and an
externally owned `iic-redis` container. The goal of this reconstruction is to make
IIC-Forge canonical and production-owning before full-scale deployment.

This is Option C from the brainstorming session: a full service platform
reconstruction. It is not a rewrite of the investment logic. The existing
TradingAgents graph, Secretary workflows, prompts, personas, and analysis
behavior stay intact unless a later gate proves a specific behavior change is
needed. The reconstruction creates stronger service boundaries, owned runtime
infrastructure, durable retry semantics, and shared operational evidence.

## 2. Approved Decisions

- Make the IIC-Forge fork canonical.
- Stop or disable old test services from the `TradingAgents` tree.
- Do not migrate old Redis or old runtime data. Treat the old service as testing
  infrastructure.
- Use a Docker Compose stack as the production runtime source of truth.
- IIC-Forge owns Redis, app services, dashboard, healthchecks, volumes, env
  templates, and runbooks.
- Keep the local LLM endpoint external to Compose, configured through
  `LOCAL_LLM_BASE_URL` and probed by the app services.
- Include runtime data-model upgrades now, especially a unified `llm_calls`
  ledger and a durable deferred-salience retry queue.
- Delivery is ordered, not blind fan-out: Telegram bot is primary, email is the
  fallback, and every attempt is auditable.

## 3. Current Findings That Drive The Design

The current branch is clean at commit `262169c` and is 41 commits ahead of the
older sibling tree at `47e1ac3`. The latest branch is 35 commits ahead of
`main`, so canonicalization must cover branch/release and runtime deployment.

Deployment files still contain host-specific paths such as
`/home/ziwei-huang/TradingAgents/TradingAgents`. The current `redis-server`
systemd unit is an alias for an existing `iic-redis` Docker container. The
checked-in `ops/redis/redis.conf` explicitly says it is not loaded by the running
container.

The local-model branch significantly improves classification routing, schema
parsing, availability policy, shadow evaluation, and cost reporting. However,
operational evidence is still fragmented across `events`, `alert_evaluations`,
`costs`, `ops_counters`, and logs. Triage can record deferred salience, but the
current stream entry can still be acknowledged while retry depends on a source
republishing the same payload. Source health is mostly implicit in cursors,
logs, and event volume. Dashboard coverage is useful but does not yet answer
"what is alive right now?" for sources, Redis, local LLM, fallback, deferred
backlog, or delivery fallback.

Focused test probes found that many socket-based tests fail in the current
sandbox because local socket creation is blocked. That is an environment/profile
issue, but it should be explicit in the test strategy. A subprocess script test
also exposed packaging/entrypoint fragility: running
`scripts/compare_deepseek_prompt_cache.py` directly could not import
`tradingagents` in this environment.

## 4. Goals

1. Establish IIC-Forge as the only canonical production runtime.
2. Replace host-specific systemd ownership with a Compose-owned service stack.
3. Give IIC-Forge its own Redis container, volume, and loaded configuration.
4. Keep the local LLM external, visible, and healthchecked.
5. Convert deferred salience from a comment-level retry assumption into a
   durable retry workflow.
6. Create a unified `llm_calls` ledger covering classification, gate, summary,
   graph, synthesis, and fallback paths where practical.
7. Add a `source_health` ledger for adapters, cursor age, poll errors, channel
   resolution, and event volume.
8. Make delivery ordered and auditable: Telegram primary, email fallback.
9. Make the dashboard and focused soak gate read the same operational evidence.
10. Keep first launch on SQLite unless soak evidence shows SQLite is the blocker.

## 5. Non-Goals

- No migration of old Redis data or old SQLite/runtime state.
- No moving the local LLM server into Compose.
- No immediate Postgres or object-storage migration.
- No rewrite of the TradingAgents graph, analysts, personas, or investment
  decision logic.
- No expansion of local LLM routing to synthesis-quality workloads unless later
  shadow evidence supports it.
- No redesign of market-data vendors beyond health/cursor correctness needed for
  production operation.

## 6. Target Architecture

Compose is the production orchestrator for IIC-Forge. It owns:

- `redis`: Redis 7 container with IIC-owned volume and mounted IIC-owned config.
- `adapter-*`: one service per source adapter where separation matters
  (`polygon`, `telegram`, `x`, `rss`, `gdelt`, `macro`).
- `triage`: consumes Redis streams, dedupes, scores salience, writes events,
  writes `llm_calls`, and manages deferred retry.
- `promoter`: groups eligible scored events, runs alert gate, creates light
  alerts, suppressions, and pending actions.
- `worker-action`: handles accepted non-graph actions, refinements, and other
  lightweight follow-up work when separation from deep studies is useful.
- `worker-deep`: runs approved full studies and graph-heavy work with explicit
  concurrency and timeout policy.
- `action-handler`: consumes accepted actions and refinement requests.
- `delivery`: handles ordered Telegram/email delivery policy where separation
  from producers is useful.
- `dashboard`: Streamlit app or successor dashboard reading the shared control
  plane.
- `gate-runner`: command or service profile for focused smoke/soak checks.

Systemd may supervise the Compose project as a single host service later, but it
must not be the source of per-daemon truth. Systemd units that remain in the repo
should either be removed from the production path or converted into a thin
`docker compose up` supervisor.

The external local LLM is not a Compose service. It is configured through
environment variables and must be probed at startup and during runtime by the
roles that depend on it.

SQLite remains the launch store. Schema additions should avoid SQLite-specific
dead ends so a later Postgres migration can map tables and indexes directly.

## 7. Service Boundaries

Each service has one ownership area and communicates through durable state:

- Source adapters own source-specific polling/streaming and write envelopes to
  Redis plus health rows to SQLite.
- Triage owns dedupe, salience, event persistence, and deferred retry state.
- Promoter owns candidate selection, alert-gate evaluation, light-alert creation,
  suppression, and pending full-study actions.
- Worker lanes own job leasing, execution, timeouts, and completion/error state.
- Delivery owns channel attempt ordering and fallback.
- Dashboard and gate runner are read-only consumers except for explicit operator
  actions already modeled through `brief_actions`.

This design prefers explicit state transfer over hidden in-process handoff.
Runtime services should be restartable without losing events or forgetting why
an event is waiting.

## 8. Data Flow

1. Source adapters read external sources and write normalized envelopes to
   IIC-owned Redis streams.
2. Each adapter updates `source_health` with last poll time, last emitted event
   time, cursor, emitted count, consecutive failures, and error details.
3. Triage consumes Redis envelopes, performs dedupe, invokes salience scoring,
   writes `llm_calls`, and persists scored events.
4. If salience cannot be scored because the local endpoint is unavailable,
   response parsing fails, or another retryable LLM failure occurs, triage writes
   a durable deferred-salience row and schedules retry with backoff. The payload
   raw path and retry reason are preserved.
5. Promoter reads only scored eligible events. It invokes the alert gate, writes
   `alert_evaluations` for the business decision and `llm_calls` for the runtime
   call evidence, then creates light alerts and pending full-study actions.
6. Worker lanes process accepted actions and deep studies with explicit
   concurrency. The launch configuration can keep heavy graph work at
   concurrency 1 while preserving a lane model for later scaling.
7. Delivery attempts Telegram first. If Telegram fails or is unavailable, email
   is attempted as fallback. Every attempt is written to `deliveries`.
8. Dashboard and gates read from the same state: `source_health`, `llm_calls`,
   deferred retry rows, queue jobs, deliveries, costs, events, and ops counters.

## 9. Data Model Additions

### 9.1 `llm_calls`

Purpose: one runtime ledger for model calls, separate from business result
tables. `costs` can remain run-scoped; `alert_evaluations` can remain a gate
business-decision table. `llm_calls` answers operational questions across every
role.

Recommended fields:

- `call_id` primary key.
- `created_ts`.
- `role`: examples include `triage_salience`, `alert_gate`,
  `light_alert_summary`, `graph_deep`, `graph_quick`, `synthesis`,
  `refinement_classifier`, `morning_digest`.
- `service_name`: Compose service or logical caller.
- `provider`, `model_id`, `base_url`.
- `request_kind`: `chat`, `structured`, `embedding`, or similar.
- `linked_type`: `event`, `brief`, `run`, `job`, `shadow_eval`, or `none`.
- `linked_id`.
- `status`: `success`, `transport_error`, `parse_error`, `timeout`,
  `fallback_used`, `skipped`, or equivalent normalized values.
- `latency_ms`.
- `parse_ok`.
- `fallback_mode` and `fallback_used`.
- `in_tokens`, `out_tokens`, `cache_hit_tokens`, `cache_miss_tokens` when known.
- `usd_estimate`: `0.0` for local calls, `NULL` for unknown, nonzero for
  priced API calls.
- `error_class` and truncated `error_message`.

### 9.2 `source_health`

Purpose: make source liveness and cursor correctness queryable.

Recommended fields:

- `source` primary key.
- `service_name`.
- `last_poll_ts`.
- `last_success_ts`.
- `last_event_ts`.
- `cursor`.
- `cursor_updated_ts`.
- `events_emitted_total`.
- `events_emitted_last_poll`.
- `consecutive_failures`.
- `last_error`.
- `last_error_ts`.
- Source-specific JSON fields for resolved Telegram entities, API quota state,
  or provider diagnostics.

### 9.3 Deferred Salience Retry

Purpose: make retry durable and visible.

Recommended fields:

- `retry_id` primary key.
- `event_id` or temporary envelope id.
- `source`.
- `raw_path`.
- `payload_hash` or external id.
- `reason`.
- `attempt_count`.
- `next_attempt_ts`.
- `last_attempt_ts`.
- `state`: `pending`, `running`, `done`, `dead`.
- `last_error`.

The exact table/stream pairing can be decided during implementation planning.
The invariant is stronger than the mechanism: retry must not depend on source
republishing after the Redis entry has been acknowledged.

### 9.4 Delivery Attempt Chain

Purpose: encode Telegram primary and email fallback without ambiguity.

Recommended delivery additions:

- `delivery_group_id`: same value for all attempts for one brief/channel policy.
- `attempt_rank`: 1 for Telegram primary, 2 for email fallback.
- `fallback_of`: nullable reference to the failed primary delivery attempt.
- `is_fallback`: boolean/integer.
- `failure_reason`: explicit failure or skip reason separate from channel ref.

Rules:

- Telegram success means email fallback is not sent by default.
- Telegram failure or channel unavailable triggers immediate email fallback.
- Telegram quiet-hours skip should also skip email by default unless the brief is
  marked urgent.
- If both Telegram and email fail, the delivery group is failed and visible in
  dashboard/gates/self-alerts.

## 10. Error Handling Policy

### Local LLM

At startup, services that require local classification probe the external local
LLM. With fallback disabled, they fail closed if the endpoint is unavailable.
With fallback explicitly enabled, they route according to the configured API
fallback policy and burn a visible budget.

At runtime, triage defers affected events into the retry queue. Promoter skips
the affected cycle. Both record `llm_calls` rows and self-alert once per outage,
with debounce and re-arm after success.

Transport failures and parse failures are different classes. Parse failures are
counted as model-output failures, not endpoint-down evidence, unless the caller
policy says otherwise.

### Sources

Adapters should stay alive under bounded backoff and record health state. A
stale source is a dashboard/gate failure, not a silent log concern.

GDELT newest-first cursor handling should get an explicit regression test like
the existing macro cursor test. Telegram sensing should log and record channel
entity resolution so a dark channel is visible.

### Redis

Redis unavailability is not hidden by in-memory fallback. Dependent services
fail healthchecks or stop processing until Redis is healthy. Redis must use the
checked-in intended persistence/eviction settings, not an undocumented container
default.

### Workers

Worker timeouts should mark leased jobs according to policy and surface capacity
signals. Abandoned work must not hide queue capacity. The heavy graph lane can
remain single-concurrency for launch, but its backlog and timeout behavior must
be measurable.

### Delivery

Delivery failures do not crash producers. They create failed delivery attempts,
trigger configured fallback, and surface failed groups in dashboard and gates.

## 11. Dashboard And Gates

The dashboard should add an operational status layer:

- Compose service status or last heartbeat.
- Redis health and persistence settings.
- External local LLM probe status by role.
- `llm_calls` volume, latency, parse rate, fallback use, and provider split.
- Source health by adapter: last poll, last event, cursor age, consecutive
  failures, resolved Telegram channels.
- Deferred salience queue depth, oldest pending age, retry/dead counts.
- Worker lane queue depth, running jobs, errors, timeouts, and backlog age.
- Delivery attempt chains and failed fallback groups.
- Cost and token trends, still preserving `0.0` local cost versus `NULL`
  unknown cost.

The focused soak gate should read the same evidence as the dashboard. A healthy
focused soak proves:

- Old test services are stopped or disabled.
- Only the Compose IIC-Forge runtime is active.
- Redis is owned by the Compose stack and answers with intended config.
- Source health is non-stale for enabled sources.
- Deferred retry queue drains or stays within an explicit threshold.
- Local classification calls are present in `llm_calls`.
- Parse failures and transport failures are within thresholds.
- Unexpected API classification spend is zero unless fallback is explicitly
  enabled and within budget.
- Worker backlog and job latency stay within SLA.
- Every brief has a delivery group with Telegram success or email fallback
  evidence.

## 12. Compose And Configuration Requirements

The production Compose stack should include:

- A named project, for example `iic-forge`.
- A single app image built from this repository.
- One service per long-running responsibility.
- A Redis service named for IIC-Forge, not `iic-redis`.
- Named volumes for Redis and IIC data.
- Env-file support from an IIC-specific template.
- Healthchecks for Redis, dashboard, and services that can expose a health
  command.
- Dependency rules that wait for Redis health before consumers process.
- Profiles or commands for smoke/gate execution.

The env template should cover:

- Database and data directory paths inside the container/volume.
- Redis URL pointing to the Compose Redis service.
- Local LLM provider/model/base URL settings.
- Delivery credentials and ordered fallback policy.
- Source adapter enablement and source-specific credentials.
- Worker lane concurrency and timeout settings.
- Fallback policy and budgets.

No production Compose, env template, runbook, or gate should reference the old
`TradingAgents` path or old `iic-redis` container.

## 13. Test Strategy

Validation should be layered:

1. Unit tests for new schema helpers: `llm_calls`, `source_health`, deferred
   retry rows, and delivery attempt chains.
2. Contract tests for Compose files and env templates: service names, volumes,
   Redis config mount, healthchecks, external `LOCAL_LLM_BASE_URL`, and absence
   of old paths.
3. Adapter correctness tests: GDELT cursor newest-first regression, Telegram
   channel/entity resolution logging, source-health updates on success and
   failure.
4. Runtime behavior tests: deferred salience retries with backoff, no silent
   XACK loss, local LLM failure counters, fallback policy, and worker lane
   timeout/recovery.
5. Delivery tests: Telegram success suppresses email fallback; Telegram failure
   triggers email fallback; both attempts are recorded and gate-readable.
6. Smoke/soak gates: Compose stack starts cleanly, old services are stopped,
   Redis has intended settings, source health is non-stale, local LLM role calls
   are recorded, no unexpected classification API spend occurs, and queue and
   delivery SLAs are met.

Socket-based tests that require local loopback servers should be marked and run
in an appropriate profile. The default suite should not appear broken simply
because a sandbox blocks socket creation.

Subprocess scripts should run from the repository root without hidden
`PYTHONPATH` assumptions. Either script entrypoints must handle imports robustly
or tests must invoke installed console commands consistently.

## 14. Rollout Sequence

1. Canonical runtime foundation: Compose stack, app image, owned Redis, env
   template, external local LLM config, and old-service shutdown checklist.
2. Control-plane schema: `llm_calls`, `source_health`, deferred retry state, and
   delivery attempt chain fields.
3. Service refactor: wire adapters, triage, promoter, worker lanes, delivery,
   dashboard, and gate runner to the new contracts.
4. Operational dashboard and gates: make dashboard and focused soak read the
   same evidence.
5. Cutover rehearsal: start Compose stack against fresh owned state, verify old
   services are stopped, run targeted smoke.
6. Focused soak: validate real runtime behavior before declaring full-scale
   readiness.

Implementation planning should split this sequence into small reviewable
phases. The design is a single runtime target; the implementation should still
land incrementally.

## 15. Risks And Mitigations

| Risk | Mitigation |
| --- | --- |
| Scope grows into an unbounded rewrite | Preserve existing graph and Secretary behavior; reconstruct runtime boundaries first. |
| Compose hides Python debugging | Keep service commands plain, logs structured, and runbooks explicit. |
| SQLite becomes a bottleneck | Keep first launch on SQLite, but monitor write contention and model schema for later Postgres migration. |
| External local LLM remains a fragile dependency | Probe it, record failures in `llm_calls`, self-alert, and keep fallback explicit. |
| Delivery fallback spams channels | Use ordered attempt chains and per-brief policy; email only after Telegram failure by default. |
| Source health thresholds create false alarms | Start with transparent dashboard evidence, then tune gate thresholds from soak observations. |
| Old services accidentally keep running | Add shutdown checklist and gate assertion that old unit/process names are absent. |

## 16. Completion Criteria

The reconstruction is complete when:

- The Compose stack can start IIC-Forge from the canonical fork on fresh owned
  state.
- Old test services are stopped or disabled and not participating in runtime.
- Redis is owned by IIC-Forge and uses the intended config.
- Enabled sources show healthy `source_health`.
- Triage deferred retry is durable, visible, and bounded.
- `llm_calls` proves local classification routing and fallback behavior.
- Dashboard and focused soak gate agree on operational status.
- Telegram-primary delivery with email fallback is recorded and gate-readable.
- A focused soak passes with no hidden API classification spend, no unexplained
  stale sources, and no missing delivery attempts.
