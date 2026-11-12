# Luban README branding, existing frontend links and GPU onboarding

Date: 2026-10-08
Related plan: [release presentation](../plans/2026-10-08-release-presentation.md)

## Changed

Updated both README languages with original vector branding, Project/arXiv/Demo
entries and CPU/GPU onboarding. Reused the existing OmniRoboAgent-Page presentation
site; the alternative generated HTML draft is excluded from Git and distributions.
The existing frontend working copy was inspected and tested without modifying it.
Removed presentation-stage approval banners; retained source and media attribution.

Replaced the conflicting PyPI robosuite 1.5.2 install with fixed official source
5ce6643f3092639d08f7b0f90ed1c6a84f50552c. Added an inference-runtime dependency file
and a GPU preflight for imports, source/assets, CUDA, checkpoint shards and EGL.
The independent simulator does not install unused RoboCasa training dependencies.
No runtime/control-flow interfaces changed.

## Files

- README.md, README.zh-CN.md, docs/assets/luban-*.svg, docs/project-page.md.
- docs/installation.md, docs/episodes.md, docs/troubleshooting.md and affected navigation.
- requirements/robocasa-runtime.txt, scripts/check_gpu_setup.py, third_party/SOURCES.toml.
- scripts/check_distribution.py and MANIFEST.in cover the new source resources.
- THIRD_PARTY_NOTICES.md and CONTRIBUTING.md retain attribution without release-stage banners.

## Verification

- Existing frontend: `node tests/static-check.mjs` passed, 12 HTML pages / 322 targets.
- Full local CPU suite: 973 passed, zero skipped; Ruff, format and mypy passed.
- `scripts/check_docs.py`: 303 Markdown pages, zero broken local targets before this record.
- `mkdocs build --strict` and `git diff --check` passed.
- Fresh Python 3.11 simulator: 46 installed packages; `uv pip check` passed.
- Simulator preflight including a 64x64 EGL render passed; missing SDK/assets return exit 1.
- Existing Python 3.12 policy environment: CUDA/import/checkpoint preflight passed.
- Real run: configs/runs/robocasa_simple.yaml, target/index 0/seed 7/horizon 600,
  at most 38 chunks and 600 seconds. Native task_success at 376 steps / 24 chunks,
  zero exceptions. Result/trace/video recorded under runs/robocasa_simple/
  4876ee4b-e96d-4f13-9450-7e62bd798e25. Only the temporary policy service was stopped.
- Independent wheel install, outside-checkout example and source-archive tests passed;
  source archive: 973 passed, zero skipped. Required branding and GPU resources present.
- Credential/content scan: no newly introduced credentials or files above 10 MiB;
  unchanged redaction-test literals reviewed. Alternative frontend draft excluded.
- Remote visibility and CI are checked after pushing the final commit.

## Remaining Work

Final hosted Project/Demo URLs and an actual arXiv identifier are not yet available.
Their README entries resolve to the existing frontend guide until those URLs exist.
The policy environment and model were reused; no new driver installation, full
policy-stack provisioning, training, physical robot task or general async runtime
is claimed. Final repository visibility must remain Private; no release/site is published.
