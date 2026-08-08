# Production readiness: Batch 1 packaging and image

Completed on 2026-07-29 against the Batch 0 working tree based on commit
`262169cbb381646888b1f665018acbba26de77f5`.

Batch 1 is limited to installable-package correctness, dependency locking,
and the reproducible application image. Compose topology, queue semantics,
runtime configuration, backup automation, and service operations remain for
later batches.

## Package-resource contract

Runtime code no longer assumes that it is running from a source checkout.

- SQLite schema loading uses `importlib.resources`.
- Persona YAML loading uses `importlib.resources`.
- The crypto ticker universe uses `importlib.resources`.
- The CLI welcome text uses `importlib.resources`.
- Secretary and delivery templates use Jinja `PackageLoader`.
- Setuptools package data explicitly includes all SQL, YAML, Jinja, and CLI
  text resources.

The distribution verifier requires all 20 runtime resources in both the wheel
and source distribution. The installed-package smoke test runs outside the
repository and rejects imports that resolve back into the source tree. It
then opens a fresh database, applies the packaged schema, loads packaged
personas and templates, and seeds the packaged crypto universe.

## Dependency contract

- Direct imports of `httpx`, `openai`, `pydantic`, `python-dateutil`, and
  `python-dotenv` are now direct project dependencies.
- Build, test, lint, typing, and dateutil typing tools are declared in the
  development extra.
- The `production` extra is limited to the approved RSS and Telegram
  ingestion stack plus the local sentence-transformer embedder. Polygon uses
  the core Requests dependency.
- `uv.lock` now describes `iic-forge` 0.2.5 rather than the inherited
  `tradingagents` 0.2.0 project.
- The current lock contains 202 cross-platform packages.
- Linux PyTorch is resolved from the official CPU-only index; CUDA, NVIDIA,
  and Triton packages are absent.
- The PEP 517 build backend is pinned to Setuptools 80.9.0 and Setuptools is
  not installed as an application runtime dependency.

Both CI and Docker use the frozen lock. Neither independently resolves
production dependencies with pip.

## Image contract

- Python 3.12.13 slim-bookworm and uv 0.11.19 are pinned by immutable
  multi-platform image digest.
- The application is installed non-editably from `uv.lock`.
- Only the installed virtual environment and pinned model cache are copied
  into the runtime stage; the source checkout is not copied.
- The runtime process uses an unprivileged UID 1000 account.
- Operator state and report directories are created mode 0700.
- The `sentence-transformers/all-MiniLM-L6-v2` model is pinned to commit
  `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, baked into the image, and
  forced offline at runtime.
- The image entry point is the installed `iic-forge` console script.

CI now builds the image and runs CLI, packaged-resource, SQLite, and offline
384-dimension embedding smoke tests inside it.

## Verification result

- `uv lock --check`: passed.
- Exact frozen `dev` plus `production` environment: installed 177 selected
  packages successfully.
- Full exact-lock suite: 915 passed, 2 skipped, 7 warnings, and 78 unittest
  subtests passed.
- Bytecode compilation: passed.
- Ruff on Batch 1 Python changes: passed.
- Mypy on Batch 1 production and verification modules: passed.
- Inherited repository-wide Ruff baseline: 226 findings, down from 227.
- Inherited repository-wide mypy baseline: unchanged at 83 errors in 30 of
  176 checked source files.
- Offline PEP 517 source distribution and wheel build: passed.
- Wheel resources: all 20 required files present.
- Source-distribution resources: all 20 required files present.
- Wheel installed outside the checkout with no dependency resolution:
  passed.
- Installed `iic-forge --help`, schema initialization, template loading,
  persona loading, and crypto seeding: passed.
- Pinned model download and 384-dimension inference: passed.
- Forced-offline reload and 384-dimension inference from the pinned cache:
  passed.
- CI workflow YAML and expected job structure: passed.
- Git diff whitespace validation and secret-pattern review: passed.

The seven warnings are inherited tests that intentionally exercise unknown
future Anthropic model names. They are not packaging or lock failures.

## Skipped tests and field procedure

### Live DeepSeek F1 exit gate

- Test:
  `tests/smoke/test_f1_exit_gate.py::test_f1_exit_gate_deepdive_aapl`
- Reason: a real `DEEPSEEK_API_KEY` was not provided to the isolated test
  process. The placeholder credential is intentionally rejected.
- Field procedure:

  ```bash
  export DEEPSEEK_API_KEY='operator-supplied-live-test-key'
  python -m pytest -q \
    tests/smoke/test_f1_exit_gate.py::test_f1_exit_gate_deepdive_aapl \
    -m integration --allow-external-network
  ```

### Live DeepSeek structured-output contract

- Test:
  `tests/test_deepseek_reasoning.py::TestDeepSeekLiveStructuredOutput::test_v4_flash_returns_structured_output`
- Reason: a real `DEEPSEEK_API_KEY` was not provided to the isolated test
  process.
- Field procedure:

  ```bash
  export DEEPSEEK_API_KEY='operator-supplied-live-test-key'
  python -m pytest -q \
    tests/test_deepseek_reasoning.py::TestDeepSeekLiveStructuredOutput::test_v4_flash_returns_structured_output \
    -m integration --allow-external-network
  ```

The exported key must exist before pytest starts. Loading it from `.env` is
intentionally disabled under pytest.

### Local Docker image build and container smoke

- Test: local `docker build` followed by the two container smoke commands.
- Reason: Docker, Podman, and BuildKit CLIs are not installed in the current
  workstation execution environment.
- CI procedure: the blocking `image-build` GitHub Actions job performs these
  checks on every pushed supported branch and pull request.
- Field procedure:

  ```bash
  docker build --tag iic-forge:batch1 .
  docker run --rm iic-forge:batch1 --help
  docker run --rm \
    --entrypoint python \
    --volume "$PWD:/workspace:ro" \
    iic-forge:batch1 \
    /workspace/scripts/installed_package_smoke.py \
    --forbid-root /workspace
  docker run --rm \
    --entrypoint python \
    iic-forge:batch1 \
    -c "from tradingagents.sensing.embeddings import SentenceTransformerEmbedder; e = SentenceTransformerEmbedder(); e.load(); assert len(e.embed('health check')) == 384"
  ```

### Remote GitHub Actions execution

- Test: hosted `test`, `image-build`, and `quality-baseline` jobs.
- Reason: Batch 0 and Batch 1 remain uncommitted and unpushed by design.
- Field procedure: after an approved commit and push, require the `test` and
  `image-build` jobs to pass. The inherited `quality-baseline` remains
  explicitly non-blocking until its findings are reduced in a later batch.
