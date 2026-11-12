[English](development.md) · [简体中文](development.zh-CN.md)

# Development and tests

Read `AGENTS.md`, `rules/README.md` and
`impl_docs/architecture/overview.md` before changing contracts. Keep core independent
of simulator/robot SDKs. Existing `src/luban` and `src/omni_atomic_skills`
are retained; no new plugin manager or registry hierarchy was added.

```bash
uv sync --frozen --group dev --python 3.11
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m mypy
.venv/bin/python -m pytest
.venv/bin/python scripts/validate_configs.py
uv build
```

`bash scripts/ci.sh` runs the same core checks and builds distributions. The default
tests avoid paid APIs, hardware and simulator asset downloads. A few optional
protocol tests skip when their SDK is absent; install the selected extra to run
them. The validation document reports current counts and reasons. Keep costly
integration checks outside default tests.

New preparation regressions cover escaped diagnostic credentials, explicit YAML
defaults/path ownership, contended lifecycle updates, finite latent sequences and
archive provenance. Run `pytest tests/release/test_private_preparation.py` for them.
Use small focused tests for actual behavior fixes, not tests mirroring formatting.

The `.github/workflow-templates/ci.yml` template pins its upstream Actions by commit and
uses read-only contents permissions. It installs core first, then all optional
clients and the `full-test` group on a fresh Ubuntu runner without restoring a
pre-existing dependency cache. It does not publish packages, Pages or Releases.
This delivery does not enable GitHub Actions. Current local/cloud evidence is in [validation](validation.md).

For zero-skip CPU coverage:

```bash
uv sync --frozen --all-extras --group dev --group docs --group full-test --python 3.11
REQUIRE_NO_SKIPS=1 bash scripts/ci.sh
.venv/bin/mkdocs build --strict
.venv/bin/python scripts/check_distribution.py
```

The test-only group adds HDF5 replay and CPU tensor storage dependencies. The
separate protocol extras exercise local test peers, not real policy quality.
Catalog and bundle validators replay their contract examples in a temporary copy,
leaving historical reports unchanged. See [new-machine instructions](new-machine.md).

Source archives include configs, small catalogs, scripts, docs, tests and owned
chemistry code. The wheel includes package resources and the offline module.
Verify both after a packaging change; tests must also work after extracting a
source archive with no `.git` directory.

Document implementation tasks in `impl_docs/plans/`, update `impl_docs/TODO.md`
and add an actual change/verification record under `impl_docs/changes/`. Preserve
upstream attribution and review newly tracked files for secrets, private paths,
weights, data and third-party restrictions before any push.


Legacy developer probes under `scripts/study/` require their own SDKs and are
not part of the quickstart. G0 drivers use `LUBAN_SIM_PYTHON` (default:
the calling interpreter), the bundled sibling probe script and
`LUBAN_PROBE_OUTPUT` (default: `runs/g0`). RoboCasa roots use the adapter's
existing `LUBAN_ROBOCASA_ROOT`. They can launch multiple GPU processes;
none were executed during this preparation. XR-1 worker startup requires
`XR1_PYTHON` and `XR1_MODEL_PATH`; SAM3 startup requires `SAM3_PYTHON` and
`SAM3_CHECKPOINT`, with optional `LUBAN_PYTHON` and `SAM3_INPUT_IMAGE` overrides.
Historical JSON output path prefixes were replaced with `<HISTORICAL_PROJECT_ROOT>`;
these are diagnostic references, not variables consumed by configuration loading.
The catalog's evidence index identifies bundled fixtures. No numeric result changed.
The optional key-file launcher reads `LUBAN_API_KEY_FILE`, defaulting to
the calling user's `.config/luban/api_key`; the normal CLI reads environment
credentials directly and does not require that helper.
