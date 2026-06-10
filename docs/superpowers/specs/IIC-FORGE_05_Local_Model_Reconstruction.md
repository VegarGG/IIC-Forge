# IIC-FORGE_05 — Reconstruction: Local Quick Model for Triage & Promoter

**Date:** 2026-06-09 · **Baseline:** `main` @ `6ce06b6` + `fix/full-brief-delivery-audit` (assumed merged per FORGE_04 Phase A) · **Scope:** software development only — hardware, OS, and model serving on the test box are out of scope per Ziwei.

## 0. Decision Summary

Route the two always-on, high-frequency classification workloads — **triage salience scoring** and the **promoter alert gate** (strict evaluator + light-alert summary) — to a **local quick model** served by **llama.cpp (`llama-server`)** on a separate test box, reached over LAN as an OpenAI-compatible endpoint. Candidate weights: **Qwen 3.6 27B** or **DeepSeek V4 Flash** (GGUF); the design is model-agnostic — both are config values, switchable without code changes. Everything else keeps API LLMs unchanged: the trading graph's deep model (`deepseek-v4-pro`) and quick model (`deepseek-v4-flash` via API) for analyst tool loops, debates, trader, PM; the Secretary's synthesis, deep dives, refinement, and morning digest.

Why this split is the right one: triage + promoter are the only components that run **24/7 at event rate** rather than at human-approval rate. They are pure single-shot classification (no tool calls, no long context, no reasoning chains) — exactly what a 27B-class local model handles well. And per the FORGE_04 audit, the promoter's no-terminal-state bug produced **815 evaluator calls in 12h** on the API meter; moving the gate local makes the always-on loop cost-free at the margin (the terminal-state bug still gets fixed — locality is not a license for waste, it changes latency and privacy, not correctness requirements). Side benefit: raw scraped OSINT text (X, Telegram) stops leaving the machine for classification.

What does **not** move and why: graph analysts need tool calling and long stable prompt prefixes (the DeepSeek API cache program); synthesis/deep-dive needs deep-model quality; morning digest is a full-graph workload. None are latency-critical at 24/7 rates. The in-process sentence-transformers embedder (`all-MiniLM-L6-v2`) already runs locally and is untouched.

## 1. Current Wiring (verified, the seams we cut)

Both target components build their LLM identically from **global** config — there is no per-role routing anywhere today:

- **Triage:** `sensing/triage.py:_main` → `create_llm_client(provider=C["llm_provider"], model=C["quick_think_llm"], base_url=C.get("backend_url"))`, wrapped in a sync `call_llm(prompt)->str` passed to `SalienceScorer`.
- **Promoter:** `orchestrator/promoter.py:166-173` → same call, and the resulting `llm` object is handed to both `evaluate_alert_strict` (`alert_evaluator.py:54`, raw `json.loads` parse) and the promoter's own `Secretary` instance, whose only LLM use on this path is the light-alert summary (`service.py:316`). So **one client swap covers all three call sites** in the promoter process.
- The worker, action handler, and morning CLI each construct their **own** Secretary with their own client — process-level isolation already exists, which is what makes this reconstruction surgical: no shared LLM object crosses the local/API boundary.

Factory facts that make this cheap: `factory.py` routes any `_OPENAI_COMPATIBLE` provider through `OpenAIClient`; `llama-server` speaks the OpenAI chat-completions API; the `ollama` provider already demonstrates the no-API-key + env-overridable-base-URL pattern (`api_key_env.py` maps it to `None`; `OLLAMA_BASE_URL` override in `openai_client.py:_resolve_provider_base_url`).

## 2. Design

### D1 — First-class `local` provider

Add `"local"` to `_OPENAI_COMPATIBLE` in `factory.py`. In `openai_client.py`: default base URL `http://127.0.0.1:8080/v1`, env override `LOCAL_LLM_BASE_URL` (this is where the test box LAN address goes, e.g. `http://192.168.x.x:8080/v1`). In `api_key_env.py`: map `"local"` to `LOCAL_LLM_API_KEY` treated as **optional** — `llama-server --api-key` is recommended on a LAN-exposed port, but absence must not raise (new semantics: optional-key provider; today's code raises when a mapped env var is unset, so the key-resolution branch needs a small `OPTIONAL_KEY_PROVIDERS` set covering `local` and `ollama`). Do **not** reuse the `ollama` provider: model-name conventions, capability flags, and future llama.cpp-specific options (grammar) shouldn't be entangled with Ollama semantics.

### D2 — Per-role LLM routing (the core change)

New config block in `default_config.py`, with env mapping:

```python
"llm_roles": {
    # Each entry: {"provider", "model", "base_url"}; any field None → global default.
    "triage_salience": {"provider": "local", "model": "qwen3.6-27b-instruct-q4_k_m", "base_url": None},
    "alert_gate":      {"provider": "local", "model": "qwen3.6-27b-instruct-q4_k_m", "base_url": None},
},
```

Env vars: `IIC_TRIAGE_LLM_PROVIDER/MODEL`, `IIC_ALERT_GATE_LLM_PROVIDER/MODEL`, `LOCAL_LLM_BASE_URL`. A single factory helper `create_role_llm(role: str, config) -> BaseLLMClient` resolves role → override → global fallback. Call-site changes are two lines each: triage `_main` uses `create_role_llm("triage_salience", C)`; promoter `main` uses `create_role_llm("alert_gate", cfg)`. **Rule: roles default to the global provider until explicitly overridden** — merging this is a no-op on the production box until the env flips, which is what makes shadow/cutover (Phase L2/L3) a pure config operation. This mechanism also subsumes the dead `refinement.classifier_llm` key flagged in FORGE_04 (delete it) and gives every future split (e.g. a cheaper digest model) a home.

### D3 — Capability entries + thinking-mode control

Add catalog/capability rows for the two candidate local models (`model_catalog.py`, `capabilities.py`): `supports_json_mode=True` (llama-server honors `response_format={"type":"json_object"}` and, stronger, GBNF grammar / `json_schema`), `supports_tool_choice=False` (irrelevant — no tools on these paths), no reasoning round-trip. **Thinking must be off on these paths**: both Qwen 3.6 and DeepSeek V4 Flash are hybrid-thinking families, and a thinking trace on a 10s-cycle classifier is pure latency. Pass `chat_template_kwargs: {"enable_thinking": false}` (llama-server supports template kwargs passthrough; fall back to the model's no-think prompt switch if a given GGUF template ignores it) via a new per-role `extra_body` field resolved by `create_role_llm`. Verify in the L2 harness that responses contain no `<think>` blocks; a response-side stripper in the role client is the belt-and-suspenders.

### D4 — Structured-output hardening rides along (FORGE_04 issue #8)

This reconstruction is the right moment to fix the classification parse path, because llama.cpp gives us something the API never did — **grammar-constrained decoding**:

- `SalienceScorer._parse` and `evaluate_alert_strict`: request `json_schema` response format derived from their Pydantic models (`AlertEvaluationPayload` already exists; give salience one too). With grammar enforcement, `invalid_json` becomes structurally impossible from the local model; keep the fence-tolerant fallback parser for the API-fallback path only.
- **Instrument the funnel:** count `invalid_json` / schema-validation failures per model into the existing run/eval telemetry (new columns on `alert_evaluations`: `model_id`, `parse_ok`, `latency_ms`). FORGE_04 found nobody can currently distinguish parse-failure rejects from genuine rejects — that ends here, since the same counters power the L2 agreement gate.
- **Stop caching fallback salience results** (`salience.py:109-117`, the 24h-TTL-on-failure bug). With a local endpoint, "LLM down" becomes a routine state (box reboots) — caching 0.1-salience for a day during an outage would silently bury a day of events.

### D5 — Availability policy: degrade loudly, fall back deliberately

The local endpoint is a new failure domain (separate box, LAN). Policy, configurable per role via `"fallback": "none" | "api"`:

- **Startup:** both services do an eager `/health` + 1-token completion probe (same fail-fast pattern as triage's eager embedder load) and log the resolved endpoint + model identity. Refuse to start the role on probe failure unless fallback is `api`.
- **Runtime (default `fallback: "none"`):** on local-endpoint failure, triage marks events `salience_source='deferred'` and leaves them **un-scored for retry** (not 0.1-scored), and the promoter skips the cycle. Both increment a failure counter that feeds the self-alerting channel FORGE_04 Phase B adds ("local LLM endpoint down" is exactly the kind of thing this system must tell its operator).
- **`fallback: "api"` (off by default):** after N consecutive failures, route the role to the global API provider with a hard daily call budget — bounded, deliberate, logged, and it reuses the role-routing mechanism (fallback is just a second role resolution).

### D6 — Telemetry and cost accounting

`run_recorder.estimate_usd` and the costs panel assume DeepSeek pricing. Local calls must record `provider='local'`, `usd_estimate=0.0` explicitly (not `None`, which currently means "unknown") so the cost dashboard distinguishes *free* from *unmetered*. Add tokens/sec and latency capture on the role client — these are the regression signals for the test box, and the dashboard costs panel gains a "local vs API call volume" split, which is also the visible proof the reconstruction is doing its job.

### D7 — What carries over for free

The salience prompt's cache-aware layout (stable prefix, volatile tail — `sensing/prompts.py`) was built for DeepSeek's API cache, but llama.cpp's slot/prefix KV cache rewards **exactly the same discipline** — keep the prompts byte-stable and the local server re-uses KV across the 24/7 stream. The existing prompt-prefix regression tests apply unchanged. No prompt rewrites are needed for the swap itself; any wording changes go through the L2 harness, not vibes.

## 3. Migration Plan (software phases; each lands behind config, trunk stays releasable)

**L0 — Plumbing (no behavior change).** D1 + D2 + D3 + D6. Roles default to global API. Unit tests: role resolution fallback chain, optional-key handling, `extra_body` passthrough. Contract test against a stub OpenAI-compatible server (FastAPI fixture in `tests/llm_clients/`) asserting request shape (json_schema, enable_thinking=false) — no GPU needed in CI.

**L1 — Classification hardening (still on API).** D4: schema-constrained salience + evaluator, parse telemetry columns, fallback-cache removal. Plus the triage event-loop fix from FORGE_04 (`asyncio.to_thread` around LLM/embed calls) — do it now because local-endpoint latency variance makes a blocked loop worse, and the shadow phase doubles call volume. Ships value even if the local box never arrives.

**L2 — Shadow evaluation (the gate that decides the model).** New `scripts/shadow_eval.py`: replays the last N (default 500) stored events/candidates through **both** the API quick model and the local endpoint, writing per-call rows (`model_id`, salience delta, evaluator verdict agreement, parse_ok, latency) to a `shadow_eval` table. Report: score MAE + threshold-crossing agreement at the live 0.85/0.9 operating points, evaluator verdict agreement (Cohen's κ), p50/p95 latency, parse-failure rate. **Run it twice — once per candidate model — and let the numbers pick Qwen 3.6 27B vs DeepSeek V4 Flash.** This doubles as the seed of the FORGE_04 Phase D labeled set: persist the replay set; hand-label ~50 of them while you're reviewing the disagreements.

**Exit gate L2 → L3 (acceptance, per chosen model):** threshold-crossing agreement ≥ 95% on salience and ≥ 90% on evaluator verdicts vs API baseline; parse failures = 0 (grammar-enforced); p95 end-to-end latency ≤ current API p95; 24h shadow soak with zero endpoint-related triage stalls.

**L3 — Cutover.** Flip the two role env vars on the production box; API path untouched and instantly revertible (revert = unset two env vars). 72h soak with the FORGE_04 F5 gate plus the new counters (local call volume, failure counter = 0, cost panel shows gate/triage API spend → 0).

**L4 — Ops hardening.** Endpoint-down self-alert wired into the Phase B alerting seam; systemd env updates (`LOCAL_LLM_BASE_URL` in the `.env` consumed by `iic-triage`/`iic-promoter` units only); runbook `ops/runbooks/local-llm.md` (probe commands, fallback flip, model-swap procedure = replace GGUF + restart llama-server, no IIC change).

**Sequencing vs FORGE_04:** FORGE_04 Phase A (terminal-state fix, delivery merge, Telegram repair) still goes **first** — this reconstruction must not become the excuse that makes the 815-reject loop "fine because it's free now". L0–L1 can proceed in parallel with Phase B; L2 needs the test box up.

## 4. Risks (software only)

The honest list: **(1) Quality regression at the gate** — a 27B quantized model is not deepseek-v4-flash; the entire mitigation is the L2 gate with hard agreement thresholds and the disagreement-review loop; if neither candidate passes, the design still pays for itself via D4/D6 and the role mechanism waits for a better model. **(2) New failure domain** — covered by D5 + L4; the key invariant is *no silent degradation*: every skipped cycle or deferred score is counted and alertable. **(3) Concurrency ceiling** — llama-server serves slots, not vLLM-grade batching; triage consumers + promoter contend on one box. Mitigated by L1's `to_thread` (loop never blocks), llama-server `--parallel` slots, and the fact that real event rate post-dedupe is low (85.5% dedupe per F3 gate); L2 measures contention honestly because shadow mode is the worst-case double load. **(4) Template/quantization drift on model swap** — GGUF chat-template quirks (thinking tags, stop tokens) differ per build; the L0 contract test + L2 harness re-run is the standing swap procedure, never a hot swap. **(5) Scope creep** — the temptation to move the morning digest or refinement local "while we're at it": don't; those are synthesis-quality workloads, and the role mechanism makes moving them later a config decision once shadow-eval-grade evidence exists.

## 5. Deliverables Checklist

| Phase | Code | Tests | Config/env |
|---|---|---|---|
| L0 | `local` provider; `create_role_llm`; capability+catalog rows; cost rows `provider/usd=0` | role-resolution units; stub-server contract test | `llm_roles`, `LOCAL_LLM_BASE_URL`, optional `LOCAL_LLM_API_KEY` |
| L1 | json_schema on salience+evaluator; parse/latency telemetry; no fallback caching; triage `to_thread` | parse-funnel units; loop-nonblocking test | — |
| L2 | `scripts/shadow_eval.py`; `shadow_eval` table | gate-report unit tests | shadow role entries |
| L3 | — (config flip) | 72h soak via existing exit gate + new counters | flip 2 role env vars |
| L4 | endpoint-down alert; runbook | alert-path unit | systemd env |

Estimated effort: L0+L1 ≈ one focused week; L2 harness ≈ 2–3 days plus soak time; L3/L4 ≈ 2 days. All independent of test-box readiness except L2 onward.
