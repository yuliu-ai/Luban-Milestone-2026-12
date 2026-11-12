# New-machine verification and private project page

Date: 2026-10-08
Related plan: [new-machine readiness](../plans/2026-10-08-new-machine-readiness.md)

## Changed

- Reviewed actual VoLoAgent, HoloAgent and OpenHands SDK project pages; added an original README, Chinese entry, readable 2×2 SVG and a reference record. No third-party page artwork/code/results were copied.
- Installed every declared optional client and the explicit HDF5/CPU Torch test group in a fresh isolated environment/cache. Shared environments and SDK files were not modified.
- Added complete source/catalog/documentation/distribution checks and pinned read-only GitHub Actions. Conditional hosted FFmpeg installation resolves the first runner's missing prerequisite. Loopback proxy bypass resolves the two observed WebSocket failures.
- Kept the native CloseBlenderLid success and two real graph-guided ScrubCuttingBoard horizon failures. Preserved post-processing truncation/timeout evidence separately from Runtime exceptions.
- Increased only reflection/advice token budgets, separated the advisory deadline, and reused compact episode evidence for advice. Added a regression proving evidence retention and original trace preservation. A standalone live advisory request on recorded episode evidence passed with four proposal-only candidates and no applied changes/new actions.
- Explicitly included CODE_OF_CONDUCT.md in the source distribution and asserted policy/config/SVG resources. Refined source SDK, NumPy and EGL setup instructions.
- Preserved an ignored recoverable old-history bundle, then pushed an independent sanitized preparation branch. Existing main was not force-pushed or rewritten.

## Files

README.md, README.zh-CN.md, docs/, configs/agents/xr1_graph_simple.yaml,
src/luban/agent_core/agents/self_evolving.py,
tests/unit/test_self_evolving_agent.py, scripts/, MANIFEST.in, pyproject.toml,
uv.lock, .github/workflows/ci.yml and implementation architecture/status documents.

## Verification

- Final all-extras CPU suite and independent source-archive suite: 973 passed each, zero skipped. The compact-advice regression is included; the focused file passed 22 tests. Wheel installation outside the checkout and source policy/config/SVG resource assertions passed.
- Ruff, format, strict mypy (163 files), 35 config constructors, all bundled Markdown targets, MkDocs strict and all four V2.1/V2.2 catalog/bundle validators passed.
- Fresh cloud run 37675885981 at 7d25ba7 passed both 972-test suites, core examples, wheel/source installation and resources. Exact final-head status is available in private Actions and the delivery receipt.
- All 95 locked public-index versions were queried through official PyPI vulnerability metadata: no findings. Initial pip-audit retry timed out; this is not a security guarantee or an audit of model weights/external GPU binaries.
- Candidate actual-key matches: zero. Gitleaks route-ID findings were manually checked; no broad secret allowlist was added. An earlier check matched all 1664 original-source baseline files. The final check found five original documentation files changed since that checkpoint and an additional deployment subtree; those files were preserved, not copied or reverted. This preparation edits/tests only the independent copy.

## Remaining Work

The legacy main branch retains 38 old path/hardware objects from preparation history.
Its replacement or a separate final repository needs the pending owner decision;
no force push is authorized yet. Public release also needs institutional/authorship
and the retained simulator-fixture redistribution decisions in THIRD_PARTY_NOTICES.md.
Full fresh GPU-stack installation, every external simulator/physical robot, selective
graph scheduling and the final full advanced post-processing configuration have not
been live-validated; the recorded component checks must not be presented as those runs.
