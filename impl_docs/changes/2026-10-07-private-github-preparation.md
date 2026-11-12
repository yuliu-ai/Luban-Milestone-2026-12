# Independent private GitHub preparation

Date: 2026-10-07
Related plan: ../plans/2026-10-07-private-github-preparation.md

## Changed

Prepared the current delivered working tree with uncommitted fixes as an independent
repository, preserving original source and starting fresh Git history. Retained
the package namespace and Apache-2.0 provenance. Excluded copied third-party trees,
weights, private operational history and automated release publishing. Added
portable CPU/live-model/real-simulation examples and complete usage documentation.

Fixed escaped diagnostic secret redaction, same-instance lifecycle locking,
finite latent interpolation, archive Git provenance and owner-relative config
precedence. Added explicit environment defaults and meaningful regressions. Fixed
the generated-output ignore rule to retain `configs/runs/`. Expanded source archive
resources to include catalog JSONL/image evidence required by existing integrity
tests, rather than weakening those checks. Updated vulnerable dependency pins and
removed the obsolete EB automatic dependency preset while retaining its adapter.

The final whole-tree path pass also removed legacy AutoDL defaults from developer
launchers and probe examples, resolved G0's sibling script without `/tmp` staging,
required explicit upstream model/interpreter inputs and redacted historical output
root prefixes. Absolute-path rejection tests retain their deliberate test literal;
it is not a runtime default or personal path. Two G0 startup regressions were added.

## Files

Source changes: serialization.py, config.py, module_evolution.py and latent.py.
Scripts: prepare_submission_study.py, link_robocasa_assets.sh, summarize_benchmarks.py,
ci.sh, check_docs.py and redacted security scanner. Packaging: pyproject.toml,
uv.lock, MANIFEST.in, .gitignore. Portable configs under configs/agents and
configs/runs. README, docs, architecture, provenance/license notices and ci template.

## Verification

See ../../docs/validation.md for current test counts, exact scope, dependency
versions, fresh installs and distribution checks. Real CloseBlenderLid seed 7:
396 environment steps, 25 chunks, task_success, 0 exceptions; full video decoded.
Live GLM text/mock counter: 2 requests, task_success. Original file fingerprints
unchanged. Candidate credential detections individually reviewed without exposing
secret values. GitHub repository creation explicitly requested Private and API
confirmed private=true before any push.

## Remaining Work

Private push completed; API confirmed private=true before and after push. Fresh GitHub clone installed independently and passed 956 tests / 14 optional skips; offline module and YAML CLI passed. Task status updated DONE on 2026-10-08.
Cloud CI template is not activated because current credential lacks workflow scope.
Public release remains unauthorized. The owner must confirm publication rights,
including historical simulator image fixture redistribution; external GPU-stack
fresh provisioning and broad live integration matrices are outside current validation.
