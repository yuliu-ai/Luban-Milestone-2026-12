# README research layout and reader-console link

Date: 2026-10-08
Related plan: [research layout](../plans/2026-10-08-readme-research-layout.md)

## Changed

Centered the actual icon/wordmark group inside its SVG, with centered tagline.
Replaced the plain header phrase with four graphic pillars ending in Memory.
Inserted the user-supplied full framework SVG unchanged and the existing frontend's
bilingual project abstract. Added dated What's NEW! milestones, reported Leaderboard
results, current Hardware scope, Reader Console and pending Citation and Acknowledgement.

The user corrected the console URL to luban-consoles-20261008.netlify.app/reader/.
No console deployment, physical robot claim, new evaluation or release is performed.
The existing frontend files and supplied diagram source remain unchanged.

## Files

README.md, README.zh-CN.md; docs/assets/luban-wordmark*.svg,
luban-pillars*.svg and luban-framework.svg; docs/reported-results.md,
docs/reader-console.md; documentation navigation and distribution resource assertions.

## Verification

- Both abstracts match the supplied frontend text; reported table values checked.
- LIBERO reported values retain execution-budget and denominator definitions.
- SVG XML and marker units checked; framework SVG preserved byte-for-byte.
- Browser layout: logo group center 639.66 px versus canvas center 640 px at 1280 px width.
- Local Markdown targets, strict MkDocs and git diff --check passed.
- Source package resource checks and final GitHub CI run on the pushed revision.
- Original console URL returned Site not found; replaced with the user's corrected
  URL. Web fetch of the corrected host was inconclusive; no live backend operation attempted.

## Remaining Work

Paper citation, authors, funding and acknowledgement details await the user.
Asynchronous execution remains the next-stage plan and is not implemented here.
