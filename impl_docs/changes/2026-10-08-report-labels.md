# Standardize report labels and metric definitions

Date: 2026-10-08
Related plan: [report labels](../plans/2026-10-08-report-labels.md)

## Changed

Standardized LIBERO labels to Without Agent / Agent in both README languages,
abstracts and the detailed results guide. Replaced ambiguous metric wording with
its explicit original-episode-ID formula, numerator counts and execution budgets.
Retained the user-supplied percentages and clarified the scope of their comparison.
Aligned related presentation planning and verification notes with the same terminology.
No runtime, configuration or experiment source records changed.

## Files

- README.md and README.zh-CN.md
- docs/reported-results.md
- Related research-layout plan/change record and impl_docs/TODO.md

## Verification

- Current tracked Markdown/HTML terminology scan: 310 pages passed before this record.
- Without Agent / Agent headings agree across both README languages and result guide.
- Suite percentages retained; counts sum to 1,934 and 1,978 over 2,000 episode IDs.
- Local documentation targets: 311 pages, zero failures before this record.
- Strict MkDocs build and git diff --check passed.
- Based on the current independent GitHub default-branch root, preserving its history.
- No new GPU episode or runtime tests required for this documentation-only update;
  repository CI runs automatically on the pushed revision.

## Remaining Work

Source experiment records remain the reference for future protocol updates.
No new evaluation or equal-budget conclusion is introduced by this wording change.
