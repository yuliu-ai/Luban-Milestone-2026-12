# Presentation material and architecture update

Date: 2026-10-18
Related plan: [Presentation materials](../plans/2026-10-18-presentation-materials.md)

## Changed

Updated bilingual README and project/report/console guides with supplied spatial-memory measurements, existing RoboTwin and RoboSynChallenge report results and the current offline-console scope. Added paired spatial report guides and retained existing LIBERO budget definitions, commands and package identity. Replaced docs/assets/luban-framework.svg with the exact user-supplied 2026-10-18 diagram. Current online links are identified as 2026-10-13 previews. A unified local website folder contains the updated pages and eight-case offline replay.

## Files

README.md, README.zh-CN.md, docs/project-page*.md, docs/reader-console*.md, docs/reported-results*.md, docs/spatial-memory-report*.md, docs/README*.md, docs/_sidebar.md, docs/assets/spatial-scene-topview.png, docs/assets/luban-framework.svg, mkdocs.yml, implementation plan and TODO.

## Verification

python scripts/check_docs.py passes with no broken local links or bilingual counterpart/code-block mismatches. git diff --check passes. The diagram matches the supplied SVG byte for byte. Measurements are attributed to supplied records, without a new GPU evaluation. Website links, media playback, layout and offline replay were checked in the separate local website delivery. All MkDocs navigation targets exist. A full MkDocs build was not run locally because MkDocs is not installed in the available runtime; the repository CI retains its strict documentation-build step.

## Remaining Work

The user explicitly authorized this GitHub update. Netlify deployment remains deferred while the new account is prepared. After deploying the local static site, update the Project/Demo/Offline Replay links to its new domain.
