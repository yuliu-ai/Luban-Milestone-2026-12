[English](provenance.md) · [简体中文](provenance.zh-CN.md)

# Source provenance and distribution boundary

This candidate comes from the current `Final agent` working tree delivered on
2026-10-07, itself derived from OmniRoboAgent commit
`bc37672c7baa68c50effdb464ee56e2746bd9dae` plus uncommitted release-boundary and
RoboCasa rendering fixes. The entire relevant current file tree, including those
changes and untracked source/tests, was the preparation input. A fresh history is
used; the parent's unreviewed Git history is not redistributed.

Upstream reference: [OmniRoboAgent](https://github.com/choucaicai/OmniRoboAgent).
The runtime namespace is now `luban`. The Apache-2.0 license and original file-level attribution remain.
Preparation modifications are described in
[the change record](https://github.com/Luban-Agents/Luban/blob/codex/private-readiness/impl_docs/changes/2026-10-07-private-github-preparation.md).
No authors, paper, DOI or benchmark performance number was invented for this copy.

Included: framework/atomic-skill source, owned Chemistry protocol/worker source,
tests, scripts, configuration templates, small JSON/JSONL catalogs and knowledge
examples, user/developer documentation. Knowledge templates are examples, not new
empirical validation of the cited research or cross-robot transfer.

Excluded from Git/source distributions: copied SpatialLM, standalone robot/navigation
workspaces and vendor CAD, model/checkpoint files, datasets/assets, generated runs,
private credentials, internal operational histories and original Git metadata.
The preparation machine keeps a private ignored preservation area; it is not part
of the delivered repository or an installation dependency.

Third-party SDKs are external, not implicit parent-directory imports. The source
reference manifest is [third_party/SOURCES.toml](https://github.com/Luban-Agents/Luban/blob/codex/private-readiness/third_party/SOURCES.toml).
OpenPI's exact archive remains an optional dependency. EmbodiedBench's no-license
status was checked against the upstream GitHub license endpoint; its source is not
copied here and the legacy dependency preset is not advertised as supported.
See [THIRD_PARTY_NOTICES.md](https://github.com/Luban-Agents/Luban/blob/codex/private-readiness/THIRD_PARTY_NOTICES.md).

## 2026-10-23 additional integration

The RoboSynChallenge adapter, policy backend, evaluator and tests are imported from OmniRoboAgent `79d372155e765337f118c4dc9170afd412257fb4`, adapted to the `luban` namespace and configurable resource paths. SDKs, models and standalone robot workspaces remain external installations.
