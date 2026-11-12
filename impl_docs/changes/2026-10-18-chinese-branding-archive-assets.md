# Chinese presentation and source archive documentation assets

Date: 2026-10-18
Related plan: [Presentation materials](../plans/2026-10-18-presentation-materials.md)

## Changed

The previous GitHub run 39089550198 passed source tests and MkDocs build, but its extracted source archive lacked docs/assets/spatial-scene-topview.png. MANIFEST.in now includes documentation PNG files. The existing distribution verification checks this PNG and the Chinese SVG variants explicitly before installing and testing the archive.

Removed the architecture-caption sentence from both README editions. The Chinese README uses 六通智能体 in its wordmark, framework diagram title, prose and report labels. Chinese user-guide display names follow the same convention. English artwork, package identifiers, fenced commands, paths and repository URLs remain unchanged.

## Files

MANIFEST.in, scripts/check_distribution.py, README.md, README.zh-CN.md, Chinese user-facing documents, docs/assets/liutong-wordmark.svg, docs/assets/liutong-wordmark-dark.svg, docs/assets/liutong-framework.svg, implementation plan and TODO.

## Verification

The original source archive was built locally and reproduced the missing PNG. Rebuilding with uv build --sdist includes the required PNG and localized SVGs byte for byte. scripts/check_docs.py passes on both the checkout and extracted archive (344 pages, zero failures). Chinese fenced commands and URLs match the previous revision. SVG XML parsing and git diff --check pass. Full source and independent distribution validation runs in GitHub Actions after the push.

## Remaining Work

Netlify publication remains deferred until the new account is ready.
