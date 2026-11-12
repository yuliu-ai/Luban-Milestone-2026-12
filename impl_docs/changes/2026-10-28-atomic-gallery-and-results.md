# Atomic-skill GIF gallery and report-backed results

Date: 2026-10-28
Related plan: [Luban presentation](../plans/2026-10-08-release-presentation.md)

## Changed

Replace the four static skill thumbnails in each homepage with nine named animated examples, one per domain. Publish all 64 original GIFs in bilingual galleries with category navigation, call identifiers, Chinese descriptions and recording provenance. Preserve original media bytes and distinguish the v2.4 demonstration conditions from the v2.5 call evaluation.

Add the three-round independent atomic/composition results and technical-report tables for planning quality, paired experience retrieval, LIBERO suite outcomes and physical-robot navigation. Keep the existing LIBERO original-ID definition and 2,000 versus 2,066 execution budgets, all atomic/composition failures, and both positive and negative experience outcomes. Preserve repository visibility and existing benchmark reference rows.

## Files

- [English homepage](../../README.md) and [Chinese homepage](../../README.zh-CN.md).
- [English gallery](../../docs/atomic-skills.md) and [Chinese gallery](../../docs/atomic-skills.zh-CN.md).
- [English results](../../docs/reported-results.md) and [Chinese results](../../docs/reported-results.zh-CN.md).
- [Media manifest](../../docs/assets/skills/gifs/manifest.json), 64 GIFs and two evidence JSON files.
- Documentation navigation and the existing presentation plan/task status.

## Verification

The focused staging command `python3 .codex-luban-atomic-gallery-20261028/validate.py` checks changed Markdown links, whitespace, bilingual gallery membership, all 64 media hashes and 1,631 decoded frames. It recounts the local original attempts: 9,582/9,600 atomic trials and 3,749/3,750 composition trials, with unique independent atomic IDs and 50 trials per item per round. Nine previews are present in each homepage and 64 in each full gallery.

GitHub's Markdown renderer was used for the preview HTML. Desktop and 390-pixel mobile previews were inspected in the in-app browser, including image loading and captions. Technical-report values were checked against DOCX paragraphs/tables and relevant rendered pages. The bundled report renderer lacks some Chinese text glyphs, so the original DOCX XML remains the source for Chinese labels. Runtime and GPU tests were not rerun because the change only affects presentation, documentation and copied media.

## Remaining Work

None.
