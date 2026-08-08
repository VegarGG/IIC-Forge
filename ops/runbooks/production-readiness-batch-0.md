# Production readiness: Batch 0 baseline

Captured on 2026-07-29 from commit
`262169cbb381646888b1f665018acbba26de77f5`.

This document records the inherited baseline. It is not a claim that the
project is production-ready.

## Confirmed production target

- Private, single-operator deployment.
- Docker Compose is the canonical production deployment.
- Enabled ingestion connectors: RSS, Telegram, and Polygon.
- Enabled delivery channels: Telegram and email.
- All alerts queue during quiet hours and transport outages.
- Quiet hours: 22:00-07:00 in `Asia/Shanghai`.
- Combined daily LLM budget: USD 20, reset at midnight in `Asia/Shanghai`.
- Local encrypted backups only. Off-host disaster recovery is explicitly out
  of scope.
- Recovery targets apply only while local backup storage survives:
  RPO at most one hour and RTO at most four hours.

## Inherited verification baseline

The pre-change audit collected 908 tests. Under an explicitly isolated
environment with localhost bypassing the host HTTP proxy:

- 906 tests passed.
- 2 live DeepSeek tests were skipped because no live test credential was
  provided.
- Bytecode compilation passed.
- A wheel could be built.
- Ruff reported 227 violations.
- Type checking reported an extensive inherited error baseline.

The wheel build was not a successful installation acceptance test: runtime
SQL, YAML, and Jinja resources were absent from the built wheel. Packaging is
scheduled for Batch 1.

## Batch 0 controls

Batch 0 adds:

- collection-time opt-out from loading operator `.env` files;
- fixed placeholder API keys instead of preserving ambient credentials;
- disposable test data paths;
- external DNS and socket blocking with localhost still allowed;
- explicit `--allow-external-network` opt-in for integration-marked live tests
  using credentials exported before pytest starts;
- initial GitHub Actions tests, compilation, and build;
- non-blocking Ruff and mypy baseline jobs.

Ruff and mypy are intentionally non-blocking in Batch 0 because making the
inherited baselines release-blocking would leave CI permanently red. Later
batches will reduce and ratchet those baselines before the final release gate.

## Batch 0 exit criteria

- The full suite passes without real operator credentials.
- No Telegram, SMTP, data-vendor, or LLM request can reach a non-loopback
  endpoint during the default test run.
- Local OpenAI-compatible stub-server tests remain functional.
- Compilation and distribution build pass.
- CI configuration is valid and contains no credentials.
- The resulting diff contains no production schema or business-logic change.

## Batch 0 verification result

After adding the isolation contracts:

- 914 tests were collected.
- 912 tests passed.
- 2 live DeepSeek tests were skipped because no live credential was provided.
- 78 unittest subtests passed.
- The suite completed without an unhandled Telegram transport thread.
- Changed Python files pass Ruff.
- The inherited repository-wide Ruff baseline remains 227 findings.
- The inherited mypy baseline is 83 errors across 30 of 176 checked source
  files.
- Bytecode compilation passed.
- Wheel construction passed; the known missing-resource defect remains
  assigned to Batch 1.
- The GitHub Actions workflow parses as YAML and contains blocking test/build
  jobs plus explicitly non-blocking Ruff and mypy baseline steps.
